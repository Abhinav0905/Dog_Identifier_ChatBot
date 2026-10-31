import sqlite3
import uuid
import json
import re
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from config import DB_PATH


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS incidents (
                incident_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                reporter_session_id TEXT,
                image_blob_path TEXT,
                image_sha256 TEXT,
                image_phash TEXT,
                lat REAL,
                lng REAL,
                location_source TEXT,
                location_accuracy REAL,
                triage_severity TEXT,
                triage_severity_score INTEGER,
                triage_confidence REAL,
                triage_summary TEXT,
                distress_flags TEXT,
                similar_incident_id TEXT,
                similarity_score REAL,
                status TEXT DEFAULT 'new'
            );

            CREATE TABLE IF NOT EXISTS alerts (
                alert_id TEXT PRIMARY KEY,
                incident_id TEXT NOT NULL,
                alert_channel TEXT NOT NULL,
                trigger_reason TEXT,
                sent_at TEXT NOT NULL,
                ack_status TEXT DEFAULT 'pending',
                ack_by TEXT,
                ack_at TEXT,
                FOREIGN KEY (incident_id) REFERENCES incidents(incident_id)
            );

            CREATE TABLE IF NOT EXISTS triage_events (
                event_id TEXT PRIMARY KEY,
                incident_id TEXT NOT NULL,
                model_version TEXT,
                raw_output TEXT,
                postprocessed_output TEXT,
                latency_ms INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY (incident_id) REFERENCES incidents(incident_id)
            );

            CREATE TABLE IF NOT EXISTS admin_query_audit (
                query_id TEXT PRIMARY KEY,
                admin_user_id TEXT,
                nl_query TEXT,
                resolved_sql TEXT,
                executed_at TEXT NOT NULL,
                row_count INTEGER,
                status TEXT
            );

            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS conversations (
                conversation_id TEXT PRIMARY KEY,
                owner_type TEXT NOT NULL DEFAULT 'guest',
                owner_id_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_incidents_sha256 ON incidents(image_sha256);
            CREATE INDEX IF NOT EXISTS idx_incidents_phash ON incidents(image_phash);
            CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);
            CREATE INDEX IF NOT EXISTS idx_incidents_severity ON incidents(triage_severity);
            CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_history(session_id);
            CREATE INDEX IF NOT EXISTS idx_conversations_owner
                ON conversations(owner_type, owner_id_hash, status);
            CREATE INDEX IF NOT EXISTS idx_conversations_expires
                ON conversations(expires_at);

            CREATE TABLE IF NOT EXISTS session_locations (
                session_id TEXT PRIMARY KEY,
                lat REAL NOT NULL,
                lng REAL NOT NULL,
                source TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS whatsapp_message_deliveries (
                message_sid TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'processing',
                lease_token TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 1,
                reply_text TEXT NOT NULL DEFAULT '',
                outbound_sid TEXT NOT NULL DEFAULT '',
                error_code TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS session_case_locations (
                session_id TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                place TEXT,
                city TEXT NOT NULL DEFAULT '',
                region TEXT NOT NULL DEFAULT '',
                lat REAL,
                lng REAL,
                country_code TEXT,
                source TEXT NOT NULL,
                in_dharamsala INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS place_resolution_cache (
                query_key TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                display_name TEXT,
                city TEXT NOT NULL DEFAULT '',
                region TEXT NOT NULL DEFAULT '',
                lat REAL,
                lng REAL,
                country_code TEXT,
                source TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ngo_search_cache (
                cache_key TEXT PRIMARY KEY,
                city TEXT NOT NULL,
                region TEXT NOT NULL,
                country_code TEXT NOT NULL,
                language TEXT NOT NULL,
                result_kind TEXT NOT NULL,
                response TEXT NOT NULL,
                resource_links TEXT NOT NULL,
                organizations_json TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ngo_search_cache_updated
                ON ngo_search_cache(updated_at);

            CREATE TABLE IF NOT EXISTS rag_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_file TEXT NOT NULL,
                title TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL,
                embedding TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_rag_chunks_doc ON rag_chunks(doc_file);
        """)
        # Existing production databases predate canonical city/region fields.
        # SQLite's CREATE TABLE IF NOT EXISTS does not add new columns.
        alert_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(alerts)").fetchall()
        }
        for name, declaration in {
            "delivery_status": "TEXT NOT NULL DEFAULT 'unknown'",
            "delivery_details": "TEXT NOT NULL DEFAULT '{}'",
            "attempted_at": "TEXT",
            "delivered_at": "TEXT",
            "delivery_payload": "TEXT NOT NULL DEFAULT '{}'",
            "delivery_attempts": "INTEGER NOT NULL DEFAULT 0",
            "delivery_retry_at": "TEXT",
            "delivery_lease_token": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if name not in alert_columns:
                conn.execute(f"ALTER TABLE alerts ADD COLUMN {name} {declaration}")
        message_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(whatsapp_message_deliveries)").fetchall()
        }
        for name, declaration in {
            "reply_to": "TEXT NOT NULL DEFAULT ''",
            "reply_from": "TEXT NOT NULL DEFAULT ''",
            "reply_attempts": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in message_columns:
                conn.execute(f"ALTER TABLE whatsapp_message_deliveries ADD COLUMN {name} {declaration}")
        case_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(session_case_locations)").fetchall()
        }
        if "city" not in case_columns:
            conn.execute(
                "ALTER TABLE session_case_locations ADD COLUMN city TEXT NOT NULL DEFAULT ''"
            )
        if "region" not in case_columns:
            conn.execute(
                "ALTER TABLE session_case_locations ADD COLUMN region TEXT NOT NULL DEFAULT ''"
            )
        place_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(place_resolution_cache)").fetchall()
        }
        if "city" not in place_columns:
            conn.execute(
                "ALTER TABLE place_resolution_cache ADD COLUMN city TEXT NOT NULL DEFAULT ''"
            )
        if "region" not in place_columns:
            conn.execute(
                "ALTER TABLE place_resolution_cache ADD COLUMN region TEXT NOT NULL DEFAULT ''"
            )
        chat_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(chat_history)").fetchall()
        }
        if "metadata_json" not in chat_columns:
            conn.execute(
                "ALTER TABLE chat_history ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"
            )
        ngo_cache_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(ngo_search_cache)").fetchall()
        }
        if "organizations_json" not in ngo_cache_columns:
            conn.execute(
                "ALTER TABLE ngo_search_cache "
                "ADD COLUMN organizations_json TEXT NOT NULL DEFAULT '[]'"
            )


def create_guest_conversation(
    owner_id_hash: str,
    *,
    ttl_hours: int = 24,
    conversation_id: str | None = None,
) -> dict:
    """Create an anonymous conversation owned by a hashed browser token."""
    if not owner_id_hash:
        raise ValueError("owner_id_hash is required")
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=max(1, int(ttl_hours)))
    conversation_id = conversation_id or str(uuid.uuid4())
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO conversations (
                conversation_id, owner_type, owner_id_hash, status,
                created_at, updated_at, expires_at
            ) VALUES (?, 'guest', ?, 'active', ?, ?, ?)
            """,
            (
                conversation_id,
                owner_id_hash,
                now.isoformat(),
                now.isoformat(),
                expires_at.isoformat(),
            ),
        )
    return {
        "conversation_id": conversation_id,
        "status": "active",
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
    }


def get_owned_conversation(
    conversation_id: str,
    owner_id_hash: str,
    *,
    include_archived: bool = False,
) -> dict | None:
    """Return a live conversation only when its anonymous owner matches."""
    now = datetime.now(timezone.utc).isoformat()
    status_clause = "status IN ('active', 'archived')" if include_archived else "status = 'active'"
    with get_db() as conn:
        row = conn.execute(
            f"""
            SELECT conversation_id, owner_type, status, created_at, updated_at, expires_at
            FROM conversations
            WHERE conversation_id = ?
              AND owner_type = 'guest'
              AND owner_id_hash = ?
              AND {status_clause}
              AND expires_at >= ?
            """,
            (conversation_id, owner_id_hash, now),
        ).fetchone()
        return dict(row) if row else None


def conversation_exists(conversation_id: str) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        return bool(row)


def touch_owned_conversation(
    conversation_id: str,
    owner_id_hash: str,
    *,
    ttl_hours: int = 24,
) -> bool:
    """Extend an active guest conversation after an authorized request."""
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=max(1, int(ttl_hours)))
    with get_db() as conn:
        cursor = conn.execute(
            """
            UPDATE conversations
            SET updated_at = ?, expires_at = ?
            WHERE conversation_id = ?
              AND owner_type = 'guest'
              AND owner_id_hash = ?
              AND status = 'active'
              AND expires_at >= ?
            """,
            (
                now.isoformat(),
                expires_at.isoformat(),
                conversation_id,
                owner_id_hash,
                now.isoformat(),
            ),
        )
        return cursor.rowcount == 1


def archive_owned_conversation(conversation_id: str, owner_id_hash: str) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.execute(
            """
            UPDATE conversations
            SET status = 'archived', updated_at = ?
            WHERE conversation_id = ?
              AND owner_type = 'guest'
              AND owner_id_hash = ?
              AND status = 'active'
              AND expires_at >= ?
            """,
            (now, conversation_id, owner_id_hash, now),
        )
        return cursor.rowcount == 1


def cleanup_expired_guest_conversations(limit: int = 100) -> int:
    """Delete expired guest chat/state while retaining incident records."""
    now = datetime.now(timezone.utc).isoformat()
    safe_limit = max(1, min(int(limit), 1000))
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT conversation_id
            FROM conversations
            WHERE owner_type = 'guest' AND expires_at < ?
            ORDER BY expires_at
            LIMIT ?
            """,
            (now, safe_limit),
        ).fetchall()
        conversation_ids = [str(row[0]) for row in rows]
        for conversation_id in conversation_ids:
            conn.execute("DELETE FROM chat_history WHERE session_id = ?", (conversation_id,))
            conn.execute(
                "DELETE FROM session_case_locations WHERE session_id = ?",
                (conversation_id,),
            )
            conn.execute(
                "DELETE FROM session_locations WHERE session_id = ?",
                (conversation_id,),
            )
            conn.execute(
                "DELETE FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            )
        return len(conversation_ids)


def save_session_location(session_id: str, lat: float, lng: float, source: str) -> None:
    """Persist the latest verified reporter location for channel workflows."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO session_locations (session_id, lat, lng, source, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                lat = excluded.lat,
                lng = excluded.lng,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            (session_id, lat, lng, source, now),
        )


def get_session_location(session_id: str, max_age_minutes: int = 60) -> dict | None:
    """Return only a recent reporter location, never an indefinitely reused pin."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max(1, max_age_minutes))
    with get_db() as conn:
        row = conn.execute(
            "SELECT lat, lng, source, updated_at FROM session_locations "
            "WHERE session_id = ? AND updated_at >= ?",
            (session_id, cutoff.isoformat()),
        ).fetchone()
        return dict(row) if row else None


def reserve_whatsapp_message(
    message_sid: str, session_id: str, *, lease_seconds: int = 600,
) -> dict:
    """Atomically claim an inbound message; retries cannot run concurrently.

    A failed or expired claim can be retried. An outbound SID is retained so a
    caller can avoid resending a reply already accepted by the messaging API.
    """
    if not message_sid or not session_id:
        raise ValueError("MessageSid and session_id are required")
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(seconds=max(30, lease_seconds))).isoformat()
    token = str(uuid.uuid4())
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM whatsapp_message_deliveries WHERE message_sid = ?",
            (message_sid,),
        ).fetchone()
        if row:
            previous = dict(row)
            if previous["session_id"] != session_id:
                raise ValueError("MessageSid belongs to another session")
            if previous["status"] == "completed" or previous["reply_attempts"] >= 3 or (
                previous["status"] == "processing" and previous["updated_at"] >= cutoff
            ):
                return {**previous, "acquired": False}
            conn.execute(
                "UPDATE whatsapp_message_deliveries SET status = 'processing', "
                "lease_token = ?, attempts = attempts + 1, error_code = '', updated_at = ? "
                "WHERE message_sid = ?",
                (token, now.isoformat(), message_sid),
            )
        else:
            conn.execute(
                "INSERT INTO whatsapp_message_deliveries "
                "(message_sid, session_id, lease_token, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (message_sid, session_id, token, now.isoformat(), now.isoformat()),
            )
        result = dict(conn.execute(
            "SELECT * FROM whatsapp_message_deliveries WHERE message_sid = ?", (message_sid,),
        ).fetchone())
        return {**result, "acquired": True}


def save_whatsapp_reply(
    message_sid: str, lease_token: str, *, to: str, from_: str, reply_text: str,
) -> bool:
    """Durably queue a generated reply before the caller's first send attempt."""
    if not to or not from_ or not reply_text:
        raise ValueError("Reply text and WhatsApp routing addresses are required")
    with get_db() as conn:
        result = conn.execute(
            "UPDATE whatsapp_message_deliveries SET reply_text = ?, reply_to = ?, "
            "reply_from = ?, reply_attempts = reply_attempts + 1, updated_at = ? "
            "WHERE message_sid = ? AND lease_token = ? AND status = 'processing' "
            "AND reply_attempts < 3",
            (reply_text, to, from_, datetime.now(timezone.utc).isoformat(), message_sid, lease_token),
        )
        return result.rowcount == 1


def claim_pending_whatsapp_replies(
    limit: int = 10, *, stale_seconds: int = 600, retry_delay_seconds: int = 60,
) -> list[dict]:
    """Claim bounded persisted replies for retry after failure or worker loss."""
    now = datetime.now(timezone.utc)
    stale = (now - timedelta(seconds=max(30, stale_seconds))).isoformat()
    retry = (now - timedelta(seconds=max(0, retry_delay_seconds))).isoformat()
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT * FROM whatsapp_message_deliveries WHERE reply_text != '' "
            "AND reply_to != '' AND reply_from != '' AND outbound_sid = '' "
            "AND reply_attempts < 3 AND ((status = 'failed' AND updated_at <= ?) "
            "OR (status = 'processing' AND updated_at <= ?)) ORDER BY updated_at LIMIT ?",
            (retry, stale, max(1, min(int(limit), 100))),
        ).fetchall()
        claimed = []
        for row in rows:
            token = str(uuid.uuid4())
            conn.execute(
                "UPDATE whatsapp_message_deliveries SET status = 'processing', lease_token = ?, "
                "attempts = attempts + 1, reply_attempts = reply_attempts + 1, updated_at = ? "
                "WHERE message_sid = ?",
                (token, now.isoformat(), row["message_sid"]),
            )
            claimed.append({**dict(row), "lease_token": token, "status": "processing"})
        return claimed


def finish_whatsapp_message(
    message_sid: str, lease_token: str, *, succeeded: bool,
    reply_text: str = "", outbound_sid: str = "", error_code: str = "",
) -> bool:
    """Finish only the active claim, preserving failed work for a later retry."""
    with get_db() as conn:
        result = conn.execute(
            "UPDATE whatsapp_message_deliveries SET status = ?, "
            "reply_text = CASE WHEN ? != '' THEN ? ELSE reply_text END, "
            "outbound_sid = CASE WHEN ? != '' THEN ? ELSE outbound_sid END, "
            "error_code = ?, updated_at = ? "
            "WHERE message_sid = ? AND lease_token = ? AND status = 'processing'",
            (
                "completed" if succeeded else "failed", reply_text, reply_text, outbound_sid, outbound_sid,
                str(error_code)[:200], datetime.now(timezone.utc).isoformat(),
                message_sid, lease_token,
            ),
        )
        return result.rowcount == 1


def save_session_case_location(session_id: str, decision: dict) -> None:
    """Persist structured case geography without re-parsing chat prose later."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO session_case_locations (
                session_id, scope, place, city, region, lat, lng, country_code,
                source, in_dharamsala, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                scope = excluded.scope,
                place = excluded.place,
                city = excluded.city,
                region = excluded.region,
                lat = excluded.lat,
                lng = excluded.lng,
                country_code = excluded.country_code,
                source = excluded.source,
                in_dharamsala = excluded.in_dharamsala,
                updated_at = excluded.updated_at
            """,
            (
                session_id,
                decision.get("scope", "unspecified"),
                decision.get("place", ""),
                decision.get("city", ""),
                decision.get("region", ""),
                decision.get("lat"),
                decision.get("lng"),
                decision.get("country_code", ""),
                decision.get("source", "unknown"),
                1 if decision.get("in_dharamsala") else 0,
                now,
            ),
        )


