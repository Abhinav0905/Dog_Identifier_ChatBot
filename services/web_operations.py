"""Web-only readiness, passive telemetry and explicit retention operations.

No model request, message send or user-data deletion occurs in readiness().
Maintenance retries existing alerts only; retention always requires apply=True.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import re

import config
import database as db

logger = logging.getLogger(__name__)


def initialize() -> None:
    with db.get_db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS web_service_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at TEXT NOT NULL,
            component TEXT NOT NULL, status TEXT NOT NULL,
            latency_ms INTEGER NOT NULL DEFAULT 0, error_type TEXT NOT NULL DEFAULT ''
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_web_service_events_time ON web_service_events(observed_at)")


def record_event(component: str, status: str, latency_ms: int = 0, error_type: str = "") -> None:
    """Record operational outcomes, never message text, coordinates or credentials."""
    if not re.fullmatch(r"[a-zA-Z0-9_:/-]{1,64}", component):
        return
    if status not in {"ok", "error", "timeout", "unavailable", "empty", "fallback"}:
        status = "error"
    safe_error = error_type if re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,79}", error_type or "") else ""
    try:
        with db.get_db() as conn:
            conn.execute(
                "INSERT INTO web_service_events(observed_at, component, status, latency_ms, error_type) "
                "VALUES (?, ?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), component, status, max(0, int(latency_ms)), safe_error),
            )
    except Exception as exc:
        logger.warning("Operational telemetry unavailable (%s)", type(exc).__name__)


def snapshot() -> dict:
    """Administrator summary; contains no chat content or recipient details."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    with db.get_db() as conn:
        events = [dict(row) for row in conn.execute(
            "SELECT component, status, COUNT(*) AS count, ROUND(AVG(latency_ms)) AS mean_latency_ms "
            "FROM web_service_events WHERE observed_at >= ? GROUP BY component, status", (cutoff,),
        )]
        latest = [dict(row) for row in conn.execute(
            "SELECT component, status, error_type, observed_at FROM web_service_events WHERE id IN "
            "(SELECT MAX(id) FROM web_service_events GROUP BY component)"
        )]
        deliveries = [dict(row) for row in conn.execute(
            "SELECT delivery_status, COUNT(*) AS count FROM alerts a JOIN incidents i "
            "ON i.incident_id = a.incident_id WHERE COALESCE(i.reporter_session_id, '') NOT LIKE 'whatsapp:%' "
            "GROUP BY delivery_status"
        )]
        attention = conn.execute(
            "SELECT COUNT(*) FROM alerts a JOIN incidents i ON i.incident_id = a.incident_id "
            "WHERE COALESCE(i.reporter_session_id, '') NOT LIKE 'whatsapp:%' AND "
            "((delivery_status = 'failed' AND delivery_attempts >= 3) OR delivery_status = 'not_configured')"
        ).fetchone()[0]
        unacknowledged = conn.execute(
            "SELECT COUNT(*) FROM alerts a JOIN incidents i ON i.incident_id = a.incident_id "
            "WHERE COALESCE(i.reporter_session_id, '') NOT LIKE 'whatsapp:%' "
            "AND delivery_status = 'delivered' AND ack_status = 'pending'"
        ).fetchone()[0]
    return {"window_hours": 24, "events": events, "latest": latest, "alert_delivery_counts": deliveries,
            "alerts_needing_operator_attention": attention, "delivered_unacknowledged": unacknowledged}


