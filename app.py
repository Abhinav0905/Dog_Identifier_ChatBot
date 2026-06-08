"""
Dharamsala Animal Rescue Chatbot - Local Prototype
FastAPI application with all Phase 1 endpoints.
"""

import io
import uuid
import json
import logging
from types import SimpleNamespace
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import quote_plus

from fastapi import BackgroundTasks, FastAPI, UploadFile, File, Form, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, Response
from starlette.datastructures import Headers

import database as db
import config
from models import (
    ChatQueryRequest, LocationUpdateRequest, AdminQueryRequest,
    IncidentStatusUpdate, ChatResponse, AdminQueryResponse,
)
from services import (
    guardrails,
    triage,
    similarity,
    location,
    alerts,
    admin_analytics,
    image_processing,
    twilio_whatsapp,
)

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("dharmasala")

# In-memory store for triage results awaiting jurisdiction confirmation.
# Only used when STRICT_LOCATION_GATE=false. Keyed by token (UUID).
_pending_triage: dict[str, dict] = {}

app = FastAPI(
    title="Dharamsala Animal Rescue Chatbot",
    version="1.0.0-prototype",
    description="AI-powered chatbot for stray dog rescue triage and guidance",
)

# Initialize database on startup
@app.on_event("startup")
def startup():
    db.init_db()
    logger.info("Database initialized at %s", config.DB_PATH)
    logger.info("Storage directory: %s", config.STORAGE_DIR)
    logger.info("OpenAI API key configured: %s", bool(config.OPENAI_API_KEY))
    logger.info("Dharamsala service-area radius: %.1f km", config.DHARAMSALA_REGION_RADIUS_KM)


# --- Language detection ---

def _detect_language(accept_language: str, text: str = "") -> str:
    """Return 'hi' or 'en'.

    Prefers the user's actual input language (Devanagari detected), then falls
    back to the browser's Accept-Language header. Defaults to English.
    """
    # If the user typed in Hindi (Devanagari script), respond in Hindi
    if any("\u0900" <= c <= "\u097F" for c in text):
        return "hi"
    # Parse the primary language tag from the Accept-Language header
    primary = accept_language.split(",")[0].split(";")[0].split("-")[0].strip().lower()
    if primary == "hi":
        return "hi"
    return "en"


# --- Static files and UI ---

app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    return FileResponse(str(config.BASE_DIR / "static" / "index.html"))


@app.get("/admin.html", response_class=HTMLResponse)
async def serve_admin():
    return FileResponse(str(config.BASE_DIR / "static" / "admin.html"))


# --- Public APIs ---

