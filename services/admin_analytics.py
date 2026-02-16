"""
Admin natural-language analytics service.
Converts NL queries to safe read-only SQL using Claude, with strict allowlists.
"""

import json
import logging
from anthropic import Anthropic
from config import ANTHROPIC_API_KEY
import database as db

logger = logging.getLogger("dharmasala.admin")

client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

NL_TO_SQL_SYSTEM = """You are a SQL query generator for the Dharmasala Animal Rescue incident database.
You convert natural language questions into safe, read-only SQLite SELECT queries.

STRICT RULES:
- ONLY generate SELECT statements. Never INSERT, UPDATE, DELETE, DROP, ALTER, or any DDL/DML.
- ONLY query these tables and columns:

  incidents: incident_id, created_at, updated_at, reporter_session_id, triage_severity,
             triage_severity_score, triage_confidence, triage_summary, distress_flags,
             lat, lng, location_source, similar_incident_id, similarity_score, status

  alerts: alert_id, incident_id, alert_channel, trigger_reason, sent_at, ack_status, ack_by, ack_at

- Use SQLite date functions (date(), datetime(), julianday()) for time-based queries.
- Limit results to 100 rows maximum.
- Use appropriate GROUP BY, COUNT, AVG aggregations for summary queries.

Respond with ONLY a valid JSON object:
{
    "sql": "SELECT ...",
    "explanation": "Brief explanation of what this query does"
}"""

# Allowed table names for validation
ALLOWED_TABLES = {"incidents", "alerts", "triage_events"}


def process_nl_query(nl_query: str, admin_user: str = "admin") -> dict:
    """Convert NL query to SQL, execute safely, and return results."""

    # Generate SQL from NL
    sql, explanation = _nl_to_sql(nl_query)

    if not sql:
        db.log_admin_query(admin_user, nl_query, "", 0, "failed_generation")
        return {
            "query": nl_query,
            "sql_generated": "",
            "results": [],
            "row_count": 0,
            "summary": "I couldn't generate a query for that question. Try asking about incidents, severity levels, alert counts, or trends over time.",
        }

    # Execute safely
    try:
        results = db.execute_readonly_sql(sql)
        row_count = len(results)
        db.log_admin_query(admin_user, nl_query, sql, row_count, "success")

        summary = _summarize_results(nl_query, results, explanation)

        return {
            "query": nl_query,
            "sql_generated": sql,
            "results": results[:100],
            "row_count": row_count,
            "summary": summary,
        }
    except ValueError as e:
        db.log_admin_query(admin_user, nl_query, sql, 0, f"blocked: {e}")
        return {
            "query": nl_query,
            "sql_generated": sql,
            "results": [],
            "row_count": 0,
            "summary": f"Query blocked by safety filter: {e}",
        }
    except Exception as e:
        db.log_admin_query(admin_user, nl_query, sql, 0, f"error: {e}")
        return {
            "query": nl_query,
            "sql_generated": sql,
            "results": [],
            "row_count": 0,
            "summary": f"Query execution error. Please try rephrasing your question.",
        }


def _nl_to_sql(nl_query: str) -> tuple[str, str]:
    """Use Claude to convert NL to SQL."""
    if not client:
        return _fallback_nl_to_sql(nl_query)

    try:
        response = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=512,
            system=NL_TO_SQL_SYSTEM,
            messages=[{"role": "user", "content": nl_query}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        data = json.loads(text)
        return data.get("sql", ""), data.get("explanation", "")
    except Exception as e:
        logger.error(f"NL-to-SQL generation failed: {e}")
        return _fallback_nl_to_sql(nl_query)


def _fallback_nl_to_sql(nl_query: str) -> tuple[str, str]:
    """Template-based fallback when AI is unavailable."""
    lower = nl_query.lower()

    if "high" in lower and "severity" in lower:
        if "last 7 days" in lower or "past week" in lower:
            return (
                "SELECT incident_id, created_at, triage_severity, triage_severity_score, lat, lng, status FROM incidents WHERE triage_severity IN ('high','critical') AND created_at >= datetime('now', '-7 days') ORDER BY created_at DESC LIMIT 100",
                "High-severity incidents from the last 7 days",
            )
        return (
            "SELECT incident_id, created_at, triage_severity, triage_severity_score, lat, lng, status FROM incidents WHERE triage_severity IN ('high','critical') ORDER BY created_at DESC LIMIT 100",
            "All high-severity incidents",
        )

    if "count" in lower or "how many" in lower:
        if "severity" in lower:
            return (
                "SELECT triage_severity, COUNT(*) as count FROM incidents GROUP BY triage_severity ORDER BY count DESC",
                "Incident counts by severity level",
            )
        return (
            "SELECT COUNT(*) as total_incidents FROM incidents",
            "Total incident count",
        )

    if "alert" in lower:
        return (
            "SELECT a.alert_id, a.incident_id, a.alert_channel, a.trigger_reason, a.sent_at, a.ack_status FROM alerts a ORDER BY a.sent_at DESC LIMIT 50",
            "Recent alerts",
        )

    if "recent" in lower or "latest" in lower:
        return (
            "SELECT incident_id, created_at, triage_severity, triage_severity_score, triage_summary, status FROM incidents ORDER BY created_at DESC LIMIT 20",
            "Most recent incidents",
        )

    # Default: show summary
    return (
        "SELECT triage_severity, status, COUNT(*) as count FROM incidents GROUP BY triage_severity, status ORDER BY count DESC",
        "Summary of incidents by severity and status",
    )


def _summarize_results(query: str, results: list[dict], explanation: str) -> str:
    """Generate a human-readable summary of query results."""
    if not results:
        return f"No results found for: {explanation}"

    count = len(results)
    if count == 1 and len(results[0]) == 1:
        key = list(results[0].keys())[0]
        return f"{explanation}: **{results[0][key]}**"

    return f"{explanation}. Found **{count}** result(s)."