def readiness() -> dict:
    """Inspect the selected local knowledge store and recent real request outcomes.

    Hosted backends use passive observation instead of issuing potentially paid
    requests during health polling. Missing checks are reported, never guessed.
    """
    result = {"database": "unavailable", "models": "not_observed", "knowledge": {},
              "model_probe_performed": False, "outbound_messages_sent": False}
    try:
        state = snapshot()
        with db.get_db() as conn:
            sqlite_count = conn.execute("SELECT COUNT(*) FROM rag_chunks").fetchone()[0]
        result["database"] = "ready"
    except Exception as exc:
        result.update(status="not_ready", error_type=type(exc).__name__)
        return result
    backend = config.RAG_VECTOR_BACKEND
    knowledge = {"backend": backend, "status": "not_checked", "sqlite_fallback_chunks": sqlite_count}
    if backend == "sqlite":
        knowledge.update(status="ready" if sqlite_count else "empty", chunks=sqlite_count)
    elif backend == "chroma":
        if not (Path(config.CHROMA_PERSIST_DIR) / "chroma.sqlite3").is_file():
            knowledge["status"] = "missing"
        else:
            try:
                from services import chroma_rag
                collection = chroma_rag._client().get_collection(
                    config.CHROMA_COLLECTION_NAME, embedding_function=None,
                )
                count = collection.count()
                warm = bool(chroma_rag._embedder.cache_info().currsize)
                knowledge.update(status=("ready" if warm else "embedding_cold") if count else "empty",
                                 chunks=count, embedding_status="warm" if warm else "cold")
            except Exception as exc:
                knowledge.update(status="unavailable", error_type=type(exc).__name__)
    elif backend == "pinecone":
        knowledge["status"] = "not_observed" if config.PINECONE_API_KEY else "not_configured"
    else:
        knowledge["status"] = "unsupported"
    recent_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    model_events = [row for row in state["latest"] if row["component"].startswith("model:") and row["observed_at"] >= recent_cutoff]
    result["model_observations"] = model_events
    result["observation_window_minutes"] = 30
    result["models"] = "not_configured" if not config.OPENAI_API_KEY else (
        "recent_failure" if any(row["status"] != "ok" for row in model_events) else
        "recent_success" if model_events else "not_observed"
    )
    rag_event = next((row for row in state["latest"] if row["component"] == "rag:" + backend and row["observed_at"] >= recent_cutoff), None)
    if rag_event:
        knowledge["last_retrieval"] = rag_event["status"]
        knowledge["last_retrieval_at"] = rag_event["observed_at"]
        if backend == "pinecone":
            knowledge["status"] = "recent_success" if rag_event["status"] == "ok" else "recent_failure"
    result["knowledge"] = knowledge
    result["status"] = "ready" if result["models"] == "recent_success" and knowledge["status"] in {"ready", "recent_success"} and (not rag_event or rag_event["status"] == "ok") else "degraded"
    result["alert_delivery_configured"] = bool(config.SLACK_WEBHOOK_URL or config.ALERT_WEBHOOK_URL)
    result["alerts_needing_operator_attention"] = state["alerts_needing_operator_attention"]
    return result


def maintenance_tick() -> dict:
    """Run bounded web alert recovery and expire only operational telemetry."""
    from services import alerts
    retried = alerts.retry_failed_alerts(limit=10)
    with db.get_db() as conn:
        conn.execute("DELETE FROM web_service_events WHERE observed_at < ?",
                     ((datetime.now(timezone.utc) - timedelta(days=7)).isoformat(),))
    return {"alert_retries": retried, "user_retention_applied": False}


def retention(*, closed_incident_days: int, apply: bool = False, limit: int = 100) -> dict:
    """Preview or scrub old closed web reports. Defaults never delete user data.

    Incident IDs/status/times remain for aggregate operations. Clinical text,
    precise location, image identifiers, originals and raw triage are removed.
    Shared image files used by retained reports are never removed.
    """
    if isinstance(closed_incident_days, bool) or not 1 <= int(closed_incident_days) <= 36500:
        raise ValueError("An explicit retention period of at least one day is required")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(closed_incident_days))).isoformat()
    storage = Path(config.STORAGE_DIR).resolve()
    with db.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        candidates = [dict(row) for row in conn.execute(
            "SELECT incident_id, image_blob_path FROM incidents WHERE status IN ('closed', 'resolved') "
            "AND updated_at < ? AND COALESCE(reporter_session_id, '') NOT LIKE 'whatsapp:%' "
            "AND (image_blob_path IS NOT NULL OR reporter_session_id IS NOT NULL OR triage_summary IS NOT NULL) "
            "ORDER BY updated_at LIMIT ?", (cutoff, max(1, min(int(limit), 1000))),
        )]
        result = {"dry_run": not apply, "closed_incident_days": int(closed_incident_days),
                  "eligible_reports": len(candidates), "scrubbed_reports": 0, "removed_images": 0,
                  "image_errors": 0, "scope": "closed web reports only; no WhatsApp or active cases"}
        if not apply:
            return result
        for incident in candidates:
            original_path = Path(incident["image_blob_path"]) if incident["image_blob_path"] else None
            if original_path and original_path.is_symlink():
                result["image_errors"] += 1
                continue
            path = original_path.resolve() if original_path else None
            if path:
                try:
                    path.relative_to(storage)
                except ValueError:
                    result["image_errors"] += 1
                    continue
                other = conn.execute(
                    "SELECT 1 FROM incidents WHERE image_blob_path = ? AND incident_id != ? LIMIT 1",
                    (incident["image_blob_path"], incident["incident_id"]),
                ).fetchone()
                if not other:
                    try:
                        if path.is_file():
                            path.unlink()
                            result["removed_images"] += 1
                    except OSError:
                        result["image_errors"] += 1
                        continue
            conn.execute("DELETE FROM triage_events WHERE incident_id = ?", (incident["incident_id"],))
            conn.execute("UPDATE alerts SET delivery_payload = '{}', ack_by = NULL WHERE incident_id = ?", (incident["incident_id"],))
            conn.execute(
                "UPDATE incidents SET reporter_session_id = NULL, image_blob_path = NULL, image_sha256 = NULL, "
                "image_phash = NULL, lat = NULL, lng = NULL, location_source = NULL, location_accuracy = NULL, "
                "triage_summary = NULL, distress_flags = NULL WHERE incident_id = ?", (incident["incident_id"],),
            )
            result["scrubbed_reports"] += 1
        return result