@app.post("/v1/triage/image")
async def triage_image(
    request: Request,
    image: UploadFile = File(...),
    context: str = Form(""),
    session_id: str = Form(""),
    lat: float = Form(None),
    lng: float = Form(None),
    location_source: str = Form(""),
):
    """UC-1: Image-based distress assessment with jurisdiction check."""
    image_bytes = await image.read()

    # Check file size
    if len(image_bytes) > config.MAX_IMAGE_SIZE_MB * 1024 * 1024:
        raise HTTPException(400, f"Image exceeds {config.MAX_IMAGE_SIZE_MB}MB limit")

    try:
        media_type = image_processing.validate_upload(image_bytes, image.content_type, image.filename)
    except image_processing.ImageProcessingError as exc:
        raise HTTPException(400, str(exc)) from exc

    if not session_id:
        session_id = str(uuid.uuid4())

    # Guardrail check on context text
    if context:
        guard = guardrails.check_input(context)
        if not guard.allowed:
            return ChatResponse(response=guard.reason)

    # Step 1: Resolve location. Either in-region EXIF or browser GPS may pass.
    loc, lat, lng, loc_source = _resolve_upload_location(image_bytes, lat, lng, location_source)
    location_verification = loc or _missing_location_verification()
    _log_location_gate_decision(session_id, image.filename, location_verification)

    # Step 2: Determine jurisdiction.
    # Strict mode requires EXIF/browser coordinates. Soft mode keeps the older
    # self-confirmation fallback when no coordinates are available.
    if lat is None or lng is None:
        if not config.STRICT_LOCATION_GATE:
            return _defer_triage_for_location_confirmation(
                image_bytes=image_bytes,
                image=image,
                media_type=media_type,
                context=context,
                session_id=session_id,
            )
        response_text = _build_location_required_response()
        db.save_chat_message(session_id, "user", f"[Image uploaded: {image.filename}] {context}")
        db.save_chat_message(session_id, "assistant", response_text)
        return ChatResponse(
            response=response_text,
            in_jurisdiction=None,
            location_verification=location_verification,
        )

    in_region = location.is_in_dharamsala_region(lat, lng)
    if not in_region:
        if config.STRICT_LOCATION_GATE:
            response_text = _build_out_of_region_location_response(loc)
        else:
            lang = _detect_language(request.headers.get("accept-language", ""), context)
            triage_result = triage.analyze_image(image_bytes, media_type, context, lang)
            response_text = _build_out_of_region_response(triage_result)
        db.save_chat_message(session_id, "user", f"[Image uploaded: {image.filename}] {context}")
        db.save_chat_message(session_id, "assistant", response_text)
        return ChatResponse(
            response=response_text,
            in_jurisdiction=False,
            location_verification=location_verification,
        )

    # Step 3: Run vision triage — jurisdiction is verified
    lang = _detect_language(request.headers.get("accept-language", ""), context)
    triage_result = triage.analyze_image(image_bytes, media_type, context, lang)

    # Steps 4–8: Incident creation and alerting only for verified in-jurisdiction cases
    sha256 = similarity.compute_sha256(image_bytes)
    phash = similarity.compute_phash(image_bytes)
    blob_filename = f"{sha256}_{image.filename}"
    blob_path = config.STORAGE_DIR / blob_filename
    blob_path.write_bytes(image_bytes)

    # Check for duplicates/similar cases
    sim_result = similarity.run_similarity_checks(image_bytes, sha256, phash)

    # Create incident record
    incident_id = db.create_incident(
        session_id=session_id,
        image_blob_path=str(blob_path),
        image_sha256=sha256,
        image_phash=phash,
        lat=lat,
        lng=lng,
        location_source=loc_source,
        triage_severity=triage_result["severity"],
        triage_severity_score=triage_result["severity_score"],
        triage_confidence=triage_result["confidence"],
        triage_summary=triage_result["triage_summary"],
        distress_flags=triage_result["indicators"],
        similar_incident_id=sim_result.get("exact_match_id") or (sim_result["similar_incidents"][0]["incident_id"] if sim_result["similar_incidents"] else None),
        similarity_score=sim_result["similar_incidents"][0]["score"] if sim_result["similar_incidents"] else None,
        status="new",
    )

    # Log triage event
    db.create_triage_event(
        incident_id=incident_id,
        model_version=triage_result.get("model_version", "unknown"),
        raw_output=triage_result.get("raw_output", ""),
        postprocessed=json.dumps({
            "severity": triage_result["severity"],
            "severity_score": triage_result["severity_score"],
            "confidence": triage_result["confidence"],
            "indicators": triage_result["indicators"],
        }),
        latency_ms=triage_result.get("latency_ms", 0),
    )

    # Trigger escalation alert if needed
    escalation_triggered = False
    if triage_result.get("escalation_needed"):
        alerts.send_alert(
            incident_id=incident_id,
            triage_result=triage_result,
            location=loc,
            similar_id=sim_result.get("exact_match_id"),
        )
        escalation_triggered = True

    triage_result["recommended_actions"] = triage.enrich_recommended_actions(triage_result, lang)
    response_text = _build_triage_response(triage_result, sim_result, loc, incident_id)
    response_text = guardrails.sanitize_response(response_text)

    db.save_chat_message(session_id, "user", f"[Image uploaded: {image.filename}] {context}")
    db.save_chat_message(session_id, "assistant", response_text)

    triage_data = None
    if not triage_result.get("is_fallback"):
        triage_data = {
            "severity": triage_result["severity"],
            "severity_score": triage_result["severity_score"],
            "confidence": triage_result["confidence"],
            "indicators": triage_result["indicators"],
            "recommended_actions": triage_result["recommended_actions"],
            "escalation_needed": triage_result.get("escalation_needed", False),
            "triage_summary": triage_result["triage_summary"],
        }

    return ChatResponse(
        response=response_text,
        incident_id=incident_id,
        triage=triage_data,
        similarity={
            "is_exact_duplicate": sim_result["is_exact_duplicate"],
            "exact_match_id": sim_result.get("exact_match_id"),
            "similar_incidents": sim_result.get("similar_incidents", []),
            "message": sim_result.get("message", ""),
        },
        escalation_triggered=escalation_triggered,
        in_jurisdiction=True,
        resource_links=_build_resource_links(loc),
        location_verification=location_verification,
    )


@app.post("/v1/chat/query")
async def chat_query(http_request: Request, request: ChatQueryRequest):
    """UC-2: Text rescue question handling."""
    session_id = request.session_id or str(uuid.uuid4())

    # Guardrail check
    guard = guardrails.check_input(request.message)
    if not guard.allowed:
        return ChatResponse(response=guard.reason)

    # Get chat history for context
    history = db.get_chat_history(session_id)

    lang = _detect_language(http_request.headers.get("accept-language", ""), request.message)

    # Generate response
    response_text = triage.generate_chat_response(request.message, history, session_id, lang)
    resource_links = []
    location_context = _request_location_dict(request)
    if _query_needs_local_services(request.message):
        resource_links = _build_resource_links(location_context)
        if not resource_links:
            response_text += (
                "\n\nShare your location in the app and I can give you one-tap Google Maps links "
                "for nearby vets and animal help."
            )
    response_text = guardrails.sanitize_response(response_text)

    # Persist
    db.save_chat_message(session_id, "user", request.message)
    db.save_chat_message(session_id, "assistant", response_text)

    return ChatResponse(response=response_text, resource_links=resource_links)


