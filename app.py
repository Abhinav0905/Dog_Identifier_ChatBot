"""
Dharamsala Animal Rescue Chatbot - Local Prototype
FastAPI application with all Phase 1 endpoints.
"""

import io
import uuid
import json
import logging
import re
import secrets
import unicodedata
import time
import asyncio
import threading
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
from datetime import datetime, timezone

from fastapi import BackgroundTasks, FastAPI, UploadFile, File, Form, HTTPException, Query, Request, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, Response
from starlette.datastructures import Headers
from starlette.concurrency import run_in_threadpool

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
    web_search,
    region_scope,
    query_router,
    conversation,
    ngo_followup,
    response_policy,
    request_limits,
    web_operations,
)

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("dharmasala")

_TRUSTED_SESSION_STATE_KEY = "_askdorjee_trusted_session_id"


app = FastAPI(
    title="Dharamsala Animal Rescue Chatbot",
    version="1.1.0-web-rc",
    description="Community-dog guidance and animal-care discovery across India",
)
app.add_middleware(request_limits.RequestLimitsMiddleware)
_delivery_retry_task = None

# Initialize database on startup
@app.on_event("startup")
def startup():
    db.init_db()
    request_limits.initialize()
    web_operations.initialize()
    if config.RAG_VECTOR_BACKEND == "chroma":
        from services import chroma_rag
        threading.Thread(target=chroma_rag.warm_up, name="rag-warmup", daemon=True).start()
    logger.info("Database initialized at %s", config.DB_PATH)
    logger.info("Storage directory: %s", config.STORAGE_DIR)
    logger.info("OpenAI API key configured: %s", bool(config.OPENAI_API_KEY))
    logger.info(
        "Dharamsala service-area mode: Deb route polygon + %.1f km checkpoint buffer",
        location.DHARAMSALA_REGION_RADIUS_KM,
    )


@app.on_event("startup")
async def start_web_maintenance():
    global _delivery_retry_task
    async def retry_loop():
        while True:
            try:
                await run_in_threadpool(web_operations.maintenance_tick)
            except Exception:
                logger.exception("Web maintenance failed")
            await asyncio.sleep(60)
    _delivery_retry_task = asyncio.create_task(retry_loop())


@app.on_event("shutdown")
async def stop_web_maintenance():
    global _delivery_retry_task
    if _delivery_retry_task:
        _delivery_retry_task.cancel()
        try:
            await _delivery_retry_task
        except asyncio.CancelledError:
            pass
        finally:
            _delivery_retry_task = None


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
async def serve_ui(request: Request):
    response = FileResponse(str(config.BASE_DIR / "static" / "index.html"))
    conversation.ensure_guest_token(request, response)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/admin.html", response_class=HTMLResponse)
async def serve_admin():
    return FileResponse(str(config.BASE_DIR / "static" / "admin.html"))


# --- Public APIs ---

@app.post("/v1/conversations")
async def create_conversation(request: Request, response: Response):
    created = conversation.create_guest_conversation(request, response)
    response.headers["Cache-Control"] = "no-store"
    return created


@app.get("/v1/conversations/{conversation_id}/messages")
async def get_conversation_messages(
    conversation_id: str,
    request: Request,
    response: Response,
):
    conversation.require_owned_conversation(
        request,
        response,
        conversation_id,
        touch=True,
    )
    history = db.get_chat_history(
        conversation_id,
        limit=config.CONVERSATION_UI_HISTORY_LIMIT,
    )
    response.headers["Cache-Control"] = "no-store"
    return {
        "conversation_id": conversation_id,
        "messages": history,
    }


@app.post("/v1/conversations/{conversation_id}/archive")
async def archive_conversation(
    conversation_id: str,
    request: Request,
    response: Response,
):
    conversation.archive_guest_conversation(request, response, conversation_id)
    response.headers["Cache-Control"] = "no-store"
    return {"status": "archived", "conversation_id": conversation_id}


def _resolve_request_session(
    request: Request,
    response: Response,
    requested_session_id: str | None,
) -> str:
    """Accept non-browser channel IDs only when a verified server route marks them."""
    requested = str(requested_session_id or "").strip()
    state = getattr(request, "state", None)
    trusted = str(getattr(state, _TRUSTED_SESSION_STATE_KEY, "") or "").strip()
    if requested and trusted and secrets.compare_digest(requested, trusted):
        return requested
    return conversation.resolve_message_session(request, response, requested)

@app.post("/v1/image/preview", response_class=Response)
async def image_preview(image: UploadFile = File(...)):
    """Return a browser-displayable JPEG preview for supported uploads."""
    image_bytes = await image.read(config.MAX_IMAGE_SIZE_MB * 1024 * 1024 + 1)
    if len(image_bytes) > config.MAX_IMAGE_SIZE_MB * 1024 * 1024:
        raise HTTPException(400, f"Image exceeds {config.MAX_IMAGE_SIZE_MB}MB limit")

    try:
        media_type = image_processing.validate_upload(image_bytes, image.content_type, image.filename)
        preview_bytes, preview_media_type = image_processing.prepare_preview(image_bytes, media_type)
    except image_processing.ImageProcessingError as exc:
        raise HTTPException(400, str(exc)) from exc

    return Response(
        content=preview_bytes,
        media_type=preview_media_type,
        headers={"Cache-Control": "no-store"},
    )


