"""
Dharamsala Animal Rescue Chatbot - Local Prototype
FastAPI application with all Phase 1 endpoints.
"""

import uuid
import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import quote_plus

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse

import database as db
import config
from models import (
    ChatQueryRequest, LocationUpdateRequest, AdminQueryRequest,
    IncidentStatusUpdate, ChatResponse, AdminQueryResponse,
)
from services import guardrails, triage, similarity, location, alerts, admin_analytics

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("dharmasala")

# In-memory store for triage results awaiting jurisdiction confirmation.
# Keyed by token (UUID). Entries are removed after /v1/triage/confirm is called.
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
    location_confirmed: bool = Form(False),
):
    """UC-1: Image-based distress assessment with jurisdiction check."""
    # Validate file type
    if image.content_type not in config.ALLOWED_IMAGE_TYPES:
        raise HTTPException(400, f"Unsupported image type: {image.content_type}. Allowed: {', '.join(config.ALLOWED_IMAGE_TYPES)}")

    image_bytes = await image.read()

    # Check file size
    if len(image_bytes) > config.MAX_IMAGE_SIZE_MB * 1024 * 1024:
        raise HTTPException(400, f"Image exceeds {config.MAX_IMAGE_SIZE_MB}MB limit")

    if not session_id:
        session_id = str(uuid.uuid4())

    # Guardrail check on context text
    if context:
        guard = guardrails.check_input(context)
        if not guard.allowed:
            return ChatResponse(response=guard.reason)

    # Step 1: Resolve location — EXIF first, then form-provided coordinates
    loc = None
    loc_source = "unknown"
    if lat is not None and lng is not None:
        loc = {"lat": lat, "lng": lng, "source": location_source or "manual"}
        loc_source = location_source or "manual"
    else:
        exif_loc = location.extract_exif_location(image_bytes)
        if exif_loc:
            loc = exif_loc
            lat, lng = exif_loc["lat"], exif_loc["lng"]
            loc_source = "exif"

    # Step 2: Determine jurisdiction
    # - Coordinates known (EXIF or form): verify against the Dharamsala region radius.
    # - No coordinates and user confirmed Dharamsala: proceed without stored location.
    # - No coordinates and no confirmation: ask jurisdiction question first; defer
    #   assessment until the user confirms. Image saved to blob so no re-upload is needed.
    if lat is not None and lng is not None:
        in_region = location.is_in_dharamsala_region(lat, lng)
    elif location_confirmed:
        in_region = True
    else:
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
            "media_type": image.content_type,
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

    # Step 3: Run vision triage — jurisdiction is now confirmed or verifiable
    lang = _detect_language(request.headers.get("accept-language", ""), context)
    triage_result = triage.analyze_image(image_bytes, image.content_type, context, lang)

    # Steps 4–8: Incident creation and alerting only for in-jurisdiction cases
    if in_region:
        # Save image to blob storage
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
        )

    else:
        # Out of jurisdiction — provide triage feedback but create no records or alerts
        response_text = _build_out_of_region_response(triage_result)
        response_text = guardrails.sanitize_response(response_text)

        db.save_chat_message(session_id, "user", f"[Image uploaded: {image.filename}] {context}")
        db.save_chat_message(session_id, "assistant", response_text)

        return ChatResponse(
            response=response_text,
            in_jurisdiction=False,
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
    response_text = guardrails.sanitize_response(response_text)

    # Persist
    db.save_chat_message(session_id, "user", request.message)
    db.save_chat_message(session_id, "assistant", response_text)

    return ChatResponse(response=response_text)


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
    """Confirm jurisdiction for a pending triage and create the incident record.

    The client receives a pending_token from POST /v1/triage/image when no location
    could be determined. The user confirms the case is in Dharamsala; the client
    then calls this endpoint with just the token — no image re-upload required.
    """
    pending = _pending_triage.get(pending_token)
    if not pending:
        raise HTTPException(404, "Pending triage token not found or already confirmed")

    session_id = session_id or pending["session_id"]

    # Run assessment now that jurisdiction is confirmed
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
    
    # Step 8: Build user-friendly response
    response_text = _build_triage_response(triage_result, sim_result, loc)
    response_text = guardrails.sanitize_response(response_text)
    resource_links = _build_google_maps_links(loc)

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
        resource_links=resource_links,
    )


@app.post("/v1/chat/query")
async def chat_query(request: ChatQueryRequest):
    """UC-2: Text rescue question handling."""
    session_id = request.session_id or str(uuid.uuid4())

    # Guardrail check
    guard = guardrails.check_input(request.message)
    if not guard.allowed:
        return ChatResponse(response=guard.reason)

    # Get chat history for context
    history = db.get_chat_history(session_id)

    # Generate response
    response_text = triage.generate_chat_response(request.message, history, session_id)
    resource_links = []
    location_context = _request_location_dict(request)
    if _query_needs_local_services(request.message):
        resource_links = _build_google_maps_links(location_context)
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
    db.update_incident(
        request.incident_id,
        lat=lat,
        lng=lng,
        location_source=request.source.value,
    )

    # Re-evaluate escalation if severity was high but location was missing
    if incident["triage_severity"] in ("high", "critical") and incident["status"] == "new":
        triage_result = {
            "severity": incident["triage_severity"],
            "severity_score": incident["triage_severity_score"],
            "confidence": incident["triage_confidence"],
            "indicators": json.loads(incident["distress_flags"] or "[]"),
        }
        loc = {"lat": lat, "lng": lng, "source": request.source.value}
        alerts.send_alert(request.incident_id, triage_result, loc)

    return {"status": "updated", "incident_id": request.incident_id}


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
    if request.admin_password != config.ADMIN_PASSWORD:
        raise HTTPException(403, "Invalid admin credentials")

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
    if admin_password != config.ADMIN_PASSWORD:
        raise HTTPException(403, "Invalid admin credentials")
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
    if admin_password != config.ADMIN_PASSWORD:
        raise HTTPException(403, "Invalid admin credentials")
    alert_list = db.get_alerts_list(limit=limit)
    return {"alerts": alert_list, "count": len(alert_list)}


@app.post("/v1/admin/incidents/{incident_id}/status")
async def update_incident_status(incident_id: str, request: IncidentStatusUpdate):
    """Update incident status."""
    if request.admin_password != config.ADMIN_PASSWORD:
        raise HTTPException(403, "Invalid admin credentials")
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


# --- Health ---

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "1.0.0-prototype",
        "ai_configured": bool(config.OPENAI_API_KEY),
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