def get_session_case_location(session_id: str, max_age_minutes: int = 60) -> dict | None:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max(1, max_age_minutes))
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT scope, place, city, region, lat, lng, country_code, source,
                   in_dharamsala, updated_at
            FROM session_case_locations
            WHERE session_id = ? AND updated_at >= ?
            """,
            (session_id, cutoff.isoformat()),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["in_dharamsala"] = bool(result["in_dharamsala"])
        return result


def clear_session_case_location(session_id: str) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM session_case_locations WHERE session_id = ?", (session_id,))


def save_place_resolution(query_key: str, resolution: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO place_resolution_cache (
                query_key, scope, display_name, city, region, lat, lng,
                country_code, source, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(query_key) DO UPDATE SET
                scope = excluded.scope,
                display_name = excluded.display_name,
                city = excluded.city,
                region = excluded.region,
                lat = excluded.lat,
                lng = excluded.lng,
                country_code = excluded.country_code,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            (
                query_key,
                resolution.get("scope", "ambiguous"),
                resolution.get("display_name", ""),
                resolution.get("city", ""),
                resolution.get("region", ""),
                resolution.get("lat"),
                resolution.get("lng"),
                resolution.get("country_code", ""),
                resolution.get("source", "geocoder"),
                now,
            ),
        )


def get_place_resolution(query_key: str, max_age_days: int = 30) -> dict | None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, max_age_days))
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT scope, display_name, city, region, lat, lng,
                   country_code, source, updated_at
            FROM place_resolution_cache
            WHERE query_key = ? AND updated_at >= ?
            """,
            (query_key, cutoff.isoformat()),
        ).fetchone()
        return dict(row) if row else None