@app.post("/v1/location/update")
async def update_location(request: LocationUpdateRequest):
    """Update location for an existing incident."""
    incident = db.get_incident(request.incident_id)
    if not incident:
        raise HTTPException(404, "Incident not found")

    lat, lng = location.truncate_precision(request.lat, request.lng)
    in_region = location.is_in_dharamsala_region(lat, lng)

    db.update_incident(
        request.incident_id,
        lat=lat,
        lng=lng,
        location_source=request.source.value,
    )

    # Re-evaluate escalation only if the location is within jurisdiction
    if in_region and incident["triage_severity"] in ("high", "critical") and incident["status"] == "new":
        triage_result = {
            "severity": incident["triage_severity"],
            "severity_score": incident["triage_severity_score"],
            "confidence": incident["triage_confidence"],
            "indicators": json.loads(incident["distress_flags"] or "[]"),
        }
        loc = {"lat": lat, "lng": lng, "source": request.source.value}
        alerts.send_alert(request.incident_id, triage_result, loc)

    return {"status": "updated", "incident_id": request.incident_id, "in_jurisdiction": in_region}


@app.post("/v1/triage/confirm")
async def triage_confirm(
    request: Request,
    pending_token: str = Form(...),
    session_id: str = Form(""),
):
    """Confirm jurisdiction for a pending triage when strict location gating is disabled."""
    if config.STRICT_LOCATION_GATE:
        raise HTTPException(
            410,
            "Location self-confirmation is disabled. Share browser location or upload a GPS-tagged photo.",
        )

    pending = _pending_triage.get(pending_token)
    if not pending:
        raise HTTPException(404, "Pending triage token not found or already confirmed")

    session_id = session_id or pending["session_id"]

    lang = _detect_language(request.headers.get("accept-language", ""), pending.get("context", ""))
    image_bytes = Path(pending["image_blob_path"]).read_bytes()
    triage_result = triage.analyze_image(image_bytes, pending["media_type"], pending["context"], lang)
    sim_result = similarity.run_similarity_checks(image_bytes, pending["image_sha256"], pending["image_phash"])

    loc = None
    if pending["lat"] is not None and pending["lng"] is not None:
        loc = {"lat": pending["lat"], "lng": pending["lng"], "source": pending["location_source"]}

    incident_id = db.create_incident(
        session_id=session_id,
        image_blob_path=pending["image_blob_path"],
        image_sha256=pending["image_sha256"],
        image_phash=pending["image_phash"],
        lat=pending["lat"],
        lng=pending["lng"],
        location_source=pending["location_source"],
        triage_severity=triage_result["severity"],
        triage_severity_score=triage_result["severity_score"],
        triage_confidence=triage_result["confidence"],
        triage_summary=triage_result["triage_summary"],
        distress_flags=triage_result["indicators"],
        similar_incident_id=sim_result.get("exact_match_id") or (sim_result["similar_incidents"][0]["incident_id"] if sim_result["similar_incidents"] else None),
        similarity_score=sim_result["similar_incidents"][0]["score"] if sim_result["similar_incidents"] else None,
        status="new",
    )

    db.create_triage_event(
        incident_id=incident_id,
        model_version=triage_result.get("model_version", "unknown"),
        raw_output=triage_result.get("raw_output", ""),
        postprocessed=json.dumps({
            "severity": triage_result["severity"],
            "severity_score": triage_result["severity_score"],
            "confidence": triage_result["confidence"],
            "indicators": triage_result["indicators"],
        }),
        latency_ms=triage_result.get("latency_ms", 0),
    )

    escalation_triggered = False
    if triage_result.get("escalation_needed"):
        alerts.send_alert(
            incident_id=incident_id,
            triage_result=triage_result,
            location=loc,
            similar_id=sim_result.get("exact_match_id"),
        )
        escalation_triggered = True

    del _pending_triage[pending_token]

    triage_result["recommended_actions"] = triage.enrich_recommended_actions(triage_result, lang)
    response_text = _build_triage_response(triage_result, sim_result, loc, incident_id)
    response_text = guardrails.sanitize_response(response_text)
    db.save_chat_message(session_id, "assistant", response_text)

    triage_data = None
    if not triage_result.get("is_fallback"):
        triage_data = {
            "severity": triage_result["severity"],
            "severity_score": triage_result["severity_score"],
            "confidence": triage_result["confidence"],
            "indicators": triage_result["indicators"],
            "recommended_actions": triage_result["recommended_actions"],
            "escalation_needed": triage_result.get("escalation_needed", False),
            "triage_summary": triage_result["triage_summary"],
        }

    return ChatResponse(
        response=response_text,
        incident_id=incident_id,
        triage=triage_data,
        similarity={
            "is_exact_duplicate": sim_result["is_exact_duplicate"],
            "exact_match_id": sim_result.get("exact_match_id"),
            "similar_incidents": sim_result.get("similar_incidents", []),
            "message": sim_result.get("message", ""),
        },
        escalation_triggered=escalation_triggered,
        in_jurisdiction=True,
        resource_links=_build_resource_links(loc),
    )