@app.post("/v1/triage/image")
async def triage_image(
    request: Request,
    response: Response,
    image: UploadFile = File(...),
    context: str = Form(""),
    session_id: str = Form(""),
    lat: float = Form(None),
    lng: float = Form(None),
    location_source: str = Form(""),
):
    """UC-1: Image-based distress assessment with jurisdiction check."""
    deadline = time.monotonic() + config.CHAT_TURN_TIMEOUT_SECONDS
    if len(context) > config.MAX_CHAT_MESSAGE_CHARS:
        raise HTTPException(413, "Photo description is too long")
    if (lat is None) != (lng is None) or (lat is not None and not web_search._coordinates_are_valid(lat, lng)):
        raise HTTPException(422, "Provide a valid latitude and longitude together")
    image_bytes = await image.read(config.MAX_IMAGE_SIZE_MB * 1024 * 1024 + 1)

    # Check file size
    if len(image_bytes) > config.MAX_IMAGE_SIZE_MB * 1024 * 1024:
        raise HTTPException(400, f"Image exceeds {config.MAX_IMAGE_SIZE_MB}MB limit")

    try:
        media_type = image_processing.validate_upload(image_bytes, image.content_type, image.filename)
    except image_processing.ImageProcessingError as exc:
        raise HTTPException(400, str(exc)) from exc

    session_id = _resolve_request_session(request, response, session_id)

    # Guardrail check on context text
    if context:
        guard = guardrails.check_input(context)
        if not guard.allowed:
            return ChatResponse(response=guard.reason)

    # Resolve location before vision processing. The India-only product scope
    # applies to every photo, including healthy-looking animals.
    loc, lat, lng, loc_source = await run_in_threadpool(
        _resolve_upload_location,
        image_bytes,
        lat,
        lng,
        location_source,
    )
    location_verification = loc or _missing_location_verification()
    _log_location_gate_decision(session_id, image.filename, location_verification)

    lang = _detect_language(request.headers.get("accept-language", ""), context)

    def resolve_caption():
        with region_scope.place_resolver.geocoding_budget(deadline):
            return region_scope.classify_text_scope(context)
    context_scope = await run_in_threadpool(resolve_caption) if context.strip() else None
    if config.INDIA_ONLY_SCOPE_ENABLED and context_scope and context_scope.is_outside_india:
        response_text = region_scope.INDIA_ONLY_RESPONSE
        db.save_chat_message(session_id, "user", f"[Image uploaded: {image.filename}] {context}")
        db.save_chat_message(session_id, "assistant", response_text)
        return ChatResponse(
            response=response_text,
            in_jurisdiction=False,
            location_verification=location_verification,
        )

    # A place name supports provider discovery, but a city centroid is not a
    # precise animal location for creating a local incident.
    has_precise_pin = lat is not None and lng is not None and loc_source not in {"named_place", "whatsapp_demo"}
    location_conflict = ""
    if context_scope and context_scope.scope == region_scope.INDIA and context_scope.lat is not None:
        if not has_precise_pin:
            lat, lng, loc_source = context_scope.lat, context_scope.lng, "named_place"
            loc = location.build_jurisdiction_details(lat, lng, loc_source)
            location_verification = loc
        elif (
            location.is_in_dharamsala_region(lat, lng) != context_scope.in_dharamsala
            or location.haversine_distance(lat, lng, context_scope.lat, context_scope.lng) > 50
        ):
            location_conflict = (
                "भेजे गए लोकेशन पिन और आपके बताए स्थान में अंतर है। कृपया पशु की सही जगह का पिन भेजें। कोई बचाव रिपोर्ट नहीं भेजी गई है।"
                if lang == "hi" else
                "The location pin and the place in your message disagree. Please share a pin for the animal's actual location. No rescue report has been submitted."
            )

    if config.INDIA_ONLY_SCOPE_ENABLED and lat is not None and not region_scope.upload_is_in_india(loc, lat, lng):
        response_text = region_scope.INDIA_ONLY_RESPONSE
        db.save_chat_message(session_id, "user", f"[Image uploaded: {image.filename}] {context}")
        db.save_chat_message(session_id, "assistant", response_text)
        return ChatResponse(
            response=response_text,
            in_jurisdiction=False,
            location_verification=location_verification,
        )

    if lat is not None and lng is not None and not location_conflict:
        db.save_session_case_location(session_id, {
            "scope": region_scope.INDIA if location.is_in_india(lat, lng) else region_scope.OUTSIDE_INDIA,
            "place": context_scope.place if context_scope and context_scope.scope == region_scope.INDIA else "",
            "lat": lat, "lng": lng, "country_code": "in" if location.is_in_india(lat, lng) else "",
            "source": loc_source, "in_dharamsala": location.is_in_dharamsala_region(lat, lng),
        })
    triage_result = await run_in_threadpool(
        triage.analyze_image,
        image_bytes,
        media_type,
        context,
        lang,
        deadline=deadline,
    )
    needs_rescue = triage.needs_rescue_help(triage_result)
    triage_data = _triage_response_payload(triage_result)

    if not needs_rescue or location_conflict:
        response_text = _build_image_assessment_response(
            triage_result,
            needs_rescue=False,
            in_region=None,
            language=lang,
        )
        if triage_result.get("is_fallback") or float(triage_result.get("confidence") or 0) < triage.VISUAL_REVIEW_CONFIDENCE_FLOOR:
            caption_guidance = triage.urgent_caption_guidance(context, language=lang)
            if caption_guidance:
                # The caption is direct reporter evidence even when pixels are
                # inconclusive. Preserve it without asserting a visual diagnosis.
                uncertainty = (
                    "फोटो से हालत की पुष्टि नहीं हो पाई; यह सलाह आपके बताए लक्षणों पर आधारित है।"
                    if lang == "hi" else
                    "The photo does not establish the condition; this guidance is based on the symptoms you described."
                )
                response_text = f"{caption_guidance}\n\n{uncertainty}"
        if location_conflict:
            response_text += f"\n\n{location_conflict}"
        response_text = guardrails.sanitize_text_response(response_text)
        db.save_chat_message(session_id, "user", f"[Image uploaded: {image.filename}] {context}")
        db.save_chat_message(session_id, "assistant", response_text)
        return ChatResponse(
            response=response_text,
            triage=triage_data,
            in_jurisdiction=None,
        )

    if lat is None or lng is None:
        response_text = _build_image_assessment_response(triage_result, needs_rescue=needs_rescue, in_region=None, language=lang)
        db.save_chat_message(session_id, "user", f"[Photo] {context}")
        db.save_chat_message(session_id, "assistant", response_text)
        return ChatResponse(response=response_text, triage=triage_data, location_verification=location_verification)

    in_region = location.is_in_dharamsala_region(lat, lng)
    if in_region and not has_precise_pin:
        response_text = _build_image_assessment_response(
            triage_result, needs_rescue=needs_rescue, in_region=True, language=lang,
        )
        response_text += (
            "\n\nस्थानीय रिपोर्ट दर्ज करने के लिए पशु की सही जगह का लोकेशन पिन भेजें। शहर का नाम अकेले पर्याप्त नहीं है; अभी कोई रिपोर्ट नहीं भेजी गई है।"
            if lang == "hi" else
            "\n\nPlease share a precise location pin for the animal before a local report can be recorded. The city name alone is not enough; no report has been submitted."
        )
        db.save_chat_message(session_id, "user", f"[Image uploaded: {image.filename}] {context}")
        db.save_chat_message(session_id, "assistant", response_text)
        return ChatResponse(response=response_text, triage=triage_data, in_jurisdiction=None, location_verification=location_verification)
    if not in_region:
        response_text = _build_out_of_region_response(triage_result, language=lang)
        search_result = await run_in_threadpool(
            web_search.search_animal_question,
            f"{context}\nFind suitable nearby veterinary treatment or animal assistance for this photo assessment: {triage_result.get('triage_summary', '')}",
            db.get_chat_history(session_id, limit=20),
            lat=lat,
            lng=lng,
            resolved_place=context_scope.place if context_scope else "",
            resolved_country_code="in",
            language=lang,
            deadline=deadline,
        )
        response_text = _append_local_help_search(response_text, search_result, language=lang)
        response_text = guardrails.sanitize_text_response(response_text)
        db.save_chat_message(session_id, "user", f"[Image uploaded: {image.filename}] {context}")
        db.save_chat_message(
            session_id,
            "assistant",
            response_text,
            metadata={"resource_links": search_result.resource_links},
        )
        return ChatResponse(
            response=response_text,
            triage=triage_data,
            in_jurisdiction=False,
            resource_links=search_result.resource_links,
        )

    # Incident creation and alerting stay internal. The user-facing response
    # intentionally does not expose a case ID or external links.
    sha256 = similarity.compute_sha256(image_bytes)
    phash = similarity.compute_phash(image_bytes)
    blob_filename = f"{sha256}_{Path(image.filename or 'photo').name}"
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
    escalation_status = "not_requested"
    if triage_result.get("escalation_needed"):
        delivery = await run_in_threadpool(
            alerts.send_alert,
            incident_id=incident_id,
            triage_result=triage_result,
            location=loc,
            similar_id=sim_result.get("exact_match_id"),
        )
        escalation_triggered = delivery.delivered
        escalation_status = delivery.status

    response_text = _build_image_assessment_response(
        triage_result,
        needs_rescue=True,
        in_region=True,
        language=lang,
    )
    response_text = guardrails.sanitize_text_response(response_text)
    if triage_result.get("escalation_needed") and not escalation_triggered:
        response_text += (
            "\n\nरिपोर्ट दर्ज हुई, लेकिन बचाव टीम को सूचना नहीं पहुँचाई जा सकी। कृपया पशु चिकित्सक या बचाव सेवा से सीधे संपर्क करें।"
            if lang == "hi" else
            "\n\nThe report was recorded, but no rescue notification was delivered. Please contact a veterinary or rescue service directly."
        )
    resource_links = _build_resource_links(loc)

    db.save_chat_message(session_id, "user", f"[Image uploaded: {image.filename}] {context}")
    db.save_chat_message(
        session_id,
        "assistant",
        response_text,
        metadata={"resource_links": resource_links},
    )

    return ChatResponse(
        response=response_text,
        triage=triage_data,
        escalation_triggered=escalation_triggered,
        escalation_status=escalation_status,
        in_jurisdiction=True,
        resource_links=resource_links,
    )