def save_ngo_search_cache(
    cache_key: str,
    *,
    city: str,
    region: str,
    country_code: str,
    language: str,
    result_kind: str,
    response: str,
    resource_links: list[dict],
    organizations: list[dict] | None = None,
) -> None:
    """Persist a fully validated city-level NGO search result."""
    if result_kind != "verified_options":
        raise ValueError("Only verified NGO options may be cached")
    if not str(city or "").strip() or not str(region or "").strip():
        raise ValueError("A city and region are required for NGO caching")
    if not _valid_cached_ngo_organizations(organizations):
        raise ValueError("At least one structured NGO organization is required")
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO ngo_search_cache (
                cache_key, city, region, country_code, language,
                result_kind, response, resource_links, organizations_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                city = excluded.city,
                region = excluded.region,
                country_code = excluded.country_code,
                language = excluded.language,
                result_kind = excluded.result_kind,
                response = excluded.response,
                resource_links = excluded.resource_links,
                organizations_json = excluded.organizations_json,
                updated_at = excluded.updated_at
            """,
            (
                cache_key,
                city,
                region,
                country_code.upper(),
                language,
                result_kind,
                response,
                json.dumps(resource_links),
                json.dumps(organizations),
                now,
            ),
        )


def get_ngo_search_cache(cache_key: str, max_age_hours: int = 24) -> dict | None:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, max_age_hours))
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT city, region, country_code, language, result_kind,
                   response, resource_links, organizations_json, updated_at
            FROM ngo_search_cache
            WHERE cache_key = ?
              AND result_kind = 'verified_options'
              AND city <> ''
              AND region <> ''
              AND updated_at >= ?
            """,
            (cache_key, cutoff.isoformat()),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    try:
        links = json.loads(result.get("resource_links") or "[]")
        organizations = json.loads(result.get("organizations_json") or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(links, list) or not _valid_cached_ngo_organizations(organizations):
        return None
    result["resource_links"] = links
    result["organizations"] = organizations
    result.pop("organizations_json", None)
    return result


def _valid_cached_ngo_organizations(value: object) -> bool:
    """Apply the storage-layer minimum contract for positive NGO snapshots."""
    if not isinstance(value, list) or not value:
        return False
    required_string_fields = (
        "name",
        "service_area",
        "service_city",
        "service_region",
        "animal_rescue_evidence",
        "service_area_evidence",
        "organization_type_evidence",
        "animal_rescue_evidence_url",
        "service_area_evidence_url",
        "organization_type_evidence_url",
        "official_url",
        "phone",
        "phone_source_url",
        "address",
        "address_source_url",
        "opening_hours",
        "opening_hours_source_url",
    )
    required_nonempty_fields = (
        "name",
        "service_area",
        "service_city",
        "service_region",
        "animal_rescue_evidence",
        "service_area_evidence",
        "official_url",
    )
    for organization in value:
        if not isinstance(organization, dict):
            return False
        if any(
            not isinstance(organization.get(field), str)
            for field in required_string_fields
        ):
            return False
        if any(
            not organization[field].strip()
            for field in required_nonempty_fields
        ):
            return False
        for detail_field, source_field in (
            ("phone", "phone_source_url"),
            ("address", "address_source_url"),
            ("opening_hours", "opening_hours_source_url"),
        ):
            if bool(organization[detail_field].strip()) != bool(
                organization[source_field].strip()
            ):
                return False
    return True


def create_incident(
    session_id: str,
    image_blob_path: str = None,
    image_sha256: str = None,
    image_phash: str = None,
    lat: float = None,
    lng: float = None,
    location_source: str = None,
    triage_severity: str = None,
    triage_severity_score: int = None,
    triage_confidence: float = None,
    triage_summary: str = None,
    distress_flags: list = None,
    similar_incident_id: str = None,
    similarity_score: float = None,
    status: str = "new",
) -> str:
    incident_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO incidents (
                incident_id, created_at, updated_at, reporter_session_id,
                image_blob_path, image_sha256, image_phash,
                lat, lng, location_source,
                triage_severity, triage_severity_score, triage_confidence, triage_summary,
                distress_flags, similar_incident_id, similarity_score, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                incident_id, now, now, session_id,
                image_blob_path, image_sha256, image_phash,
                lat, lng, location_source,
                triage_severity, triage_severity_score, triage_confidence, triage_summary,
                json.dumps(distress_flags or []), similar_incident_id, similarity_score, status,
            ),
        )
    return incident_id


def get_incident(incident_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)
        ).fetchone()
        return dict(row) if row else None