@app.get("/v1/incidents/{incident_id}")
async def get_incident(incident_id: str):
    """Retrieve incident details."""
    incident = db.get_incident(incident_id)
    if not incident:
        raise HTTPException(404, "Incident not found")
    # Convert to serializable dict
    result = dict(incident)
    if result.get("distress_flags"):
        try:
            result["distress_flags"] = json.loads(result["distress_flags"])
        except (json.JSONDecodeError, TypeError):
            pass
    return result


# --- Admin APIs ---

@app.post("/v1/admin/query")
async def admin_query(request: AdminQueryRequest):
    """UC-5: Admin natural-language analytics."""
    _require_admin_password(request.admin_password)

    result = admin_analytics.process_nl_query(request.query, admin_user="admin")
    return AdminQueryResponse(**result)


@app.get("/v1/admin/incidents")
async def admin_list_incidents(
    limit: int = Query(50, le=200),
    status: str = Query(None),
    severity: str = Query(None),
    admin_password: str = Query(...),
):
    """List incidents with optional filters."""
    _require_admin_password(admin_password)
    incidents = db.get_incidents_list(limit=limit, status=status, severity=severity)
    for inc in incidents:
        if inc.get("distress_flags"):
            try:
                inc["distress_flags"] = json.loads(inc["distress_flags"])
            except (json.JSONDecodeError, TypeError):
                pass
    return {"incidents": incidents, "count": len(incidents)}


@app.get("/v1/admin/alerts")
async def admin_list_alerts(
    limit: int = Query(50, le=200),
    admin_password: str = Query(...),
):
    """List alerts."""
    _require_admin_password(admin_password)
    alert_list = db.get_alerts_list(limit=limit)
    return {"alerts": alert_list, "count": len(alert_list)}


@app.post("/v1/admin/incidents/{incident_id}/status")
async def update_incident_status(incident_id: str, request: IncidentStatusUpdate):
    """Update incident status."""
    _require_admin_password(request.admin_password)
    incident = db.get_incident(incident_id)
    if not incident:
        raise HTTPException(404, "Incident not found")
    db.update_incident(incident_id, status=request.status.value)
    return {"status": "updated", "incident_id": incident_id, "new_status": request.status.value}


# --- Integration APIs ---

@app.post("/v1/integrations/slack/alert")
async def trigger_slack_alert(incident_id: str = Form(...)):
    """Manually trigger a Slack alert for an incident."""
    incident = db.get_incident(incident_id)
    if not incident:
        raise HTTPException(404, "Incident not found")
    triage_result = {
        "severity": incident["triage_severity"],
        "severity_score": incident["triage_severity_score"],
        "confidence": incident["triage_confidence"],
        "indicators": json.loads(incident["distress_flags"] or "[]"),
    }
    loc = None
    if incident["lat"] and incident["lng"]:
        loc = {"lat": incident["lat"], "lng": incident["lng"], "source": incident["location_source"]}
    alert_id = alerts.send_alert(incident_id, triage_result, loc)
    return {"alert_id": alert_id, "status": "sent"}


@app.post("/v1/integrations/events")
async def integration_event(event: dict):
    """Generic integration event endpoint."""
    logger.info("Integration event received: %s", json.dumps(event)[:500])
    return {"status": "received"}