@app.post("/v1/chat/query")
def chat_query(
    http_request: Request,
    http_response: Response,
    request: ChatQueryRequest,
):
    """Model-directed conversation and open animal-help search, on a worker thread."""
    deadline = time.monotonic() + config.CHAT_TURN_TIMEOUT_SECONDS
    session_id = _resolve_request_session(http_request, http_response, request.session_id)
    guard = guardrails.check_input(request.message)
    if not guard.allowed:
        return ChatResponse(response=guard.reason)

    history = db.get_chat_history(session_id, limit=config.CONVERSATION_HISTORY_LIMIT)
    case_location = db.get_session_case_location(
        session_id, max_age_minutes=config.CASE_LOCATION_TTL_MINUTES,
    )
    language = _detect_language(http_request.headers.get("accept-language", ""), request.message)
    known_context = case_location
    if not known_context and request.lat is not None and request.lng is not None:
        known_context = {"lat": request.lat, "lng": request.lng}
    turn = query_router.plan_text_turn(
        request.message, history, case_location=known_context, language=language,
        deadline=deadline,
    )
    effective_phone_only = turn.phone_only
    if turn.routing_failed:
        from services.search_evidence import _number_only_request
        effective_phone_only = _number_only_request(request.message)
    if turn.new_case and turn.location_kind == "none":
        case_location = None
        # Keep the transcript, but do not let a new animal inherit an old case location.
        with db.get_db() as conn:
            conn.execute("DELETE FROM session_case_locations WHERE session_id = ?", (session_id,))
    if turn.action == query_router.TextAction.CLARIFY and case_location and case_location.get("scope") == region_scope.INDIA and re.search(
        r"\b(?:which|what|share|provide|tell)\b.{0,60}\b(?:city|location|state|place)\b", turn.clarification_question, re.I
    ):
        turn = replace(turn, action=query_router.TextAction.SEARCH, clarification_question="", local_help=True)
    with region_scope.place_resolver.geocoding_budget(deadline):
        scope = _text_turn_scope(turn, request, case_location, history)
    requested_location_text = _literal_case_location_text(turn, history, scope)
    if scope.explicit_location and scope.scope in {region_scope.INDIA, region_scope.OUTSIDE_INDIA, region_scope.AMBIGUOUS}:
        db.save_session_case_location(session_id, scope.as_case_location())
    elif turn.location_kind == region_scope.place_resolver.NAMED_PLACE:
        db.save_session_case_location(session_id, {"scope": scope.scope, "place": turn.location_text, "source": "current_unresolved_place"})

    # An old place must not gate a newly started general behavior conversation.
    outside_case = scope.is_outside_india and (turn.local_help or turn.needs_immediate_guidance)
    links = []
    searched = False
    result_kind = "care_answer"
    request_satisfied = None
    if config.INDIA_ONLY_SCOPE_ENABLED and outside_case:
        answer = region_scope.INDIA_ONLY_RESPONSE
        result_kind = "outside_india"
    elif (scope.needs_clarification and turn.local_help) or turn.action == query_router.TextAction.CLARIFY:
        answer = region_scope.location_clarification_response(scope, language=language) if scope.needs_clarification else turn.clarification_question
        if turn.needs_immediate_guidance:
            guidance = triage.immediate_safety_response(
                request.message, history, contextual_message=turn.contextual_request,
                language=language,
            )
            answer = _prepend_immediate_guidance(guidance or "", answer)
        result_kind = "clarification"
    elif turn.action == query_router.TextAction.SEARCH or turn.routing_failed:
        # An actual safety response remains available even when both models are
        # down. No old NGO directory, cache, or guessed place is a fallback.
        guidance = None
        try:
            result = web_search.search_animal_question(
                request.message,
                history,
                contextual_request=turn.contextual_request,
                resolved_place=scope.place if scope.scope in {
                    region_scope.INDIA, region_scope.OUTSIDE_INDIA,
                } else "",
                resolved_country_code=scope.country_code,
                lat=scope.lat,
                lng=scope.lng,
                language=language,
                tool_choice="auto" if turn.routing_failed else "required",
                requested_institution=None if turn.routing_failed else turn.requested_institution,
                requested_location_text=requested_location_text,
                phone_only=None if turn.routing_failed else turn.phone_only,
                deadline=deadline,
            )
        except Exception as exc:
            logger.warning("Open text search failed: %s", type(exc).__name__)
            result = web_search.SearchResult(
                response=web_search._animal_question_unavailable_response(language),
                result_kind="unavailable",
            )
        answer, links = result.response, result.resource_links
        searched, result_kind = result.searched, result.result_kind
        request_satisfied = getattr(result, "request_satisfied", None)
        if result_kind != "unavailable" and not effective_phone_only:
            # A successful lookup does not replace first aid for an explicitly
            # reported emergency. Ordinary discovery and feared future bites
            # do not acquire an emergency checklist.
            answer = _prepend_immediate_guidance(
                triage.urgent_caption_guidance(request.message, language) or "", answer,
            )
        # Only a real condition receives immediate guidance. A general
        # provider lookup must never acquire a welcome/checklist prefix.
        if result_kind == "unavailable" and (turn.needs_immediate_guidance or turn.routing_failed):
            answer = _prepend_immediate_guidance(
                triage.generate_chat_response(
                    request.message, history, session_id, language,
                    contextual_message=turn.contextual_request,
                    deadline=deadline,
                ), answer,
            )
    else:
        answer = triage.generate_chat_response(
            request.message, history, session_id, language,
            contextual_message=turn.contextual_request,
            deadline=deadline,
        )

    answer = guardrails.sanitize_text_response(answer)
    db.save_chat_message(session_id, "user", request.message)
    db.save_chat_message(session_id, "assistant", answer, metadata={
        "resource_links": links,
        "text_action": turn.action.value,
        "contextual_request": turn.contextual_request,
        "routing_source": turn.source,
        "awaiting_clarification": result_kind == "clarification",
        "web_search_ran": searched,
        "result_kind": result_kind,
        "request_satisfied": request_satisfied,
        "model_history_policy": triage.MODEL_HISTORY_INCLUDE,
        "requested_institution": turn.requested_institution,
        "requested_location_text": requested_location_text,
        "case_place": scope.place,
        "phone_only": effective_phone_only,
        "local_help": turn.local_help,
        "latency_ms": round((config.CHAT_TURN_TIMEOUT_SECONDS - max(0, deadline - time.monotonic())) * 1000),
    })
    logger.info(
        "text_turn session_id=%s action=%s routing_source=%s search_ran=%s "
        "result_kind=%s scope=%s place=%r",
        session_id, turn.action.value, turn.source, searched,
        result_kind, scope.scope, scope.place,
    )
    return ChatResponse(response=answer, resource_links=links, answer_format="phone_only" if effective_phone_only else "text")