def update_incident(incident_id: str, **kwargs):
    kwargs["updated_at"] = datetime.now(timezone.utc).isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [incident_id]
    with get_db() as conn:
        conn.execute(
            f"UPDATE incidents SET {set_clause} WHERE incident_id = ?", values
        )


def find_by_sha256(sha256: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM incidents WHERE image_sha256 = ? ORDER BY created_at DESC LIMIT 1",
            (sha256,),
        ).fetchone()
        return dict(row) if row else None


def find_all_phashes() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT incident_id, image_phash FROM incidents WHERE image_phash IS NOT NULL"
        ).fetchall()
        return [dict(r) for r in rows]


def create_alert(
    incident_id: str, channel: str, reason: str, *, delivery_status: str = "unknown",
) -> str:
    alert_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO alerts (
                alert_id, incident_id, alert_channel, trigger_reason, sent_at,
                attempted_at, delivery_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (alert_id, incident_id, channel, reason, now, now, delivery_status),
        )
    return alert_id


def record_alert_delivery(
    alert_id: str, status: str, *, channels: list[str], failures: dict[str, str],
) -> None:
    """Store external delivery separately from acknowledgement or case acceptance."""
    if status not in {"delivered", "failed", "not_configured"}:
        raise ValueError("Unknown alert delivery status")
    delivered_at = datetime.now(timezone.utc).isoformat() if status == "delivered" else None
    with get_db() as conn:
        conn.execute(
            "UPDATE alerts SET delivery_status = ?, delivery_details = ?, "
            "delivered_at = ?, alert_channel = ? WHERE alert_id = ?",
            (
                status, json.dumps({"channels": channels, "failures": failures}),
                delivered_at, ",".join(channels) or "console", alert_id,
            ),
        )