@app.post("/v1/integrations/twilio/whatsapp", response_class=Response)
async def twilio_whatsapp_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receive an inbound Twilio WhatsApp message and return a TwiML reply."""
    form = await request.form()
    params = {str(key): str(value) for key, value in form.multi_items()}
    request_url = twilio_whatsapp.public_request_url(
        str(request.url),
        request.url.path,
        request.url.query,
    )
    signature = request.headers.get("x-twilio-signature", "")
    if not twilio_whatsapp.validate_webhook(request_url, params, signature):
        logger.warning("Rejected invalid Twilio WhatsApp signature for %s", request_url)
        raise HTTPException(403, "Invalid Twilio signature")

    sender = params.get("From", "")
    recipient = params.get("To", "")
    session_id = twilio_whatsapp.session_id_for_sender(sender or "unknown")
    message_sid = params.get("MessageSid", "")
    body = params.get("Body", "").strip()
    lat = _optional_float(params.get("Latitude"))
    lng = _optional_float(params.get("Longitude"))

    if lat is not None and lng is not None:
        db.save_session_location(session_id, lat, lng, "whatsapp")
    else:
        saved_location = db.get_session_location(session_id)
        if saved_location:
            lat = saved_location["lat"]
            lng = saved_location["lng"]

    num_media = _optional_int(params.get("NumMedia"))
    logger.info(
        "twilio_whatsapp_inbound session=%s message_sid=%s body_chars=%d media=%d location=%s",
        session_id,
        message_sid,
        len(body),
        num_media,
        lat is not None and lng is not None,
    )

    try:
        if num_media:
            background_tasks.add_task(
                _process_twilio_whatsapp_media_background,
                params=params,
                body=body,
                session_id=session_id,
                message_sid=message_sid,
                lat=lat,
                lng=lng,
                sender=sender,
                recipient=recipient,
                accept_language=request.headers.get("accept-language", ""),
            )
            response_text = (
                "Photo received. I am assessing it now and will send the result here shortly."
            )
        elif params.get("Latitude") and params.get("Longitude") and not body:
            response_text = _build_whatsapp_location_received_response(lat, lng)
        elif body:
            chat_response = await chat_query(
                request,
                ChatQueryRequest(
                    message=body,
                    session_id=session_id,
                    lat=lat,
                    lng=lng,
                    location_source="whatsapp" if lat is not None and lng is not None else None,
                ),
            )
            response_text = _with_whatsapp_resource_links(chat_response)
        else:
            response_text = (
                "Message received. Send a rescue question, share a WhatsApp location pin, "
                "or send a dog photo after sharing your location."
            )
    except Exception as exc:  # noqa: BLE001 - always return valid TwiML to Twilio
        logger.exception("Twilio WhatsApp message handling failed: %s", exc)
        response_text = (
            "Sorry, I could not process that WhatsApp message. Please try again. "
            "For a photo report, share a WhatsApp location pin first and then send the photo."
        )

    return Response(
        content=twilio_whatsapp.build_twiml(response_text),
        media_type="application/xml",
    )


async def _process_twilio_whatsapp_media_background(
    *,
    params: dict[str, str],
    body: str,
    session_id: str,
    message_sid: str,
    lat: float | None,
    lng: float | None,
    sender: str,
    recipient: str,
    accept_language: str,
) -> None:
    try:
        header_only_request = SimpleNamespace(
            headers=Headers({"accept-language": accept_language})
        )
        response_text = await _handle_twilio_whatsapp_media(
            request=header_only_request,
            params=params,
            body=body,
            session_id=session_id,
            message_sid=message_sid,
            lat=lat,
            lng=lng,
        )
    except Exception as exc:  # noqa: BLE001 - send a WhatsApp failure note instead of going silent
        logger.exception("Background WhatsApp media processing failed: %s", exc)
        response_text = (
            "Sorry, I could not process that WhatsApp photo. Please try sending it again, "
            "or describe what you see so I can still help."
        )

    try:
        outbound_sid = twilio_whatsapp.send_whatsapp_message(
            to=sender,
            from_=recipient,
            text=response_text,
        )
        logger.info(
            "twilio_whatsapp_background_reply session=%s inbound_sid=%s outbound_sid=%s",
            session_id,
            message_sid,
            outbound_sid,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to send background WhatsApp media reply: %s", exc)


# --- Health ---

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "1.0.0-prototype",
        "ai_configured": bool(config.OPENAI_API_KEY),
        "strict_location_gate": config.STRICT_LOCATION_GATE,
        "service_area_radius_km": config.DHARAMSALA_REGION_RADIUS_KM,
        "max_image_size_mb": config.MAX_IMAGE_SIZE_MB,
        "heic_supported": image_processing.heif_support_available(),
        "twilio_whatsapp_configured": bool(config.TWILIO_ACCOUNT_SID and config.TWILIO_AUTH_TOKEN),
        "whatsapp_demo_location_fallback": config.WHATSAPP_DEMO_LOCATION_FALLBACK,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# --- Helpers ---

def _build_triage_response(triage_result: dict, sim_result: dict, loc: dict | None, incident_id: str | None = None) -> str:
    """Build a youth-friendly response combining triage, similarity, and case info."""
    parts = []
    is_fallback = triage_result.get("is_fallback", False)

    if is_fallback:
        # Minimal response when AI assessment is unavailable
        parts.append(triage_result["triage_summary"])

        if triage_result["recommended_actions"]:
            parts.append("\n**Recommended next steps:**")
            for i, action in enumerate(triage_result["recommended_actions"][:5], 1):
                parts.append(f"{i}. {action}")

        # Similarity info still relevant
        if sim_result.get("message"):
            parts.append(f"\n*{sim_result['message']}*")

        return "\n".join(parts)

    severity = triage_result["severity"]
    score = triage_result["severity_score"]

    # Severity header
    if severity == "critical":
        parts.append("**URGENT - Immediate Help Needed**")
    elif severity == "high":
        parts.append("**High Priority - This Animal Needs Help Soon**")
    elif severity == "moderate":
        parts.append("**Moderate Concern Detected**")
    else:
        parts.append("**Assessment Complete**")

    # Triage summary
    parts.append(f"\n{triage_result['triage_summary']}")

    # Indicators
    if triage_result["indicators"]:
        parts.append("\n**What we noticed:**")
        for indicator in triage_result["indicators"][:5]:
            parts.append(f"- {indicator}")

    # Recommended actions
    if triage_result["recommended_actions"]:
        parts.append("\n**Recommended next steps:**")
        for i, action in enumerate(triage_result["recommended_actions"][:5], 1):
            parts.append(f"{i}. {action}")

    # Similarity info
    if sim_result.get("message"):
        parts.append(f"\n*{sim_result['message']}*")

    # Location prompt
    if not loc:
        parts.append("\n**Can you share the location?** This helps us avoid duplicate reports and suggest nearby vets or animal help. You can share your location using the button below or describe the nearest landmark.")
    else:
        parts.append("\n**Nearby help:** I've added quick Google Maps links below in case you want to check for nearby vets or animal rescue support.")

    # Escalation note
    if triage_result.get("escalation_needed"):
        parts.append("\n*This report has been flagged as urgent and sent to our rescue coordination team.*")

    # Confidence note
    confidence = triage_result["confidence"]
    if confidence < 0.5:
        parts.append(f"\n*Note: Our assessment confidence is {confidence:.0%}. Please provide additional details or a clearer photo if possible.*")

    # Case reference
    if incident_id:
        parts.append(f"\n*Case reference: {incident_id[:8].upper()}*")

    return "\n".join(parts)


def _require_admin_password(candidate: str) -> None:
    if not config.ADMIN_PASSWORD:
        raise HTTPException(503, "Admin access is not configured")
    if candidate != config.ADMIN_PASSWORD:
        raise HTTPException(403, "Invalid admin credentials")


async def _handle_twilio_whatsapp_media(
    *,
    request: Request,
    params: dict[str, str],
    body: str,
    session_id: str,
    message_sid: str,
    lat: float | None,
    lng: float | None,
) -> str:
    for index in range(_optional_int(params.get("NumMedia"))):
        content_type = params.get(f"MediaContentType{index}", "").split(";", 1)[0].lower()
        media_url = params.get(f"MediaUrl{index}", "")
        if not content_type.startswith("image/") or not media_url:
            continue

        image_bytes, media_type, filename = twilio_whatsapp.download_image_media(
            media_url,
            content_type,
            message_sid,
        )
        upload = UploadFile(
            file=io.BytesIO(image_bytes),
            filename=filename,
            size=len(image_bytes),
            headers=Headers({"content-type": media_type}),
        )
        media_lat, media_lng, media_source = _resolve_whatsapp_media_location(lat, lng)
        if media_source == "whatsapp_demo":
            logger.info(
                "whatsapp_demo_location_fallback session=%s message_sid=%s lat=%.4f lng=%.4f",
                session_id,
                message_sid,
                media_lat,
                media_lng,
            )
        triage_response = await triage_image(
            request=request,
            image=upload,
            context=body,
            session_id=session_id,
            lat=media_lat,
            lng=media_lng,
            location_source=media_source,
        )
        return _with_whatsapp_resource_links(triage_response)

    return "Only image attachments are supported. Please send a JPEG, PNG, WebP, GIF, HEIC, or HEIF photo."


def _resolve_whatsapp_media_location(
    lat: float | None,
    lng: float | None,
) -> tuple[float | None, float | None, str]:
    if lat is not None and lng is not None:
        return lat, lng, "whatsapp"
    if config.WHATSAPP_DEMO_LOCATION_FALLBACK:
        return config.WHATSAPP_DEMO_LAT, config.WHATSAPP_DEMO_LNG, "whatsapp_demo"
    return None, None, ""


def _build_whatsapp_location_received_response(lat: float | None, lng: float | None) -> str:
    if lat is None or lng is None:
        return "I could not read that location. Please share a WhatsApp location pin again."
    details = location.build_jurisdiction_details(lat, lng, "whatsapp")
    if details["in_jurisdiction"]:
        return (
            f"Location received and saved for this WhatsApp chat. It is {details['distance_km']:.1f} km "
            "from the Dharamsala service-area center and is currently accepted. You can now send a dog "
            "photo or ask a rescue question."
        )
    return (
        f"Location received, but it is {details['distance_km']:.1f} km from the Dharamsala service-area "
        f"center, outside the configured {details['allowed_radius_km']:.1f} km radius."
    )


def _with_whatsapp_resource_links(response: ChatResponse) -> str:
    links_text = _format_resource_links_for_whatsapp(response.resource_links)
    if not links_text:
        return response.response
    return f"{response.response}\n\n{links_text}"


def _format_resource_links_for_whatsapp(resource_links: list) -> str:
    lines = []
    seen_urls: set[str] = set()
    for link in resource_links or []:
        label = getattr(link, "label", None)
        url = getattr(link, "url", None)
        if isinstance(link, dict):
            label = label or link.get("label")
            url = url or link.get("url")
        if not label or not url or url in seen_urls:
            continue
        seen_urls.add(url)
        lines.append(f"- {label}: {url}")
    if not lines:
        return ""
    return "Helpful links:\n" + "\n".join(lines)


def _optional_float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _optional_int(value: str | None) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _resolve_upload_location(
    image_bytes: bytes,
    lat: float | None,
    lng: float | None,
    location_source: str,
) -> tuple[dict | None, float | None, float | None, str]:
    """Accept either in-region EXIF GPS or an in-region client location.

    EXIF remains preferred when it is in the service area. If EXIF is outside,
    an in-region browser location is a valid fallback because the reporter is
    physically within DAR's service area.
    """
    candidates: list[dict] = []
    exif_loc = location.extract_exif_location(image_bytes)
    if exif_loc:
        candidates.append(
            location.build_jurisdiction_details(exif_loc["lat"], exif_loc["lng"], "exif")
        )
    if lat is not None and lng is not None:
        source = location_source or "manual"
        candidates.append(location.build_jurisdiction_details(lat, lng, source))
    if not candidates:
        return None, None, None, "unknown"

    selected = next(
        (candidate for candidate in candidates if candidate["in_jurisdiction"]),
        candidates[0],
    )
    selected_source = selected["source"]
    if selected["in_jurisdiction"] and selected_source == "exif":
        resolution_reason = "accepted_in_region_exif"
    elif selected["in_jurisdiction"] and any(c["source"] == "exif" for c in candidates):
        resolution_reason = "accepted_reporter_location_fallback_after_outside_exif"
    elif selected["in_jurisdiction"]:
        resolution_reason = "accepted_in_region_reporter_location"
    else:
        resolution_reason = "rejected_all_verified_locations_outside"

    audit_candidates = [
        {**candidate, "selected": candidate is selected}
        for candidate in candidates
    ]
    resolved = {
        **selected,
        "decision": "accepted" if selected["in_jurisdiction"] else "rejected",
        "resolution_reason": resolution_reason,
        "candidates": audit_candidates,
    }
    return resolved, selected["lat"], selected["lng"], selected_source


def _missing_location_verification() -> dict:
    return {
        "source": "unknown",
        "in_jurisdiction": None,
        "decision": "rejected",
        "resolution_reason": "rejected_no_verified_location",
        "allowed_radius_km": location.DHARAMSALA_REGION_RADIUS_KM,
        "candidates": [],
    }


def _log_location_gate_decision(session_id: str, image_filename: str | None, verification: dict) -> None:
    payload = {
        "event": "location_gate_decision",
        "session_id": session_id,
        "image_filename": image_filename or "",
        "strict_location_gate": config.STRICT_LOCATION_GATE,
        **verification,
    }
    logger.info("location_gate_decision %s", json.dumps(payload, sort_keys=True))


def _defer_triage_for_location_confirmation(
    *,
    image_bytes: bytes,
    image: UploadFile,
    media_type: str,
    context: str,
    session_id: str,
) -> ChatResponse:
    sha256 = similarity.compute_sha256(image_bytes)
    phash = similarity.compute_phash(image_bytes)
    blob_filename = f"{sha256}_{image.filename}"
    blob_path = config.STORAGE_DIR / blob_filename
    blob_path.write_bytes(image_bytes)

    pending_token = str(uuid.uuid4())
    _pending_triage[pending_token] = {
        "session_id": session_id,
        "image_blob_path": str(blob_path),
        "image_sha256": sha256,
        "image_phash": phash,
        "media_type": media_type,
        "lat": None,
        "lng": None,
        "location_source": "unknown",
        "context": context,
        "image_filename": image.filename,
    }

    response_text = (
        "Thanks for reaching out to Dharamsala Animal Rescue!\n\n"
        "**Is this animal located in or near Dharamsala?** "
        "Please confirm so we can assess the photo and log this report with our rescue team."
    )
    db.save_chat_message(session_id, "user", f"[Image uploaded: {image.filename}] {context}")
    db.save_chat_message(session_id, "assistant", response_text)

    return ChatResponse(
        response=response_text,
        location_confirmed_needed=True,
        pending_token=pending_token,
    )


def _build_location_required_response() -> str:
    return (
        "**Location verification required**\n\n"
        "To assess and log an image report, Dharamsala Animal Rescue needs a verified location. "
        "Please either upload a photo that contains GPS metadata from the camera, or use the location "
        "button in the app and upload the photo again.\n\n"
        "Reports are accepted only when the verified photo location or shared browser location is within "
        "the Dharamsala service area."
    )


def _build_out_of_region_location_response(loc: dict | None) -> str:
    source = (loc or {}).get("source", "provided")
    details = ""
    if loc and loc.get("lat") is not None and loc.get("lng") is not None:
        coordinate_label = _format_coordinate_pair(loc["lat"], loc["lng"])
        details = (
            f"\n\nDetected coordinates: **{coordinate_label}** "
            f"from {source}. They are **{loc.get('distance_km', 0):.1f} km** from the "
            f"Dharamsala service-area center; the allowed radius is "
            f"**{loc.get('allowed_radius_km', location.DHARAMSALA_REGION_RADIUS_KM):.1f} km**."
        )
    return (
        "**Outside Dharamsala Animal Rescue's service area**\n\n"
        f"The verified {source} location for this image report is outside our Dharamsala service area, "
        "so this photo cannot be assessed or logged by Dharamsala Animal Rescue."
        f"{details}\n\n"
        "Please contact a local animal rescue organisation, SPCA/animal welfare society, municipal animal "
        "service, or nearby veterinary clinic in that area."
    )


def _format_coordinate_pair(lat: float, lng: float) -> str:
    lat_ref = "S" if lat < 0 else "N"
    lng_ref = "W" if lng < 0 else "E"
    return f"{abs(lat):.6f} {lat_ref}, {abs(lng):.6f} {lng_ref}"


def _build_out_of_region_response(triage_result: dict) -> str:
    """Build a response for cases whose coordinates fall outside the Dharamsala jurisdiction.

    Triage feedback is included so the user gets useful guidance, but no records are created.
    """
    parts = []
    severity = triage_result.get("severity", "unknown")
    is_fallback = triage_result.get("is_fallback", False)

    if not is_fallback:
        if severity == "critical":
            parts.append("**URGENT - This Animal Needs Immediate Help**")
        elif severity == "high":
            parts.append("**High Priority - This Animal Needs Help Soon**")
        elif severity == "moderate":
            parts.append("**Moderate Concern Detected**")
        else:
            parts.append("**Assessment Complete**")

        parts.append(f"\n{triage_result['triage_summary']}")

        if triage_result.get("recommended_actions"):
            parts.append("\n**Recommended next steps:**")
            for i, action in enumerate(triage_result["recommended_actions"][:5], 1):
                parts.append(f"{i}. {action}")

    parts.append(
        "\n---\n"
        "**Outside Dharamsala Animal Rescue's service area**\n\n"
        "Based on the location detected in your photo, this case appears to be outside our "
        "operational area. Dharamsala Animal Rescue only tracks cases within the Dharamsala region.\n\n"
        "Please contact a local animal rescue organisation or veterinary service in your area. "
        "You can reach out to:\n"
        "- Your nearest SPCA or animal welfare society\n"
        "- A local veterinary clinic\n"
        "- Local municipal animal control services"
    )

    return "\n".join(parts)


def _request_location_dict(request: ChatQueryRequest) -> dict | None:
    if request.lat is None or request.lng is None:
        return None
    return {
        "lat": request.lat,
        "lng": request.lng,
        "source": request.location_source.value if request.location_source else "browser",
    }


def _build_google_maps_links(loc: dict | None) -> list[dict]:
    if not loc:
        return []

    lat = loc.get("lat")
    lng = loc.get("lng")
    if lat is None or lng is None:
        return []

    nearby = f"{lat:.4f},{lng:.4f}"
    return [
        {
            "label": "Find nearby vets",
            "url": f"https://www.google.com/maps/search/?api=1&query={quote_plus(f'veterinarian near {nearby}')}",
        },
        {
            "label": "Find animal help nearby",
            "url": f"https://www.google.com/maps/search/?api=1&query={quote_plus(f'animal rescue NGO near {nearby}')}",
        },
    ]


def _build_resource_links(loc: dict | None) -> list[dict]:
    links: list[dict] = []
    if _location_is_in_dharamsala_service_area(loc):
        links.append(
            {
                "label": "Contact Dharamsala Animal Rescue",
                "url": config.DAR_CONTACT_URL,
            }
        )
    links.extend(_build_google_maps_links(loc))
    return links


def _location_is_in_dharamsala_service_area(loc: dict | None) -> bool:
    if not loc:
        return False
    if loc.get("in_jurisdiction") is True:
        return True
    lat = loc.get("lat")
    lng = loc.get("lng")
    if lat is None or lng is None:
        return False
    try:
        return location.is_in_dharamsala_region(float(lat), float(lng))
    except (TypeError, ValueError):
        return False


def _query_needs_local_services(message: str) -> bool:
    lower = message.lower()
    return any(
        phrase in lower
        for phrase in (
            "near me",
            "nearby",
            "google maps",
            "map",
            "vet",
            "veterinarian",
            "clinic",
            "ngo",
            "animal help",
            "animal hospital",
        )
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.HOST, port=config.PORT)