def _literal_case_location_text(
    turn: query_router.TextTurn, history: list[dict], scope: region_scope.ScopeDecision,
) -> str:
    """Keep the user's selected place alongside its geocoder spelling.

    Follow-ups reuse structured turn metadata only for the same current case
    place. A new case or browser coordinates cannot inherit an old city label.
    """
    if turn.location_kind == region_scope.place_resolver.NAMED_PLACE:
        return turn.location_text
    if scope.source != "session_case" or not scope.place:
        return ""
    for row in reversed(history):
        metadata = row.get("metadata") or {}
        if row.get("role") != "assistant" or not isinstance(metadata, dict):
            continue
        if metadata.get("case_place") == scope.place and metadata.get("requested_location_text"):
            return str(metadata["requested_location_text"])
    return ""


def _text_turn_scope(
    turn: query_router.TextTurn,
    request: ChatQueryRequest,
    case_location: dict | None,
    history: list[dict] | None = None,
) -> region_scope.ScopeDecision:
    """Resolve explicit places only; missing geocodes do not prevent text search."""
    resolver = region_scope.place_resolver
    if turn.location_kind in {resolver.NAMED_PLACE, resolver.NEAR_ME} or (case_location and case_location.get("scope") == region_scope.AMBIGUOUS):
        try:
            return region_scope.classify_text_scope(
                request.message,
                lat=request.lat,
                lng=request.lng,
                history=history or [],
                case_location=case_location if case_location and (turn.location_kind == resolver.NEAR_ME or case_location.get("scope") == region_scope.AMBIGUOUS) else None,
                place_reference=resolver.PlaceReference(
                    turn.location_kind, turn.location_text, source="text_action_model",
                ),
            )
        except Exception as exc:
            logger.warning("Text location context unavailable: %s", type(exc).__name__)
            return region_scope.ScopeDecision(
                region_scope.UNSPECIFIED, place=turn.location_text,
                source="unresolved_text", reference_kind=turn.location_kind,
            )
    if case_location and case_location.get("scope") in {region_scope.INDIA, region_scope.OUTSIDE_INDIA, region_scope.AMBIGUOUS}:
        return region_scope.ScopeDecision(
            scope=case_location["scope"], place=str(case_location.get("place") or ""),
            country_code=str(case_location.get("country_code") or ""),
            city=str(case_location.get("city") or ""), region=str(case_location.get("region") or ""),
            lat=case_location.get("lat"), lng=case_location.get("lng"),
            source="session_case", reference_kind="session",
        )
    if web_search._coordinates_are_valid(request.lat, request.lng):
        # The action model may be unavailable before it can identify "near me".
        # Keep valid browser coordinates as context without asserting a country
        # or replacing an explicit place or the confirmed case handled above.
        return region_scope.ScopeDecision(
            region_scope.UNSPECIFIED,
            lat=float(request.lat), lng=float(request.lng),
            source="browser_context", reference_kind="browser",
        )
    return region_scope.ScopeDecision(region_scope.UNSPECIFIED, source="no_explicit_place")


@app.post("/v1/location/update")
async def update_location(request: LocationUpdateRequest, http_request: Request, response: Response):
    """Update location for an existing incident."""
    incident = db.get_incident(request.incident_id)
    if not incident:
        raise HTTPException(404, "Incident not found")
    _require_incident_access(http_request, response, incident)

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
        await run_in_threadpool(alerts.send_alert, request.incident_id, triage_result, loc)

    return {"status": "updated", "incident_id": request.incident_id, "in_jurisdiction": in_region}


@app.post("/v1/triage/confirm")
async def triage_confirm(
    request: Request,
    response: Response,
    pending_token: str = Form(...),
    session_id: str = Form(""),
):
    """Confirm jurisdiction for a pending triage when strict location gating is disabled."""
    raise HTTPException(410, "Self-confirmation is retired. Share the animal's city/state or location with a new photo; advice does not require submitting a report.")