def get_delivered_alert(incident_id: str) -> dict | None:
    """Reuse a confirmed notification when an automatic workflow is repeated."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM alerts WHERE incident_id = ? AND delivery_status = 'delivered' "
            "ORDER BY delivered_at DESC LIMIT 1", (incident_id,),
        ).fetchone()
        return dict(row) if row else None


def reserve_alert_delivery(incident_id: str, payload: dict, reason: str, *, force_retry: bool = False) -> dict:
    """Atomically reserve a web alert, suppressing concurrent duplicate sends."""
    now = datetime.now(timezone.utc)
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not force_retry:
            previous = conn.execute(
                "SELECT * FROM alerts WHERE incident_id = ? AND delivery_status IN ('delivered', 'pending') "
                "ORDER BY CASE WHEN delivery_status = 'delivered' THEN 0 ELSE 1 END, sent_at DESC LIMIT 1",
                (incident_id,),
            ).fetchone()
            if previous:
                return {**dict(previous), "acquired": False}
            retry = conn.execute(
                "SELECT * FROM alerts WHERE incident_id = ? AND delivery_status = 'failed' "
                "AND delivery_payload != '{}' ORDER BY sent_at DESC LIMIT 1", (incident_id,),
            ).fetchone()
            if retry:
                # A normal repeat cannot bypass the bounded retry worker.
                return {**dict(retry), "acquired": False}
        alert_id, token = str(uuid.uuid4()), str(uuid.uuid4())
        conn.execute(
            "INSERT INTO alerts (alert_id, incident_id, alert_channel, trigger_reason, sent_at, "
            "attempted_at, delivery_status, delivery_payload, delivery_attempts, delivery_lease_token) "
            "VALUES (?, ?, 'console', ?, ?, ?, 'pending', ?, 1, ?)",
            (alert_id, incident_id, reason, now.isoformat(), now.isoformat(), json.dumps(payload), token),
        )
        return dict(conn.execute("SELECT *, 1 AS acquired FROM alerts WHERE alert_id = ?", (alert_id,)).fetchone())


def finish_alert_delivery(
    alert_id: str, lease_token: str, status: str, *, channels: list[str], failures: dict[str, str],
) -> bool:
    if status not in {"delivered", "failed", "not_configured"}:
        raise ValueError("Unknown alert delivery status")
    now = datetime.now(timezone.utc)
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT delivery_attempts FROM alerts WHERE alert_id = ? AND delivery_lease_token = ? "
            "AND delivery_status = 'pending'", (alert_id, lease_token),
        ).fetchone()
        if not row:
            return False
        retry_at = (now + timedelta(seconds=60 * (2 ** max(0, row[0] - 1)))).isoformat() if status == "failed" and row[0] < 3 else None
        updated = conn.execute(
            "UPDATE alerts SET delivery_status = ?, delivery_details = ?, delivered_at = ?, "
            "delivery_retry_at = ?, alert_channel = ? WHERE alert_id = ? AND delivery_lease_token = ? "
            "AND delivery_status = 'pending'",
            (status, json.dumps({"channels": channels, "failures": failures}),
             now.isoformat() if status == "delivered" else None, retry_at,
             ",".join(channels) or "console", alert_id, lease_token),
        )
        return updated.rowcount == 1


def claim_failed_alerts(limit: int = 10) -> list[dict]:
    """Claim due web-only retries; stale in-flight attempts are also recoverable."""
    now = datetime.now(timezone.utc)
    stale = (now - timedelta(minutes=5)).isoformat()
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE alerts SET delivery_status = 'failed', delivery_retry_at = NULL, "
            "delivery_details = ? WHERE delivery_status = 'pending' AND delivery_attempts >= 3 "
            "AND attempted_at <= ? AND incident_id IN (SELECT incident_id FROM incidents "
            "WHERE COALESCE(reporter_session_id, '') NOT LIKE 'whatsapp:%')",
            (json.dumps({"channels": [], "failures": {"worker": "InterruptedDeliveryOutcomeUnknown"}}), stale),
        )
        rows = conn.execute(
            "SELECT a.* FROM alerts a JOIN incidents i ON i.incident_id = a.incident_id "
            "WHERE COALESCE(i.reporter_session_id, '') NOT LIKE 'whatsapp:%' "
            "AND i.status NOT IN ('closed', 'resolved') AND a.delivery_payload != '{}' "
            "AND a.delivery_attempts < 3 AND ((a.delivery_status = 'failed' AND a.delivery_retry_at <= ?) "
            "OR (a.delivery_status = 'pending' AND a.attempted_at <= ?)) "
            "AND NOT EXISTS (SELECT 1 FROM alerts done WHERE done.incident_id = a.incident_id "
            "AND done.delivery_status = 'delivered') ORDER BY a.sent_at LIMIT ?",
            (now.isoformat(), stale, max(1, min(int(limit), 50))),
        ).fetchall()
        claimed = []
        for row in rows:
            token = str(uuid.uuid4())
            conn.execute(
                "UPDATE alerts SET delivery_status = 'pending', delivery_attempts = delivery_attempts + 1, "
                "delivery_lease_token = ?, attempted_at = ?, delivery_retry_at = NULL WHERE alert_id = ?",
                (token, now.isoformat(), row["alert_id"]),
            )
            claimed.append({**dict(row), "delivery_lease_token": token, "delivery_attempts": row["delivery_attempts"] + 1})
        return claimed


def acknowledge_delivered_alert(alert_id: str, actor: str) -> bool:
    """Record an authenticated receiver's acknowledgement, never infer dispatch."""
    if not actor.strip() or len(actor) > 120:
        raise ValueError("An acknowledgement actor is required (maximum 120 characters)")
    with get_db() as conn:
        result = conn.execute(
            "UPDATE alerts SET ack_status = 'acknowledged', ack_by = ?, ack_at = ? "
            "WHERE alert_id = ? AND delivery_status = 'delivered' AND ack_status = 'pending'",
            (actor.strip(), datetime.now(timezone.utc).isoformat(), alert_id),
        )
        return result.rowcount == 1


