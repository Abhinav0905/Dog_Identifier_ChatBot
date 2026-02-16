import sqlite3
import uuid
import json
from datetime import datetime, timezone
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
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_incidents_sha256 ON incidents(image_sha256);
            CREATE INDEX IF NOT EXISTS idx_incidents_phash ON incidents(image_phash);
            CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);
            CREATE INDEX IF NOT EXISTS idx_incidents_severity ON incidents(triage_severity);
            CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_history(session_id);
        """)


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


def create_alert(incident_id: str, channel: str, reason: str) -> str:
    alert_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO alerts (alert_id, incident_id, alert_channel, trigger_reason, sent_at)
            VALUES (?, ?, ?, ?, ?)""",
            (alert_id, incident_id, channel, reason, now),
        )
    return alert_id


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


def save_chat_message(session_id: str, role: str, content: str):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO chat_history (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (session_id, role, content, now),
        )


def get_chat_history(session_id: str, limit: int = 20) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT role, content FROM chat_history WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


def execute_readonly_sql(sql: str) -> list[dict]:
    """Execute a read-only SQL query. Only SELECT statements allowed."""
    stripped = sql.strip().upper()
    if not stripped.startswith("SELECT"):
        raise ValueError("Only SELECT queries are allowed")
    for forbidden in ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE", "EXEC"]:
        if forbidden in stripped.split("SELECT", 1)[-1]:
            raise ValueError(f"Forbidden keyword: {forbidden}")
    with get_db() as conn:
        rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]


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


def get_alerts_list(limit: int = 100) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT a.*, i.triage_severity, i.triage_summary FROM alerts a JOIN incidents i ON a.incident_id = i.incident_id ORDER BY a.sent_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