@app.get("/v1/incidents/{incident_id}")
async def get_incident(incident_id: str, request: Request, response: Response):
    """Retrieve incident details."""
    incident = db.get_incident(incident_id)
    if not incident:
        raise HTTPException(404, "Incident not found")
    _require_incident_access(request, response, incident)
    # Convert to serializable dict
    result = dict(incident)
    result.pop("image_blob_path", None)
    result.pop("reporter_session_id", None)
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

    result = await run_in_threadpool(admin_analytics.process_nl_query, request.query, admin_user="admin")
    return AdminQueryResponse(**result)


@app.get("/v1/admin/incidents")
async def admin_list_incidents(
    limit: int = Query(50, ge=1, le=200),
    status: str = Query(None),
    severity: str = Query(None),
    admin_password: str = Header("", alias="X-Admin-Password"),
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
    limit: int = Query(50, ge=1, le=200),
    admin_password: str = Header("", alias="X-Admin-Password"),
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
async def trigger_slack_alert(incident_id: str = Form(...), admin_password: str = Header("", alias="X-Admin-Password")):
    """Manually trigger a Slack alert for an incident."""
    _require_admin_password(admin_password)
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
    delivery = await run_in_threadpool(alerts.send_alert, incident_id, triage_result, loc)
    return {"alert_id": delivery.alert_id, "status": delivery.status, "delivered": delivery.delivered}


@app.post("/v1/integrations/events")
async def integration_event(event: dict, admin_password: str = Header("", alias="X-Admin-Password")):
    """Generic integration event endpoint."""
    _require_admin_password(admin_password)
    logger.info("Authenticated integration event received")
    return {"status": "received"}


# Retired channel: deliberately not mounted in the web application.
# The implementation remains only for historical compatibility tests.
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

    sender = params.get("From", "").strip()
    recipient = params.get("To", "").strip()
    message_sid = params.get("MessageSid", "").strip()
    if not all(re.fullmatch(r"whatsapp:\+[1-9]\d{6,14}", address) for address in (sender, recipient)):
        raise HTTPException(400, "Valid WhatsApp sender and recipient are required")
    if not re.fullmatch(r"SM[A-Za-z0-9_-]{1,126}", message_sid):
        raise HTTPException(400, "A valid MessageSid is required")
    body = params.get("Body", "").strip()
    if len(body) > config.MAX_CHAT_MESSAGE_CHARS:
        raise HTTPException(413, "Message is too long")
    raw_lat, raw_lng = params.get("Latitude", ""), params.get("Longitude", "")
    lat = _optional_float(raw_lat)
    lng = _optional_float(raw_lng)
    if (raw_lat or raw_lng) and not web_search._coordinates_are_valid(lat, lng):
        raise HTTPException(422, "Provide a valid latitude and longitude together")
    session_id = twilio_whatsapp.session_id_for_sender(sender)
    if not await run_in_threadpool(request_limits.allowed, "whatsapp:" + session_id):
        raise HTTPException(429, "Too many messages. Please wait a minute.", headers={"Retry-After": "60"})
    reservation = db.reserve_whatsapp_message(message_sid, session_id)
    if not reservation["acquired"]:
        # A redelivered webhook must not generate another answer or external message.
        return Response(content="<Response/>", media_type="application/xml")
    lease_token = reservation["lease_token"]
    if reservation.get("outbound_sid"):
        db.finish_whatsapp_message(message_sid, lease_token, succeeded=True)
        return Response(content="<Response/>", media_type="application/xml")
    if reservation.get("reply_text"):
        # Resume only delivery. Replaying inference could duplicate history,
        # incidents and notifications after an otherwise successful assessment.
        background_tasks.add_task(
            _deliver_persisted_whatsapp_reply,
            message_sid=message_sid,
            lease_token=lease_token,
            response_text=reservation["reply_text"],
            sender=reservation.get("reply_to") or sender,
            recipient=reservation.get("reply_from") or recipient,
        )
        return Response(content="<Response/>", media_type="application/xml")

    if lat is not None and lng is not None:
        db.save_session_location(session_id, lat, lng, "whatsapp")
    else:
        saved_location = db.get_session_location(session_id, max_age_minutes=config.WHATSAPP_LOCATION_TTL_MINUTES)
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
                lease_token=lease_token,
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
            background_tasks.add_task(
                _process_twilio_whatsapp_text_background,
                body=body,
                session_id=session_id,
                message_sid=message_sid,
                lease_token=lease_token,
                lat=lat,
                lng=lng,
                sender=sender,
                recipient=recipient,
                accept_language=request.headers.get("accept-language", ""),
            )
            response_text = (
                "Message received. I am checking it now and will send the answer here shortly."
            )
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
        db.finish_whatsapp_message(message_sid, lease_token, succeeded=False, error_code=type(exc).__name__)

    if not num_media and (not body or (params.get("Latitude") and params.get("Longitude") and not body)):
        db.finish_whatsapp_message(message_sid, lease_token, succeeded=True, reply_text=response_text)

    return Response(
        content=twilio_whatsapp.build_twiml(response_text),
        media_type="application/xml",
    )


def _deliver_persisted_whatsapp_reply(
    *, message_sid: str, lease_token: str, response_text: str,
    sender: str, recipient: str,
) -> None:
    """Resume an existing reply without repeating the inbound processing."""
    try:
        if not db.save_whatsapp_reply(
            message_sid, lease_token, to=sender, from_=recipient, reply_text=response_text,
        ):
            return
        outbound_sid = twilio_whatsapp.send_whatsapp_message(
            to=sender, from_=recipient, text=response_text,
        )
        db.finish_whatsapp_message(
            message_sid, lease_token, succeeded=True, outbound_sid=outbound_sid,
        )
    except Exception as exc:
        db.finish_whatsapp_message(
            message_sid, lease_token, succeeded=False, error_code=type(exc).__name__,
        )
        logger.warning("Persisted WhatsApp reply delivery failed (%s)", type(exc).__name__)


def _process_twilio_whatsapp_text_background(
    *,
    body: str,
    session_id: str,
    message_sid: str,
    lease_token: str,
    lat: float | None,
    lng: float | None,
    sender: str,
    recipient: str,
    accept_language: str,
) -> None:
    try:
        header_only_request = SimpleNamespace(
            headers=Headers({"accept-language": accept_language}),
            state=SimpleNamespace(**{_TRUSTED_SESSION_STATE_KEY: session_id}),
        )
        chat_response = chat_query(
            header_only_request,
            Response(),
            ChatQueryRequest(
                message=body,
                session_id=session_id,
                lat=lat,
                lng=lng,
                location_source=(
                    "whatsapp" if lat is not None and lng is not None else None
                ),
            ),
        )
        response_text = _with_whatsapp_resource_links(chat_response)
    except Exception as exc:  # noqa: BLE001 - outbound failure note is safer than silence
        logger.exception("Background WhatsApp text processing failed: %s", exc)
        response_text = (
            "Sorry, I could not process that WhatsApp message. Please try again shortly."
        )

    try:
        if not db.save_whatsapp_reply(message_sid, lease_token, to=sender, from_=recipient, reply_text=response_text):
            return
        outbound_sid = twilio_whatsapp.send_whatsapp_message(
            to=sender,
            from_=recipient,
            text=response_text,
        )
        db.finish_whatsapp_message(message_sid, lease_token, succeeded=True, reply_text=response_text, outbound_sid=outbound_sid)
        logger.info(
            "twilio_whatsapp_background_text_reply session=%s inbound_sid=%s outbound_sid=%s",
            session_id,
            message_sid,
            outbound_sid,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to send background WhatsApp text reply: %s", exc)
        db.finish_whatsapp_message(message_sid, lease_token, succeeded=False, reply_text=response_text, error_code=type(exc).__name__)


async def _process_twilio_whatsapp_media_background(
    *,
    params: dict[str, str],
    body: str,
    session_id: str,
    message_sid: str,
    lease_token: str,
    lat: float | None,
    lng: float | None,
    sender: str,
    recipient: str,
    accept_language: str,
) -> None:
    try:
        header_only_request = SimpleNamespace(
            headers=Headers({"accept-language": accept_language}),
            state=SimpleNamespace(**{_TRUSTED_SESSION_STATE_KEY: session_id}),
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
        if not db.save_whatsapp_reply(message_sid, lease_token, to=sender, from_=recipient, reply_text=response_text):
            return
        outbound_sid = await run_in_threadpool(
            twilio_whatsapp.send_whatsapp_message,
            to=sender,
            from_=recipient,
            text=response_text,
        )
        db.finish_whatsapp_message(message_sid, lease_token, succeeded=True, reply_text=response_text, outbound_sid=outbound_sid)
        logger.info(
            "twilio_whatsapp_background_reply session=%s inbound_sid=%s outbound_sid=%s",
            session_id,
            message_sid,
            outbound_sid,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to send background WhatsApp media reply: %s", exc)
        db.finish_whatsapp_message(message_sid, lease_token, succeeded=False, reply_text=response_text, error_code=type(exc).__name__)


# --- Health ---

@app.get("/health")
def health():
    state = web_operations.readiness()
    return {
        **state,
        "version": "1.1.0-web-rc",
        "india_only_scope": config.INDIA_ONLY_SCOPE_ENABLED,
        "india_boundary_loaded": location.india_boundary_available(),
        "location_required_for_assessment": False,
        "location_required_for_local_report": True,
        "max_image_size_mb": config.MAX_IMAGE_SIZE_MB,
        "heic_supported": image_processing.heif_support_available(),
        "health_scope": "Local readiness and recent real requests; does not certify answers or rescue dispatch.",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/v1/admin/operations")
def admin_operations(admin_password: str = Header("", alias="X-Admin-Password")):
    _require_admin_password(admin_password)
    return {"readiness": web_operations.readiness(), "operations": web_operations.snapshot()}


@app.post("/v1/admin/alerts/{alert_id}/acknowledge")
def acknowledge_web_alert(alert_id: str, admin_password: str = Header("", alias="X-Admin-Password")):
    _require_admin_password(admin_password)
    if not alerts.acknowledge_alert(alert_id, actor="web-admin"):
        raise HTTPException(404, "Delivered alert not found")
    return {"alert_id": alert_id, "acknowledged": True, "dispatch_confirmed": False}


# --- Helpers ---

def _triage_response_payload(triage_result: dict) -> dict | None:
    """Return optional structured triage data for the UI badge."""
    if triage_result.get("is_fallback") or float(triage_result.get("confidence") or 0) < 0.65:
        return None
    severity = triage_result.get("severity")
    score = triage_result.get("severity_score")
    confidence = triage_result.get("confidence")
    if severity not in {"low", "moderate", "high", "critical"}:
        return None
    if score is None or confidence is None:
        return None
    return {
        "severity": severity,
        "severity_score": score,
        "confidence": confidence,
        "indicators": triage_result.get("indicators", []),
        "recommended_actions": triage_result.get("recommended_actions", []),
        "escalation_needed": triage_result.get("escalation_needed", False),
        "triage_summary": triage_result.get("triage_summary", ""),
    }


def _dar_phone_sentence() -> str:
    phone = config.DAR_PHONE_NUMBER
    if phone:
        return f"For DAR assistance, call its configured contact: {phone}. Availability and pickup must be confirmed directly."
    return f"For DAR assistance, use its [contact page]({config.DAR_CONTACT_URL}). Availability and pickup must be confirmed directly."


def _build_image_assessment_response(
    triage_result: dict,
    *,
    needs_rescue: bool,
    in_region: bool | None,
    language: str = "en",
) -> str:
    """Preserve the assessment's uncertainty and useful actions in every region."""
    if triage_result.get("is_fallback"):
        if language == "hi":
            return (
                "इस फोटो का आकलन नहीं हो पाया। कृपया बताएँ कि क्या हो रहा है। "
                "चोट, साँस लेने में कठिनाई या हिल न पाने पर तुरंत पशु चिकित्सक की सहायता लें।"
            )
        return (
            "I could not assess this photo. Please describe what is happening. "
            "If the animal is injured, struggling to breathe or unable to move, seek urgent veterinary help."
        )
    summary = str(triage_result.get("triage_summary") or "I cannot determine the animal's condition from this photo.").strip()
    uncertain = float(triage_result.get("confidence") or 0) < 0.65
    parts = [summary]
    if uncertain:
        parts.append(
            "इस आकलन में अनिश्चितता है; केवल फोटो से पशु के स्वस्थ होने की पुष्टि नहीं हो सकती। साफ फोटो और लक्षणों का विवरण मदद करेगा।"
            if language == "hi" else
            "This assessment is uncertain; a photo cannot establish that an animal is healthy. A clearer photo and a description of its symptoms would help."
        )
    actions = triage_result.get("recommended_actions") or []
    if actions:
        parts.append("\n".join(f"{i}. {guardrails.sanitize_text_response(str(action))}" for i, action in enumerate(actions[:5], 1)))
    if needs_rescue:
        if in_region is True:
            if language == "hi":
                contact = config.DAR_PHONE_NUMBER or f"[संपर्क पेज]({config.DAR_CONTACT_URL})"
                parts.append(f"DAR की सहायता के लिए {contact} पर संपर्क करें। उपलब्धता और पशु को लेने की सुविधा सीधे पूछकर सुनिश्चित करें।")
            else:
                parts.append(_dar_phone_sentence())
        elif in_region is False:
            parts.append(
                "देखभाल और परिवहन की ज़रूरत के अनुसार पशु चिकित्सक, पशु अस्पताल या कॉलेज, सरकारी पशु चिकित्सा सेवा या बचाव संस्था सहायता कर सकते हैं।"
                if language == "hi" else
                "Suitable help may include a veterinarian, veterinary hospital or college, public veterinary service, or rescue organisation, depending on the care and transport needed."
            )
        else:
            parts.append(
                "पशु किस शहर और राज्य में है? मैं उपयुक्त पशु चिकित्सा या बचाव सहायता खोज सकता हूँ। अभी कोई बचाव रिपोर्ट नहीं भेजी गई है।"
                if language == "hi" else
                "Which city and state is the animal in? I can look for suitable veterinary or rescue help. No rescue report has been submitted."
            )
    return "\n\n".join(parts)


def _build_triage_response(triage_result: dict, sim_result: dict, loc: dict | None, incident_id: str | None = None) -> str:
    """Compatibility wrapper for older call sites."""
    return _build_image_assessment_response(
        triage_result,
        needs_rescue=triage.needs_rescue_help(triage_result),
        in_region=bool(loc),
    )


def _require_admin_password(candidate: str) -> None:
    if not config.ADMIN_PASSWORD:
        raise HTTPException(503, "Admin access is not configured")
    if not secrets.compare_digest(str(candidate), config.ADMIN_PASSWORD):
        raise HTTPException(403, "Invalid admin credentials")


def _require_incident_access(request: Request, response: Response, incident: dict) -> None:
    candidate = request.headers.get("x-admin-password", "")
    if candidate:
        _require_admin_password(candidate)
        return
    conversation.require_owned_conversation(request, response, str(incident.get("reporter_session_id") or ""))


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

        image_bytes, media_type, filename = await run_in_threadpool(
            twilio_whatsapp.download_image_media,
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
            response=Response(),
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
    if response.answer_format == "phone_only":
        return response.response
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
        phone = getattr(link, "phone", None)
        if isinstance(link, dict):
            phone = phone or link.get("phone")
        phone_text = f" | Phone: {phone}" if phone else ""
        lines.append(f"- {label}: {url}{phone_text}")
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
    """Prefer the photo location; reporter location never overrides conflicting EXIF."""
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

    selected = candidates[0]  # Photo GPS must never be replaced just to enter DAR jurisdiction.
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
        "decision": "assessment_only",
        "resolution_reason": "no_verified_location_for_reporting",
        "allowed_radius_km": location.DHARAMSALA_REGION_RADIUS_KM,
        "service_area_match": None,
        "candidates": [],
    }


def _log_location_gate_decision(session_id: str, image_filename: str | None, verification: dict) -> None:
    payload = {
        "event": "location_gate_decision",
        "session_id": session_id,
        "image_filename": image_filename or "",
        "location_required_for_assessment": False,
        **verification,
    }
    logger.info("location_gate_decision %s", json.dumps(payload, sort_keys=True))


def _build_location_required_response(triage_result: dict | None = None) -> str:
    summary = ""
    if triage_result and not triage_result.get("is_fallback"):
        summary = f"{triage_result.get('triage_summary', '').strip()}\n\n"
    return (
        "**Location verification required**\n\n"
        f"{summary}"
        "This animal appears to need help, but I could not verify whether the photo is within the "
        "Dharamsala service area. Please upload a GPS-tagged photo or share your location in the app. "
        "If this is outside Dharamsala, contact a local animal rescue organisation, animal welfare NGO, or local nonprofit."
    )


def _build_out_of_region_location_response(loc: dict | None) -> str:
    return (
        "**Outside Dharamsala Animal Rescue's service area**\n\n"
        "The verified location is outside the Dharamsala service area. Please contact a local animal "
        "rescue organisation, animal welfare NGO, or local nonprofit in that area."
    )


def _format_coordinate_pair(lat: float, lng: float) -> str:
    lat_ref = "S" if lat < 0 else "N"
    lng_ref = "W" if lng < 0 else "E"
    return f"{abs(lat):.6f} {lat_ref}, {abs(lng):.6f} {lng_ref}"


def _build_out_of_region_response(triage_result: dict, language: str = "en") -> str:
    return _build_image_assessment_response(
        triage_result,
        needs_rescue=True,
        in_region=False,
        language=language,
    )


def _append_local_help_search(
    response_text: str,
    search_result: web_search.SearchResult,
    language: str = "en",
) -> str:
    heading = "**स्थानीय पशु सहायता:**" if language == "hi" else (
        "**Local animal help options:**" if search_result.searched else "**Local animal help:**"
    )
    return f"{response_text}\n\n{heading}\n\n{search_result.response}"


def _prepend_immediate_guidance(guidance: str, response_text: str) -> str:
    """Compose the dog answer first and local-contact result second."""
    first = str(guidance or "").strip()
    second = str(response_text or "").strip()
    if not first:
        return second
    if not second or second.startswith(first):
        return second or first
    return f"{first}\n\n{second}"


def _search_verified_local_help_safely(*args, **kwargs) -> web_search.SearchResult:
    """Keep unexpected NGO-provider failures from suppressing dog guidance."""
    try:
        return web_search.search_verified_india_local_help(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - endpoint must retain the safety answer
        logger.warning("Verified local-help lookup failed unexpectedly: %s", type(exc).__name__)
        return web_search.SearchResult(response=web_search.unavailable_response())


def _request_location_dict(request: ChatQueryRequest) -> dict | None:
    if request.lat is None or request.lng is None:
        return None
    return {
        "lat": request.lat,
        "lng": request.lng,
        "source": request.location_source.value if request.location_source else "browser",
    }


def _build_google_maps_links(loc: dict | None) -> list[dict]:
    return []


def _build_resource_links(loc: dict | None) -> list[dict]:
    if not _location_is_in_dharamsala_service_area(loc):
        return []
    return [
        {
            "label": "Dharamsala Animal Rescue contact page",
            "url": config.DAR_CONTACT_URL,
            "phone": config.DAR_PHONE_NUMBER,
        }
    ]


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


def _place_reference_from_query_analysis(
    analysis: query_router.QueryAnalysis,
) -> region_scope.place_resolver.PlaceReference | None:
    """Translate model-extracted text into resolver input, never a scope verdict."""
    resolver = region_scope.place_resolver
    if analysis.location_kind == query_router.LocationKind.NEAR_ME:
        return resolver.PlaceReference(resolver.NEAR_ME, source=analysis.source)
    if analysis.location_kind == query_router.LocationKind.AMBIGUOUS:
        return resolver.PlaceReference(
            resolver.AMBIGUOUS,
            analysis.place_query,
            source=analysis.source,
        )
    if analysis.location_kind != query_router.LocationKind.NAMED_PLACE:
        # The structured router already combines model extraction with a
        # deterministic cross-check.  Preserve its explicit "no location"
        # decision so a second extractor cannot reinterpret ordinary phrases
        # such as "at night" as a geographic place.
        return resolver.PlaceReference(resolver.NONE, source=analysis.source)

    components = resolver.PlaceComponents(
        street=analysis.street,
        area=analysis.locality,
        city=analysis.city,
        district=analysis.district,
        state=analysis.state,
        postal_code=analysis.postcode,
        country=analysis.country,
    )
    return resolver.PlaceReference(
        resolver.NAMED_PLACE,
        analysis.place_query,
        source=analysis.source,
        components=components,
    )


def _resolve_service_city(
    analysis: query_router.QueryAnalysis,
    scope_decision: region_scope.ScopeDecision,
) -> region_scope.place_resolver.PlaceResolution | None:
    """Resolve the NGO coverage city independently from a street/sub-area."""
    explicit_parent_city = _explicit_parent_city(analysis)
    city = (explicit_parent_city or scope_decision.city or analysis.city).strip()
    if not city:
        return None
    country = analysis.country.strip()
    if not country and scope_decision.scope == region_scope.INDIA:
        country = "India"
    resolution = region_scope.place_resolver.resolve_service_city(
        city,
        state=(scope_decision.region or analysis.state).strip(),
        district=analysis.district.strip(),
        country=country,
    )
    if (
        explicit_parent_city
        and resolution.scope == region_scope.INDIA
        and scope_decision.scope == region_scope.INDIA
        and not _service_city_is_plausible_for_incident(resolution, scope_decision)
    ):
        logger.info(
            "service_city_conflict requested=%r incident_place=%r incident_region=%r "
            "service_region=%r",
            explicit_parent_city,
            scope_decision.place,
            scope_decision.region,
            resolution.region,
        )
        return region_scope.place_resolver.PlaceResolution(
            region_scope.AMBIGUOUS,
            display_name=explicit_parent_city,
            source="explicit_city_conflicts_with_incident",
        )
    logger.info(
        "service_city_decision requested=%r scope=%s place=%r source=%s",
        city,
        resolution.scope,
        resolution.display_name,
        resolution.source,
    )
    return resolution


def _promote_verified_region_service_area(
    service_city: region_scope.place_resolver.PlaceResolution | None,
    scope_decision: region_scope.ScopeDecision,
) -> region_scope.place_resolver.PlaceResolution | None:
    """Allow verified Indian state/UT requests to use regional NGO fallback."""
    if (
        not service_city
        or service_city.scope != region_scope.AMBIGUOUS
        or scope_decision.scope != region_scope.INDIA
        or scope_decision.city
        or not scope_decision.region
        or scope_decision.lat is None
        or scope_decision.lng is None
    ):
        return service_city
    if service_city.source not in {
        "service_city_is_region",
        "service_city_not_verified",
        "service_city_region_not_verified",
    }:
        return service_city
    return region_scope.place_resolver.PlaceResolution(
        region_scope.INDIA,
        display_name=scope_decision.place or f"{scope_decision.region}, India",
        lat=scope_decision.lat,
        lng=scope_decision.lng,
        country_code=scope_decision.country_code or "in",
        city=scope_decision.region,
        region=scope_decision.region,
        source="verified_region_service_area",
    )


def _explicit_parent_city(analysis: query_router.QueryAnalysis) -> str:
    """Return a city explicitly supplied above a street/locality component."""
    city = analysis.city.strip()
    if not city:
        return ""
    return city if any(
        value.strip()
        for value in (
            analysis.street,
            analysis.locality,
            analysis.district,
            analysis.state,
            analysis.postcode,
        )
    ) else ""


def _normalise_geo_label(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", (value or "").casefold())
    without_marks = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9]+", " ", without_marks).strip()


def _service_city_is_plausible_for_incident(
    service_city: region_scope.place_resolver.PlaceResolution,
    incident: region_scope.ScopeDecision,
) -> bool:
    service_region = _normalise_geo_label(service_city.region)
    incident_region = _normalise_geo_label(incident.region)
    if service_region and incident_region and service_region != incident_region:
        return False
    if None in (service_city.lat, service_city.lng, incident.lat, incident.lng):
        # An explicit parent-city disagreement cannot be validated without both
        # coordinate pairs. Fail closed and ask the user to clarify.
        return False
    distance_km = location.haversine_distance(
        float(service_city.lat),
        float(service_city.lng),
        float(incident.lat),
        float(incident.lng),
    )
    return distance_km <= config.SERVICE_CITY_MAX_DISTANCE_KM


def _incident_is_in_dharamsala_service_area(
    analysis: query_router.QueryAnalysis,
    scope_decision: region_scope.ScopeDecision,
) -> bool:
    """Require a stated street/sub-area to survive geocoding before DAR routing."""
    if scope_decision.scope != region_scope.INDIA or not scope_decision.in_dharamsala:
        return False
    leaf = (analysis.street or analysis.locality).strip()
    if not leaf:
        return True
    if region_scope.place_resolver.is_dharamsala_alias(leaf):
        # A model may classify the sole city name as a locality. Canonical
        # spelling differences (Dharamsala/Dharamshala) must not bypass DAR.
        return True

    def normalise(value: str) -> list[str]:
        return [
            token
            for token in re.findall(r"[^\W_]+", value.casefold(), flags=re.UNICODE)
            if token
        ]

    place_tokens = normalise(scope_decision.place)
    leaf_tokens = normalise(leaf)
    width = len(leaf_tokens)
    return bool(
        width
        and any(
            place_tokens[index : index + width] == leaf_tokens
            for index in range(len(place_tokens) - width + 1)
        )
    )


def _query_needs_local_services(message: str) -> bool:
    return web_search.should_search(message)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.HOST, port=config.PORT)