def create_triage_event(incident_id: str, model_version: str, raw_output: str, postprocessed: str, latency_ms: int) -> str:
    event_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO triage_events (event_id, incident_id, model_version, raw_output, postprocessed_output, latency_ms, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (event_id, incident_id, model_version, raw_output, postprocessed, latency_ms, now),
        )
    return event_id


def log_admin_query(admin_user: str, nl_query: str, sql: str, row_count: int, status: str) -> str:
    query_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO admin_query_audit (query_id, admin_user_id, nl_query, resolved_sql, executed_at, row_count, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (query_id, admin_user, nl_query, sql, now, row_count, status),
        )
    return query_id


def save_chat_message(
    session_id: str,
    role: str,
    content: str,
    metadata: dict | None = None,
):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO chat_history (
                session_id, role, content, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, role, content, json.dumps(metadata or {}), now),
        )


def get_chat_history(session_id: str, limit: int = 20) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT role, content, metadata_json, created_at
            FROM chat_history
            WHERE session_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()
        history: list[dict] = []
        for row in reversed(rows):
            item = dict(row)
            try:
                item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                item.pop("metadata_json", None)
                item["metadata"] = {}
            history.append(item)
        return history


ADMIN_ANALYTICS_TABLES = frozenset({"incidents", "alerts", "triage_events"})
_ANALYTICS_FUNCTIONS = frozenset({
    "abs", "avg", "coalesce", "count", "date", "datetime", "ifnull", "iif",
    "instr", "julianday", "length", "lower", "ltrim", "max", "min", "nullif",
    "replace", "round", "rtrim", "strftime", "substr", "substring", "sum",
    "time", "total", "trim", "typeof", "unixepoch", "upper", "like", "glob",
    "row_number", "rank", "dense_rank", "lag", "lead", "first_value", "last_value",
})


def execute_readonly_sql(
    sql: str, *, max_rows: int = 100, timeout_seconds: float = 1.0,
) -> list[dict]:
    """Run bounded analytics with SQLite enforcing read-only table access.

    The authorizer sees parsed table accesses, including nested queries and
    CTEs. Names such as created_at are data identifiers, not SQL commands.
    """
    if not isinstance(sql, str) or not re.match(r"\s*(?:SELECT|WITH)\b", sql, re.I):
        raise ValueError("Only SELECT queries are allowed")
    if len(sql) > 20_000:
        raise ValueError("Query is too long")
    limit = max(1, min(int(max_rows), 100))
    budget = max(0.01, min(float(timeout_seconds), 5.0))
    deadline = time.monotonic() + budget

    def authorize(action, table, field, database, source):
        if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE}:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            return sqlite3.SQLITE_OK if (
                (database == "main" or (database is None and field == ""))
                and str(table).casefold() in ADMIN_ANALYTICS_TABLES
            ) else sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION:
            return sqlite3.SQLITE_OK if str(field).casefold() in _ANALYTICS_FUNCTIONS else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_DENY

    conn = sqlite3.connect(Path(DB_PATH).resolve().as_uri() + "?mode=ro", uri=True, timeout=budget)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        if hasattr(conn, "setlimit"):
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1_000_000)
            conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 20_000)
            conn.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 128)
            conn.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 100)
        conn.set_authorizer(authorize)
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        return [dict(row) for row in conn.execute(sql).fetchmany(limit)]
    except sqlite3.DatabaseError as exc:
        if time.monotonic() >= deadline or "interrupted" in str(exc).lower():
            raise ValueError("Query exceeded its execution time limit") from exc
        raise ValueError("Query must read only approved analytics tables and functions") from exc
    finally:
        conn.close()


def get_incidents_list(limit: int = 100, status: str = None, severity: str = None) -> list[dict]:
    query = "SELECT * FROM incidents"
    params = []
    conditions = []
    if status:
        conditions.append("status = ?")
        params.append(status)
    if severity:
        conditions.append("triage_severity = ?")
        params.append(severity)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with get_db() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def insert_rag_chunk(doc_file: str, title: str, chunk_index: int, content: str, embedding: str | None):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO rag_chunks (doc_file, title, chunk_index, content, embedding) VALUES (?, ?, ?, ?, ?)",
            (doc_file, title, chunk_index, content, embedding),
        )


def delete_rag_chunks_for_doc(doc_file: str):
    with get_db() as conn:
        conn.execute("DELETE FROM rag_chunks WHERE doc_file = ?", (doc_file,))


def get_all_rag_chunks() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, doc_file, title, chunk_index, content, embedding FROM rag_chunks ORDER BY doc_file, chunk_index"
        ).fetchall()
        return [dict(r) for r in rows]


def get_alerts_list(limit: int = 100) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT a.*, i.triage_severity, i.triage_summary FROM alerts a JOIN incidents i ON a.incident_id = i.incident_id ORDER BY a.sent_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
