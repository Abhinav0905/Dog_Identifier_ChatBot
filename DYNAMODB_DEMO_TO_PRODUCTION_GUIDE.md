# Dharamsala Animal Rescue Chatbot — DynamoDB Serverless Demo to Production Guide

## Overview

This guide transitions the local FastAPI prototype to the Config 1 serverless architecture:
API Gateway HTTP API + Lambda (FastAPI/Mangum) + DynamoDB, with no VPC.

The largest single difference from the prototype is `database.py` — the entire module is
rewritten from `sqlite3` to `boto3` DynamoDB. All function signatures are preserved so the
rest of the application (services, app.py routes) requires only targeted changes. The admin
NL-to-SQL analytics system is rebuilt using a predefined DynamoDB query library rather than
arbitrary SQL execution.

---

## Prerequisites

- AWS account with admin access, AWS CLI v2 configured
- AWS SAM CLI installed (`brew install aws-sam-cli`)
- Python 3.12, `pip`
- GitHub repository for the chatbot code
- Domain name registered in Route 53 (e.g., `rescue.dharmasala.org`)
- Slack workspace with an incoming webhook configured

---

## Phase 1: Foundation — DynamoDB Tables, S3, Secrets Manager (Week 1)

### 1.1 DynamoDB Tables

Five tables, each created with `PAY_PER_REQUEST` billing (no capacity planning) and
point-in-time recovery enabled. These are defined in `template.yaml` (Phase 2) and created
automatically on first `sam deploy`. For manual creation before SAM is ready:

```bash
# incidents table — main data store
aws dynamodb create-table \
  --table-name dharmasala-incidents-prod \
  --billing-mode PAY_PER_REQUEST \
  --attribute-definitions \
    AttributeName=incident_id,AttributeType=S \
    AttributeName=image_sha256,AttributeType=S \
    AttributeName=status,AttributeType=S \
    AttributeName=triage_severity,AttributeType=S \
    AttributeName=created_at,AttributeType=S \
  --key-schema AttributeName=incident_id,KeyType=HASH \
  --global-secondary-indexes \
    '[
      {"IndexName":"sha256-index","KeySchema":[{"AttributeName":"image_sha256","KeyType":"HASH"}],"Projection":{"ProjectionType":"ALL"}},
      {"IndexName":"status-created_at-index","KeySchema":[{"AttributeName":"status","KeyType":"HASH"},{"AttributeName":"created_at","KeyType":"RANGE"}],"Projection":{"ProjectionType":"ALL"}},
      {"IndexName":"severity-created_at-index","KeySchema":[{"AttributeName":"triage_severity","KeyType":"HASH"},{"AttributeName":"created_at","KeyType":"RANGE"}],"Projection":{"ProjectionType":"ALL"}}
    ]' \
  --sse-specification Enabled=true \
  --point-in-time-recovery-specification PointInTimeRecoveryEnabled=true \
  --region ap-south-1

# chat_history table — session store with automatic TTL expiry (replaces Redis)
aws dynamodb create-table \
  --table-name dharmasala-chat-history-prod \
  --billing-mode PAY_PER_REQUEST \
  --attribute-definitions \
    AttributeName=session_id,AttributeType=S \
    AttributeName=message_id,AttributeType=S \
  --key-schema \
    AttributeName=session_id,KeyType=HASH \
    AttributeName=message_id,KeyType=RANGE \
  --sse-specification Enabled=true \
  --region ap-south-1

# Enable TTL on chat_history (24-hour session expiry)
aws dynamodb update-time-to-live \
  --table-name dharmasala-chat-history-prod \
  --time-to-live-specification Enabled=true,AttributeName=expires_at \
  --region ap-south-1

# alerts table
aws dynamodb create-table \
  --table-name dharmasala-alerts-prod \
  --billing-mode PAY_PER_REQUEST \
  --attribute-definitions \
    AttributeName=alert_id,AttributeType=S \
    AttributeName=incident_id,AttributeType=S \
    AttributeName=sent_at,AttributeType=S \
  --key-schema AttributeName=alert_id,KeyType=HASH \
  --global-secondary-indexes \
    '[{"IndexName":"incident_id-sent_at-index","KeySchema":[{"AttributeName":"incident_id","KeyType":"HASH"},{"AttributeName":"sent_at","KeyType":"RANGE"}],"Projection":{"ProjectionType":"ALL"}}]' \
  --sse-specification Enabled=true \
  --region ap-south-1

# triage_events table
aws dynamodb create-table \
  --table-name dharmasala-triage-events-prod \
  --billing-mode PAY_PER_REQUEST \
  --attribute-definitions \
    AttributeName=event_id,AttributeType=S \
    AttributeName=incident_id,AttributeType=S \
    AttributeName=created_at,AttributeType=S \
  --key-schema AttributeName=event_id,KeyType=HASH \
  --global-secondary-indexes \
    '[{"IndexName":"incident_id-created_at-index","KeySchema":[{"AttributeName":"incident_id","KeyType":"HASH"},{"AttributeName":"created_at","KeyType":"RANGE"}],"Projection":{"ProjectionType":"ALL"}}]' \
  --sse-specification Enabled=true \
  --region ap-south-1

# admin_query_audit table
aws dynamodb create-table \
  --table-name dharmasala-admin-audit-prod \
  --billing-mode PAY_PER_REQUEST \
  --attribute-definitions AttributeName=query_id,AttributeType=S \
  --key-schema AttributeName=query_id,KeyType=HASH \
  --sse-specification Enabled=true \
  --region ap-south-1
```

**Verify tables are ACTIVE before proceeding:**

```bash
aws dynamodb describe-table --table-name dharmasala-incidents-prod \
  --query 'Table.TableStatus' --region ap-south-1
# Expected: "ACTIVE"
```

### 1.2 Secrets Manager

Config 1 stores only one secret — no database password (DynamoDB uses IAM authentication).

```bash
aws secretsmanager create-secret \
  --name dharmasala/prod/secrets \
  --region ap-south-1 \
  --secret-string '{
    "SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/..."
  }'
```

Note the secret ARN. Set it as `SECRET_ARN` in GitHub repository secrets for CI/CD (Phase 5).

### 1.3 S3 Image Bucket

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET="dharmasala-images-prod-${ACCOUNT_ID}"

aws s3 mb s3://${BUCKET} --region ap-south-1

aws s3api put-public-access-block \
  --bucket ${BUCKET} \
  --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

aws s3api put-bucket-encryption \
  --bucket ${BUCKET} \
  --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"aws:kms"}}]}'

# CORS for pre-signed PUT uploads from browser
aws s3api put-bucket-cors \
  --bucket ${BUCKET} \
  --cors-configuration '{
    "CORSRules": [{
      "AllowedMethods": ["PUT"],
      "AllowedOrigins": ["https://rescue.dharmasala.org"],
      "AllowedHeaders": ["*"],
      "MaxAgeSeconds": 300
    }]
  }'

aws s3api put-bucket-lifecycle-configuration \
  --bucket ${BUCKET} \
  --lifecycle-configuration '{
    "Rules": [{
      "ID": "Archive", "Status": "Enabled",
      "Transitions": [
        {"Days": 90, "StorageClass": "STANDARD_IA"},
        {"Days": 365, "StorageClass": "GLACIER"}
      ]
    }]
  }'
```

---

## Phase 2: Code Changes + SAM Template (Week 2)

### 2.1 `config.py` — Replace dotenv with Secrets Manager; add DynamoDB table names

```python
import os
import json
import boto3

def _load_secrets() -> dict:
    """Fetch from Secrets Manager in prod, fall back to .env locally."""
    secret_arn = os.environ.get("SECRET_ARN")
    if not secret_arn:
        from dotenv import load_dotenv
        load_dotenv()
        return {}
    client = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", "ap-south-1"))
    return json.loads(client.get_secret_value(SecretId=secret_arn)["SecretString"])

_secrets = _load_secrets()

# AWS environment (injected by SAM)
AWS_REGION           = os.environ.get("AWS_REGION", "ap-south-1")
ENVIRONMENT          = os.environ.get("ENVIRONMENT", "dev")
IMAGE_BUCKET         = os.environ.get("IMAGE_BUCKET", "")
SQS_ALERT_QUEUE_URL  = os.environ.get("SQS_ALERT_QUEUE_URL", "")
SECRET_ARN           = os.environ.get("SECRET_ARN", "")

# DynamoDB table names (injected by SAM as env vars)
INCIDENTS_TABLE      = os.environ.get("INCIDENTS_TABLE",     "dharmasala-incidents-prod")
CHAT_HISTORY_TABLE   = os.environ.get("CHAT_HISTORY_TABLE",  "dharmasala-chat-history-prod")
ALERTS_TABLE         = os.environ.get("ALERTS_TABLE",        "dharmasala-alerts-prod")
TRIAGE_EVENTS_TABLE  = os.environ.get("TRIAGE_EVENTS_TABLE", "dharmasala-triage-events-prod")
ADMIN_AUDIT_TABLE    = os.environ.get("ADMIN_AUDIT_TABLE",   "dharmasala-admin-audit-prod")

# Secrets
SLACK_WEBHOOK_URL    = _secrets.get("SLACK_WEBHOOK_URL") or os.getenv("SLACK_WEBHOOK_URL", "")
# ANTHROPIC_API_KEY not needed — using Bedrock IAM auth instead

# Local dev only (ignored in Lambda)
from pathlib import Path
BASE_DIR    = Path(__file__).parent
DB_PATH     = BASE_DIR / "dharmasala.db"
STORAGE_DIR = BASE_DIR / "storage"

# Unchanged thresholds
ESCALATION_SEVERITY_THRESHOLD  = 7
SIMILARITY_PHASH_THRESHOLD      = 10
SIMILARITY_EMBEDDING_THRESHOLD  = 0.85
MAX_IMAGE_SIZE_MB               = 10
ALLOWED_IMAGE_TYPES             = {"image/jpeg", "image/png", "image/webp", "image/gif"}
```

### 2.2 `database.py` — Complete rewrite from sqlite3 to DynamoDB

This is the largest single change. All function signatures are preserved — callers (app.py,
services/) are unaffected. The module detects whether it's running in Lambda (DynamoDB) or
locally (SQLite) via the presence of `config.INCIDENTS_TABLE` environment variable.

```python
"""
Database layer — DynamoDB in production, SQLite locally.
All public function signatures are identical to the original sqlite3 version.
"""

import uuid
import json
import os
from datetime import datetime, timezone, timedelta
from typing import Optional
import boto3
from boto3.dynamodb.conditions import Key, Attr
import config

# ── DynamoDB resource (module-level — reused across Lambda invocations) ──────
# No connection pooling required: DynamoDB is HTTP-based, not connection-based.
_dynamodb = None

def _get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=config.AWS_REGION)
    return _dynamodb

def _table(name: str):
    return _get_dynamodb().Table(name)

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _ttl_24h() -> int:
    """Unix epoch timestamp 24 hours from now (for DynamoDB TTL attribute)."""
    return int((datetime.now(timezone.utc) + timedelta(hours=24)).timestamp())

# ── Detect environment ────────────────────────────────────────────────────────
_USE_DYNAMODB = bool(os.environ.get("INCIDENTS_TABLE"))

if not _USE_DYNAMODB:
    # Local development: fall back to original sqlite3 implementation
    import sqlite3
    from pathlib import Path

    def _get_sqlite():
        conn = sqlite3.connect(str(config.DB_PATH))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

# ── Public API — DynamoDB implementations ─────────────────────────────────────

def init_db():
    """No-op in production (DynamoDB tables created by SAM). Runs SQLite DDL locally."""
    if _USE_DYNAMODB:
        return
    # ... original sqlite3 DDL for local dev ...


def create_incident(
    session_id: str,
    image_blob_path: str = None,   # interpreted as s3_key in production
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
    if not _USE_DYNAMODB:
        return _sqlite_create_incident(**locals())  # delegates to original sqlite3 logic

    incident_id = str(uuid.uuid4())
    now = _now()
    item = {
        "incident_id":          incident_id,
        "created_at":           now,
        "updated_at":           now,
        "reporter_session_id":  session_id,
        "image_s3_key":         image_blob_path or "",   # param name preserved for compatibility
        "image_sha256":         image_sha256 or "",
        "image_phash":          image_phash or "",
        "status":               status,
    }
    # Only write non-None optional fields (DynamoDB doesn't store null/None)
    if lat is not None:              item["lat"]                  = str(lat)   # DynamoDB Number via Decimal; use str for simplicity
    if lng is not None:              item["lng"]                  = str(lng)
    if location_source:              item["location_source"]      = location_source
    if triage_severity:              item["triage_severity"]      = triage_severity
    if triage_severity_score is not None: item["triage_severity_score"] = triage_severity_score
    if triage_confidence is not None:     item["triage_confidence"]     = str(triage_confidence)
    if triage_summary:               item["triage_summary"]       = triage_summary
    if distress_flags:               item["distress_flags"]       = distress_flags  # DynamoDB List
    if similar_incident_id:          item["similar_incident_id"]  = similar_incident_id
    if similarity_score is not None: item["similarity_score"]     = str(similarity_score)

    _table(config.INCIDENTS_TABLE).put_item(Item=item)
    return incident_id


def get_incident(incident_id: str) -> Optional[dict]:
    if not _USE_DYNAMODB:
        return _sqlite_get_incident(incident_id)

    resp = _table(config.INCIDENTS_TABLE).get_item(Key={"incident_id": incident_id})
    item = resp.get("Item")
    return dict(item) if item else None


def update_incident(incident_id: str, **kwargs):
    if not _USE_DYNAMODB:
        return _sqlite_update_incident(incident_id, **kwargs)

    kwargs["updated_at"] = _now()
    # Build UpdateExpression dynamically; prefix all names with # to avoid reserved word conflicts
    update_expr = "SET " + ", ".join(f"#attr_{k} = :val_{k}" for k in kwargs)
    expr_names  = {f"#attr_{k}": k for k in kwargs}
    expr_values = {f":val_{k}": v for k, v in kwargs.items()}
    _table(config.INCIDENTS_TABLE).update_item(
        Key={"incident_id": incident_id},
        UpdateExpression=update_expr,
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )


def find_by_sha256(sha256: str) -> Optional[dict]:
    if not _USE_DYNAMODB:
        return _sqlite_find_by_sha256(sha256)

    resp = _table(config.INCIDENTS_TABLE).query(
        IndexName="sha256-index",
        KeyConditionExpression=Key("image_sha256").eq(sha256),
        Limit=1,
        ScanIndexForward=False,
    )
    items = resp.get("Items", [])
    return dict(items[0]) if items else None


def find_all_phashes() -> list[dict]:
    """
    Scan for all phash values. Used by similarity.py for in-memory Hamming comparison.
    Acceptable for MVP (< 10,000 incidents). Replaced by Bedrock Knowledge Bases at scale.
    """
    if not _USE_DYNAMODB:
        return _sqlite_find_all_phashes()

    results = []
    table = _table(config.INCIDENTS_TABLE)
    scan_kwargs = {
        "ProjectionExpression": "incident_id, image_phash",
        "FilterExpression": Attr("image_phash").exists() & Attr("image_phash").ne(""),
    }
    # Paginate through all items (handles tables > 1 MB)
    while True:
        resp = table.scan(**scan_kwargs)
        results.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        scan_kwargs["ExclusiveStartKey"] = last

    return results  # each item: {"incident_id": "...", "image_phash": "..."}


def create_alert(incident_id: str, channel: str, reason: str) -> str:
    if not _USE_DYNAMODB:
        return _sqlite_create_alert(incident_id, channel, reason)

    alert_id = str(uuid.uuid4())
    _table(config.ALERTS_TABLE).put_item(Item={
        "alert_id":       alert_id,
        "incident_id":    incident_id,
        "alert_channel":  channel,
        "trigger_reason": reason,
        "sent_at":        _now(),
        "ack_status":     "pending",
    })
    return alert_id


def create_triage_event(
    incident_id: str, model_version: str, raw_output: str,
    postprocessed: str, latency_ms: int
) -> str:
    if not _USE_DYNAMODB:
        return _sqlite_create_triage_event(incident_id, model_version, raw_output, postprocessed, latency_ms)

    event_id = str(uuid.uuid4())
    _table(config.TRIAGE_EVENTS_TABLE).put_item(Item={
        "event_id":              event_id,
        "incident_id":           incident_id,
        "model_version":         model_version or "",
        "raw_output":            raw_output or "",
        "postprocessed_output":  postprocessed or "",
        "latency_ms":            latency_ms,
        "created_at":            _now(),
    })
    return event_id


def log_admin_query(admin_user: str, nl_query: str, sql: str, row_count: int, status: str) -> str:
    if not _USE_DYNAMODB:
        return _sqlite_log_admin_query(admin_user, nl_query, sql, row_count, status)

    query_id = str(uuid.uuid4())
    _table(config.ADMIN_AUDIT_TABLE).put_item(Item={
        "query_id":       query_id,
        "admin_user_id":  admin_user,
        "nl_query":       nl_query,
        "resolved_query": sql,   # PartiQL or description of predefined query
        "executed_at":    _now(),
        "row_count":      row_count,
        "status":         status,
    })
    return query_id


def save_chat_message(session_id: str, role: str, content: str):
    if not _USE_DYNAMODB:
        return _sqlite_save_chat_message(session_id, role, content)

    now = _now()
    # message_id is timestamp-prefixed so Query with ScanIndexForward=False gives newest first
    message_id = f"{now}#{uuid.uuid4()}"
    _table(config.CHAT_HISTORY_TABLE).put_item(Item={
        "session_id": session_id,
        "message_id": message_id,
        "role":       role,
        "content":    content,
        "created_at": now,
        "expires_at": _ttl_24h(),   # DynamoDB TTL — item auto-deleted after 24h
    })


def get_chat_history(session_id: str, limit: int = 20) -> list[dict]:
    if not _USE_DYNAMODB:
        return _sqlite_get_chat_history(session_id, limit)

    resp = _table(config.CHAT_HISTORY_TABLE).query(
        KeyConditionExpression=Key("session_id").eq(session_id),
        ScanIndexForward=False,   # newest first
        Limit=limit,
        ProjectionExpression="#r, content",
        ExpressionAttributeNames={"#r": "role"},
    )
    # Reverse to return chronological order (oldest first), matching original behaviour
    return list(reversed([{"role": i["role"], "content": i["content"]} for i in resp["Items"]]))


def execute_readonly_sql(sql: str) -> list[dict]:
    """
    In production: not called — admin_analytics.py uses execute_admin_query() instead.
    In local dev: original sqlite3 implementation runs as before.
    """
    if not _USE_DYNAMODB:
        return _sqlite_execute_readonly_sql(sql)
    raise NotImplementedError(
        "execute_readonly_sql is not available in DynamoDB mode. "
        "Use execute_admin_query() from admin_analytics.py."
    )


def get_incidents_list(limit: int = 100, status: str = None, severity: str = None) -> list[dict]:
    """
    Returns incidents filtered by optional status and/or severity, newest first.
    Uses GSI Query when a single filter is active; Scan with FilterExpression for combined filters.
    """
    if not _USE_DYNAMODB:
        return _sqlite_get_incidents_list(limit, status, severity)

    table = _table(config.INCIDENTS_TABLE)

    if status and not severity:
        # Efficient: GSI Query on status-created_at-index
        resp = table.query(
            IndexName="status-created_at-index",
            KeyConditionExpression=Key("status").eq(status),
            ScanIndexForward=False,
            Limit=limit,
        )
        return [dict(i) for i in resp["Items"]]

    if severity and not status:
        # Efficient: GSI Query on severity-created_at-index
        resp = table.query(
            IndexName="severity-created_at-index",
            KeyConditionExpression=Key("triage_severity").eq(severity),
            ScanIndexForward=False,
            Limit=limit,
        )
        return [dict(i) for i in resp["Items"]]

    # Both filters or neither: Scan with optional FilterExpression
    scan_kwargs: dict = {}
    if status and severity:
        scan_kwargs["FilterExpression"] = (
            Attr("status").eq(status) & Attr("triage_severity").eq(severity)
        )
    items = []
    while len(items) < limit:
        resp = table.scan(**scan_kwargs)
        items.extend(resp.get("Items", []))
        if not resp.get("LastEvaluatedKey"):
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    # Sort by created_at descending (Scan has no inherent order)
    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return [dict(i) for i in items[:limit]]


def get_alerts_list(limit: int = 100) -> list[dict]:
    if not _USE_DYNAMODB:
        return _sqlite_get_alerts_list(limit)

    resp = _table(config.ALERTS_TABLE).scan(Limit=limit)
    items = resp.get("Items", [])
    items.sort(key=lambda x: x.get("sent_at", ""), reverse=True)
    return [dict(i) for i in items[:limit]]
```

### 2.3 `services/admin_analytics.py` — Replace NL-to-SQL with predefined DynamoDB queries

The `NL_TO_SQL_SYSTEM` prompt (`admin_analytics.py:16-37`) references SQLite and SQL syntax that
has no equivalent in DynamoDB. The function `execute_readonly_sql()` (`database.py:229`) is not
available in DynamoDB mode. The admin analytics service is rebuilt in two parts:

**Part 1 — Intent classifier (Claude):** Claude classifies the admin's natural language question
into one of the predefined query types. This is simpler than NL-to-SQL — it only needs to return
a query key and any parameters.

**Part 2 — Query executor:** A Python dispatcher runs the appropriate DynamoDB operation and
returns results. Claude then summarises the results exactly as before.

```python
"""
Admin analytics — DynamoDB version.
Replaces NL-to-SQL with an intent-classification + predefined-query approach.
"""

import json
import logging
from typing import Optional
import boto3
from boto3.dynamodb.conditions import Key, Attr
from datetime import datetime, timezone, timedelta
import config
import database as db

logger = logging.getLogger("dharmasala.admin")

# ── Intent classification prompt ─────────────────────────────────────────────
NL_TO_INTENT_SYSTEM = """You are an admin query classifier for the Dharamsala Animal Rescue database.
Classify the user's question into ONE of these query types. Return only valid JSON.

Query types:
  "count_by_severity"    - Count of incidents grouped by severity level
  "count_by_status"      - Count of incidents grouped by status
  "recent_incidents"     - List the most recent incidents (optionally filtered by severity or status)
  "high_severity_recent" - High/critical incidents from the last N days
  "alerts_recent"        - Recent alert records
  "total_count"          - Total number of incidents

Response format:
{
  "query_type": "<one of the types above>",
  "params": {
    "severity": "<low|moderate|high|critical or null>",
    "status": "<new|in_progress|alerted|resolved or null>",
    "days": <integer or null>,
    "limit": <integer, max 100, default 20>
  },
  "explanation": "Brief description of what will be queried"
}"""


def process_nl_query(nl_query: str, admin_user: str = "admin") -> dict:
    intent = _classify_intent(nl_query)
    if not intent:
        db.log_admin_query(admin_user, nl_query, "", 0, "failed_classification")
        return {
            "query": nl_query,
            "sql_generated": "",
            "results": [],
            "row_count": 0,
            "summary": "Could not classify this question. Try asking about incident counts, "
                       "recent high-severity cases, alert history, or status breakdowns.",
        }

    try:
        results = _execute_query(intent)
        row_count = len(results)
        db.log_admin_query(admin_user, nl_query, intent["query_type"], row_count, "success")
        summary = _summarize_results(nl_query, results, intent.get("explanation", ""))
        return {
            "query": nl_query,
            "sql_generated": f"[DynamoDB: {intent['query_type']}]",
            "results": results[:100],
            "row_count": row_count,
            "summary": summary,
        }
    except Exception as e:
        logger.error("Admin query execution failed: %s", e)
        db.log_admin_query(admin_user, nl_query, intent.get("query_type", ""), 0, f"error: {e}")
        return {
            "query": nl_query,
            "sql_generated": "",
            "results": [],
            "row_count": 0,
            "summary": "Query execution error. Please try rephrasing.",
        }


def _classify_intent(nl_query: str) -> Optional[dict]:
    """Use Claude via Bedrock to classify intent into a predefined query type."""
    try:
        bedrock = boto3.client("bedrock-runtime", region_name=config.AWS_REGION)
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 256,
            "system": NL_TO_INTENT_SYSTEM,
            "messages": [{"role": "user", "content": nl_query}],
        })
        resp = bedrock.invoke_model(
            modelId="anthropic.claude-3-5-sonnet-20241022-v2:0",
            body=body,
        )
        text = json.loads(resp["body"].read())["content"][0]["text"].strip()
        return json.loads(text)
    except Exception as e:
        logger.warning("Intent classification failed, using fallback: %s", e)
        return _fallback_classify(nl_query)


def _fallback_classify(nl_query: str) -> dict:
    """Keyword-based fallback matching the original _fallback_nl_to_sql patterns."""
    lower = nl_query.lower()
    if "alert" in lower:
        return {"query_type": "alerts_recent", "params": {"limit": 50}, "explanation": "Recent alerts"}
    if ("high" in lower or "critical" in lower) and ("day" in lower or "week" in lower or "recent" in lower):
        return {"query_type": "high_severity_recent", "params": {"days": 7, "limit": 50}, "explanation": "High-severity incidents last 7 days"}
    if "count" in lower or "how many" in lower:
        if "status" in lower:
            return {"query_type": "count_by_status", "params": {}, "explanation": "Incident count by status"}
        return {"query_type": "count_by_severity", "params": {}, "explanation": "Incident count by severity"}
    if "recent" in lower or "latest" in lower or "last" in lower:
        return {"query_type": "recent_incidents", "params": {"limit": 20}, "explanation": "Most recent incidents"}
    return {"query_type": "count_by_severity", "params": {}, "explanation": "Incident summary by severity"}


def _execute_query(intent: dict) -> list[dict]:
    """Dispatch to the appropriate DynamoDB operation."""
    qtype  = intent["query_type"]
    params = intent.get("params", {})
    limit  = min(int(params.get("limit") or 20), 100)
    table  = boto3.resource("dynamodb", region_name=config.AWS_REGION).Table(config.INCIDENTS_TABLE)

    if qtype == "count_by_severity":
        counts = {}
        for sev in ("low", "moderate", "high", "critical"):
            resp = table.query(
                IndexName="severity-created_at-index",
                KeyConditionExpression=Key("triage_severity").eq(sev),
                Select="COUNT",
            )
            counts[sev] = resp["Count"]
        return [{"triage_severity": k, "count": v} for k, v in counts.items()]

    if qtype == "count_by_status":
        counts = {}
        for st in ("new", "in_progress", "alerted", "resolved"):
            resp = table.query(
                IndexName="status-created_at-index",
                KeyConditionExpression=Key("status").eq(st),
                Select="COUNT",
            )
            counts[st] = resp["Count"]
        return [{"status": k, "count": v} for k, v in counts.items()]

    if qtype == "recent_incidents":
        severity = params.get("severity")
        status   = params.get("status")
        return db.get_incidents_list(limit=limit, status=status, severity=severity)

    if qtype == "high_severity_recent":
        days      = int(params.get("days") or 7)
        cutoff    = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        results   = []
        for sev in ("high", "critical"):
            resp = table.query(
                IndexName="severity-created_at-index",
                KeyConditionExpression=Key("triage_severity").eq(sev) & Key("created_at").gte(cutoff),
                ScanIndexForward=False,
                Limit=limit,
            )
            results.extend(resp.get("Items", []))
        results.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return [dict(i) for i in results[:limit]]

    if qtype == "alerts_recent":
        return db.get_alerts_list(limit=limit)

    if qtype == "total_count":
        resp = table.scan(Select="COUNT")
        return [{"total_incidents": resp["Count"]}]

    return []


def _summarize_results(query: str, results: list[dict], explanation: str) -> str:
    if not results:
        return f"No results found for: {explanation}"
    count = len(results)
    if count == 1 and len(results[0]) == 1:
        key = list(results[0].keys())[0]
        return f"{explanation}: **{results[0][key]}**"
    return f"{explanation}. Found **{count}** result(s)."
```

**Response field compatibility:** The return dict preserves `sql_generated` as a key (set to
`"[DynamoDB: {query_type}]"`) so the admin dashboard frontend continues to work without changes.

### 2.4 `app.py` — Four targeted changes

**Change 1:** Add Mangum handler (before `if __name__ == "__main__":`)
```python
from mangum import Mangum
handler = Mangum(app, lifespan="off")
```

**Change 2:** Remove static file serving — delete `app.py:48–57`:
```python
# DELETE:
app.mount("/static", StaticFiles(...), name="static")

@app.get("/", response_class=HTMLResponse)
async def serve_ui(): ...

@app.get("/admin.html", response_class=HTMLResponse)
async def serve_admin(): ...
```

**Change 3:** Move `db.init_db()` to module level (Lambda initialises on import, not via
ASGI lifecycle events):
```python
# Replace the @app.on_event("startup") block with:
db.init_db()  # no-op in production, runs SQLite DDL locally
logger.info("Config loaded. Bucket: %s, Region: %s", config.IMAGE_BUCKET, config.AWS_REGION)
```

**Change 4:** Replace multipart image upload with two-step S3 pre-signed URL flow — identical
to the Config 2 change (see `SERVERLESS_DEMO_TO_PRODUCTION_GUIDE.md` Phase 2.1, Change 4 for
the full endpoint code). Lambda reads the image from S3 using the `s3_key` parameter, then
runs all existing triage logic unchanged. The `image_blob_path` parameter passed to
`db.create_incident()` becomes the S3 object key.

**Change 5:** Replace `admin_password` checks with Cognito JWT group validation — identical to
Config 2 (see `SERVERLESS_DEMO_TO_PRODUCTION_GUIDE.md` Phase 2.1, Change 5).

### 2.5 `services/alerts.py` — Publish to SQS

Replace the `_send_slack()` / `_send_webhook()` direct calls in `send_alert()` with an SQS
publish — identical to Config 2 (see `SERVERLESS_DEMO_TO_PRODUCTION_GUIDE.md` Phase 2.1).
Create `services/alert_dispatcher.py` as a standalone Lambda handler for SQS delivery.

### 2.6 `requirements.txt` — Add production dependencies

```
# Existing
fastapi
uvicorn
anthropic
imagehash
Pillow
requests
python-dotenv
python-multipart

# New for production
mangum                   # ASGI adapter for Lambda
boto3                    # AWS SDK (DynamoDB, S3, SQS, Secrets Manager, Bedrock)
aws-lambda-powertools    # Structured logging, X-Ray, custom metrics
python-jose[cryptography]  # JWT validation (for local token testing)
```

Remove `psycopg2-binary`, `sqlalchemy`, `mysql-connector-python` — not needed without a
relational DB. This keeps the Lambda package smaller and cold starts faster.

### 2.7 SAM Template (`template.yaml`)

```yaml
AWSTemplateFormatVersion: '2010-09-09'
Transform: AWS::Serverless-2016-10-31

Parameters:
  Environment:
    Type: String
    Default: prod
    AllowedValues: [dev, staging, prod]
  SecretArn:
    Type: String
  CognitoUserPoolId:
    Type: String
  CognitoUserPoolClientId:
    Type: String

Globals:
  Function:
    Runtime: python3.12
    Timeout: 60
    MemorySize: 512
    # No VpcConfig — Lambda runs in AWS-managed network
    Layers:
      - !Sub arn:aws:lambda:${AWS::Region}:017000801446:layer:AWSLambdaPowertoolsPythonV2:latest
    Environment:
      Variables:
        POWERTOOLS_SERVICE_NAME: dharmasala-chatbot
        LOG_LEVEL: INFO
        ENVIRONMENT: !Ref Environment

Resources:

  # ── API Gateway ──────────────────────────────────────────────────────────────
  ChatApi:
    Type: AWS::Serverless::HttpApi
    Properties:
      StageName: !Ref Environment
      Auth:
        DefaultAuthorizer: CognitoAuth
        Authorizers:
          CognitoAuth:
            JwtConfiguration:
              issuer: !Sub https://cognito-idp.${AWS::Region}.amazonaws.com/${CognitoUserPoolId}
              audience: [!Ref CognitoUserPoolClientId]

  # ── Main Lambda (FastAPI + Mangum, no VPC) ────────────────────────────────────
  ChatbotFunction:
    Type: AWS::Serverless::Function
    Properties:
      Handler: app.handler
      CodeUri: .
      # No VpcConfig — this is the key difference from Config 2
      Environment:
        Variables:
          SECRET_ARN:           !Ref SecretArn
          IMAGE_BUCKET:         !Ref ImageBucket
          SQS_ALERT_QUEUE_URL:  !Ref AlertQueue
          INCIDENTS_TABLE:      !Ref IncidentsTable
          CHAT_HISTORY_TABLE:   !Ref ChatHistoryTable
          ALERTS_TABLE:         !Ref AlertsTable
          TRIAGE_EVENTS_TABLE:  !Ref TriageEventsTable
          ADMIN_AUDIT_TABLE:    !Ref AdminAuditTable
      Policies:
        - AWSSecretsManagerGetSecretValuePolicy:
            SecretArn: !Ref SecretArn
        - S3CrudPolicy:
            BucketName: !Ref ImageBucket
        - SQSSendMessagePolicy:
            QueueName: !GetAtt AlertQueue.QueueName
        - DynamoDBCrudPolicy:
            TableName: !Ref IncidentsTable
        - DynamoDBCrudPolicy:
            TableName: !Ref ChatHistoryTable
        - DynamoDBCrudPolicy:
            TableName: !Ref AlertsTable
        - DynamoDBCrudPolicy:
            TableName: !Ref TriageEventsTable
        - DynamoDBCrudPolicy:
            TableName: !Ref AdminAuditTable
        - Statement:
            - Effect: Allow
              Action:
                - dynamodb:Query
                - dynamodb:Scan
              Resource:
                - !Sub ${IncidentsTable.Arn}/index/*
                - !Sub ${AlertsTable.Arn}/index/*
                - !Sub ${TriageEventsTable.Arn}/index/*
            - Effect: Allow
              Action: bedrock:InvokeModel
              Resource: "arn:aws:bedrock:*::foundation-model/anthropic.claude-3-5-sonnet-20241022-v2:0"
      Events:
        RootRoute:
          Type: HttpApi
          Properties: {ApiId: !Ref ChatApi, Path: /, Method: ANY}
        ProxyRoute:
          Type: HttpApi
          Properties: {ApiId: !Ref ChatApi, Path: /{proxy+}, Method: ANY}
      ReservedConcurrentExecutions: 50

  # Provisioned concurrency on triage endpoint (eliminates cold starts on primary path)
  ChatbotFunctionVersion:
    Type: AWS::Lambda::Version
    Properties:
      FunctionName: !Ref ChatbotFunction

  ChatbotProvisionedConcurrency:
    Type: AWS::Lambda::ProvisionedConcurrencyConfig
    Properties:
      FunctionName: !Ref ChatbotFunction
      Qualifier: !GetAtt ChatbotFunctionVersion.Version
      ProvisionedConcurrentExecutions: 2

  # ── Alert Dispatcher Lambda ───────────────────────────────────────────────────
  AlertDispatchFunction:
    Type: AWS::Serverless::Function
    Properties:
      Handler: services.alert_dispatcher.handler
      CodeUri: .
      Policies:
        - AWSSecretsManagerGetSecretValuePolicy:
            SecretArn: !Ref SecretArn
        - SQSPollerPolicy:
            QueueName: !GetAtt AlertQueue.QueueName
      Events:
        SQSTrigger:
          Type: SQS
          Properties:
            Queue: !GetAtt AlertQueue.Arn
            BatchSize: 10
            FunctionResponseTypes: [ReportBatchItemFailures]

  # ── DynamoDB Tables ────────────────────────────────────────────────────────────
  IncidentsTable:
    Type: AWS::DynamoDB::Table
    Properties:
      BillingMode: PAY_PER_REQUEST
      PointInTimeRecoverySpecification:
        PointInTimeRecoveryEnabled: true
      SSESpecification:
        SSEEnabled: true
      AttributeDefinitions:
        - {AttributeName: incident_id,     AttributeType: S}
        - {AttributeName: image_sha256,    AttributeType: S}
        - {AttributeName: status,          AttributeType: S}
        - {AttributeName: triage_severity, AttributeType: S}
        - {AttributeName: created_at,      AttributeType: S}
      KeySchema:
        - {AttributeName: incident_id, KeyType: HASH}
      GlobalSecondaryIndexes:
        - IndexName: sha256-index
          KeySchema: [{AttributeName: image_sha256, KeyType: HASH}]
          Projection: {ProjectionType: ALL}
        - IndexName: status-created_at-index
          KeySchema:
            - {AttributeName: status,     KeyType: HASH}
            - {AttributeName: created_at, KeyType: RANGE}
          Projection: {ProjectionType: ALL}
        - IndexName: severity-created_at-index
          KeySchema:
            - {AttributeName: triage_severity, KeyType: HASH}
            - {AttributeName: created_at,      KeyType: RANGE}
          Projection: {ProjectionType: ALL}

  ChatHistoryTable:
    Type: AWS::DynamoDB::Table
    Properties:
      BillingMode: PAY_PER_REQUEST
      TimeToLiveSpecification:
        AttributeName: expires_at
        Enabled: true
      SSESpecification:
        SSEEnabled: true
      AttributeDefinitions:
        - {AttributeName: session_id, AttributeType: S}
        - {AttributeName: message_id, AttributeType: S}
      KeySchema:
        - {AttributeName: session_id, KeyType: HASH}
        - {AttributeName: message_id, KeyType: RANGE}

  AlertsTable:
    Type: AWS::DynamoDB::Table
    Properties:
      BillingMode: PAY_PER_REQUEST
      SSESpecification:
        SSEEnabled: true
      AttributeDefinitions:
        - {AttributeName: alert_id,    AttributeType: S}
        - {AttributeName: incident_id, AttributeType: S}
        - {AttributeName: sent_at,     AttributeType: S}
      KeySchema:
        - {AttributeName: alert_id, KeyType: HASH}
      GlobalSecondaryIndexes:
        - IndexName: incident_id-sent_at-index
          KeySchema:
            - {AttributeName: incident_id, KeyType: HASH}
            - {AttributeName: sent_at,     KeyType: RANGE}
          Projection: {ProjectionType: ALL}

  TriageEventsTable:
    Type: AWS::DynamoDB::Table
    Properties:
      BillingMode: PAY_PER_REQUEST
      SSESpecification:
        SSEEnabled: true
      AttributeDefinitions:
        - {AttributeName: event_id,    AttributeType: S}
        - {AttributeName: incident_id, AttributeType: S}
        - {AttributeName: created_at,  AttributeType: S}
      KeySchema:
        - {AttributeName: event_id, KeyType: HASH}
      GlobalSecondaryIndexes:
        - IndexName: incident-created_at-index
          KeySchema:
            - {AttributeName: incident_id, KeyType: HASH}
            - {AttributeName: created_at,  KeyType: RANGE}
          Projection: {ProjectionType: ALL}

  AdminAuditTable:
    Type: AWS::DynamoDB::Table
    Properties:
      BillingMode: PAY_PER_REQUEST
      SSESpecification:
        SSEEnabled: true
      AttributeDefinitions:
        - {AttributeName: query_id, AttributeType: S}
      KeySchema:
        - {AttributeName: query_id, KeyType: HASH}

  # ── S3 Image Bucket ─────────────────────────────────────────────────────────────
  ImageBucket:
    Type: AWS::S3::Bucket
    Properties:
      BucketEncryption:
        ServerSideEncryptionConfiguration:
          - ServerSideEncryptionByDefault: {SSEAlgorithm: aws:kms}
      PublicAccessBlockConfiguration:
        BlockPublicAcls: true
        BlockPublicPolicy: true
        IgnorePublicAcls: true
        RestrictPublicBuckets: true
      CorsConfiguration:
        CorsRules:
          - AllowedMethods: [PUT]
            AllowedOrigins: ['*']   # Tighten to Amplify domain after Phase 3
            AllowedHeaders: ['*']
            MaxAge: 300
      LifecycleConfiguration:
        Rules:
          - Id: Archive
            Status: Enabled
            Transitions:
              - {TransitionInDays: 90,  StorageClass: STANDARD_IA}
              - {TransitionInDays: 365, StorageClass: GLACIER}

  # ── SQS Alert Queue ─────────────────────────────────────────────────────────────
  AlertQueue:
    Type: AWS::SQS::Queue
    Properties:
      VisibilityTimeout: 300
      RedrivePolicy:
        deadLetterTargetArn: !GetAtt AlertDLQ.Arn
        maxReceiveCount: 3

  AlertDLQ:
    Type: AWS::SQS::Queue

  # ── CloudWatch Alarms ────────────────────────────────────────────────────────────
  DLQDepthAlarm:
    Type: AWS::CloudWatch::Alarm
    Properties:
      AlarmName: !Sub dharmasala-dlq-depth-${Environment}
      MetricName: ApproximateNumberOfMessagesVisible
      Namespace: AWS/SQS
      Dimensions:
        - Name: QueueName
          Value: !GetAtt AlertDLQ.QueueName
      Threshold: 0
      ComparisonOperator: GreaterThanThreshold
      EvaluationPeriods: 1
      Period: 60
      Statistic: Sum
      TreatMissingData: notBreaching

Outputs:
  ApiEndpoint:
    Value: !Sub https://${ChatApi}.execute-api.${AWS::Region}.amazonaws.com/${Environment}
  ImageBucketName:
    Value: !Ref ImageBucket
  AlertQueueUrl:
    Value: !Ref AlertQueue
  IncidentsTableName:
    Value: !Ref IncidentsTable
```

### 2.8 Deploy and Validate

```bash
sam build
sam deploy --guided --config-env prod
# On first run, sam deploy --guided prompts for parameter values and creates samconfig.toml

# Test Lambda function directly
aws lambda invoke \
  --function-name dharmasala-chatbot-prod-ChatbotFunction \
  --payload '{"version":"2.0","routeKey":"GET /health","rawPath":"/health","headers":{},"requestContext":{"http":{"method":"GET","path":"/health"}}}' \
  /tmp/response.json && cat /tmp/response.json

# Write a test item to DynamoDB and read it back
aws dynamodb put-item \
  --table-name dharmasala-incidents-prod \
  --item '{"incident_id":{"S":"test-001"},"created_at":{"S":"2024-01-01T00:00:00Z"},"status":{"S":"new"},"triage_severity":{"S":"low"}}' \
  --region ap-south-1

aws dynamodb get-item \
  --table-name dharmasala-incidents-prod \
  --key '{"incident_id":{"S":"test-001"}}' \
  --region ap-south-1

# Clean up test item
aws dynamodb delete-item \
  --table-name dharmasala-incidents-prod \
  --key '{"incident_id":{"S":"test-001"}}' \
  --region ap-south-1
```

---

## Phase 3: API Gateway, CloudFront, WAF, Amplify, Cognito (Week 3)

Identical to Config 2. See `SERVERLESS_DEMO_TO_PRODUCTION_GUIDE.md` Phase 3 for:
- Cognito user pool creation and group setup (volunteer / admin)
- CloudFront distribution with WAF WebACL (OWASP rules + rate limiting)
- Amplify Hosting connected to GitHub, `amplify.yml` build config
- `static/app.js` updates for API base URL and two-step image upload

The only difference: the SAM `ChatApi` JWT authorizer parameters reference the new Cognito
pool IDs, which are passed as SAM parameters (already in the template above).

---

## Phase 4: Observability (Week 4)

Identical to Config 2. Add Lambda Powertools decorators to `app.py` and emit custom DynamoDB
metrics in addition to triage metrics:

```python
from aws_lambda_powertools import Logger, Tracer, Metrics
from aws_lambda_powertools.metrics import MetricUnit

logger  = Logger()
tracer  = Tracer()
metrics = Metrics(namespace="DharamsalaChatbot")

@metrics.log_metrics(capture_cold_start_metric=True)
@tracer.capture_lambda_handler
def handler(event, context):
    return mangum_handler(event, context)

mangum_handler = Mangum(app, lifespan="off")
```

**Additional DynamoDB-specific metrics to emit in `database.py`:**
```python
# In find_all_phashes() — track Scan cost
metrics.add_metric("PhashScanItemCount", MetricUnit.Count, len(results))

# In get_chat_history() — track session size
metrics.add_metric("ChatHistoryLength", MetricUnit.Count, len(resp["Items"]))
```

**CloudWatch alarm additions specific to Config 1:**

| Metric | Threshold | Action |
|---|---|---|
| DynamoDB `SystemErrors` (any table) | > 0 | SNS → email |
| DynamoDB `ConsumedWriteCapacityUnits` | > 5,000 / 5 min | SNS → warning (unexpected traffic spike) |
| `PhashScanItemCount` (custom) | > 5,000 | SNS → warning (time to evaluate Bedrock Knowledge Bases upgrade) |

---

## Phase 5: CI/CD — GitHub Actions + SAM (Week 5)

Identical to Config 2, with one change: the SAM deploy command has no VPC parameters.

```yaml
# .github/workflows/deploy.yml
name: Test and Deploy
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

permissions:
  id-token: write
  contents: read

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: '3.12'}
      - run: pip install -r requirements.txt
      - run: python -m pytest tests/ -v --tb=short

  deploy:
    needs: test
    runs-on: ubuntu-latest
    if: github.ref == 'refs/heads/main' && github.event_name == 'push'
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: '3.12'}
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE_ARN }}
          aws-region: ap-south-1
      - uses: aws-actions/setup-sam@v2
      - run: sam build
      - run: |
          sam deploy \
            --config-env prod \
            --no-confirm-changeset \
            --no-fail-on-empty-changeset \
            --parameter-overrides \
              Environment=prod \
              SecretArn=${{ secrets.SECRET_ARN }} \
              CognitoUserPoolId=${{ secrets.COGNITO_USER_POOL_ID }} \
              CognitoUserPoolClientId=${{ secrets.COGNITO_CLIENT_ID }}
          # Note: no VpcId, PrivateSubnetIds, LambdaSecurityGroupId vs Config 2
```

**GitHub Secrets required:** `AWS_DEPLOY_ROLE_ARN`, `SECRET_ARN`, `COGNITO_USER_POOL_ID`,
`COGNITO_CLIENT_ID`. Fewer secrets than Config 2 (no VPC or DB proxy parameters).

---

## Phase 6: Testing and Go-Live (Week 6)

### 6.1 DynamoDB-Specific Validation

```bash
# Verify all GSIs are ACTIVE (GSI creation is asynchronous)
for idx in sha256-index status-created_at-index severity-created_at-index; do
  aws dynamodb describe-table --table-name dharmasala-incidents-prod \
    --query "Table.GlobalSecondaryIndexes[?IndexName=='${idx}'].IndexStatus" \
    --output text --region ap-south-1
done
# Expected: ACTIVE for each

# Verify TTL is enabled on chat_history
aws dynamodb describe-time-to-live \
  --table-name dharmasala-chat-history-prod \
  --query 'TimeToLiveDescription.TimeToLiveStatus' \
  --region ap-south-1
# Expected: ENABLED

# Verify PITR is enabled on incidents
aws dynamodb describe-continuous-backups \
  --table-name dharmasala-incidents-prod \
  --query 'ContinuousBackupsDescription.PointInTimeRecoveryDescription.PointInTimeRecoveryStatus' \
  --region ap-south-1
# Expected: ENABLED

# Test phash scan end-to-end (requires at least 1 incident in DB)
# Submit a test triage request and confirm similarity check ran without error
```

### 6.2 Admin Analytics Validation

```bash
# Test intent classification for each predefined query type
curl -X POST https://<api-endpoint>/v1/admin/query \
  -H "Authorization: Bearer <admin-jwt>" \
  -H "Content-Type: application/json" \
  -d '{"query": "how many incidents by severity"}'
# Expected: results array with severity breakdown

curl -X POST https://<api-endpoint>/v1/admin/query \
  -H "Authorization: Bearer <admin-jwt>" \
  -H "Content-Type: application/json" \
  -d '{"query": "show high severity incidents from the last 7 days"}'
# Expected: results filtered to high/critical created after cutoff
```

### 6.3 Go-Live Checklist

**DynamoDB:**
- [ ] All 5 tables in ACTIVE state
- [ ] All GSIs in ACTIVE state
- [ ] TTL enabled on `chat_history`
- [ ] Point-in-time recovery enabled on `incidents`
- [ ] Test write/read cycle against each table succeeds

**Lambda:**
- [ ] `GET /health` returns 200 with `ai_configured: true`
- [ ] Pre-signed URL flow: upload URL issued, image lands in S3, triage returns result
- [ ] Chat history persists across two requests in same session and expires after 24h TTL
- [ ] Alert fires to Slack on severity >= 7
- [ ] Admin query returns results for all 6 predefined query types
- [ ] Admin endpoint returns 403 without `admin` Cognito group

**Infrastructure:**
- [ ] Lambda function has no VPC configuration (confirm in console — no VPC shown)
- [ ] Cognito user pool created; admin user in `admin` group
- [ ] CloudFront + WAF active; rate limiting fires at 1000 req/5 min
- [ ] Amplify Hosting serving static UI from production domain
- [ ] Route 53 alias pointing to CloudFront
- [ ] SQS DLQ alarm triggers when DLQ has messages

**Testing:**
- [ ] All `test_unit.py` tests pass against staging stack
- [ ] Load test: P95 < 4s text, P95 < 8s image at 50 concurrent users
- [ ] `PhashScanItemCount` metric visible in CloudWatch after 10+ incidents created

**Go-Live:**
- [ ] DNS cutover: Route 53 record updated to CloudFront distribution
- [ ] Smoke test via production domain
- [ ] Monitor error rate and latency for 30 minutes post-cutover

---

## Code Changes Summary

| File | Nature of change | Scope |
|---|---|---|
| `config.py` | Replace dotenv with Secrets Manager; remove DB path vars; add DynamoDB table name env vars | Rewrite |
| `database.py` | Replace sqlite3 with DynamoDB boto3 resource; add environment detection for local dev | Full rewrite |
| `services/admin_analytics.py` | Replace NL-to-SQL system prompt and `execute_readonly_sql()` with intent classifier + predefined DynamoDB query dispatcher | Rewrite |
| `app.py` | Add Mangum; remove static serving; replace image upload with pre-signed URL flow; replace password auth with Cognito group check; move `init_db()` to module level | Targeted edits |
| `services/alerts.py` | Replace direct Slack call with SQS publish | Small edit |
| `static/app.js` | Update API base URL; implement two-step image upload | Targeted edit |
| `requirements.txt` | Add mangum, boto3, aws-lambda-powertools; remove psycopg2, sqlalchemy | Additions/removals |
| New: `template.yaml` | SAM template with 5 DynamoDB tables, Lambda, API Gateway, SQS, S3 | New file |
| New: `samconfig.toml` | SAM deploy configuration per environment | New file |
| New: `.github/workflows/deploy.yml` | GitHub Actions CI/CD (4 secrets vs 7 for Config 2) | New file |
| New: `services/alert_dispatcher.py` | Standalone Lambda for SQS alert delivery | New file |
| New: `amplify.yml` | Amplify Hosting build configuration | New file |

---

## Rollback Plan

| Scenario | Rollback action |
|---|---|
| Lambda regression | `sam deploy` with previous artifact (SAM stores in S3); redeploy last working commit via GitHub Actions |
| DynamoDB data corruption | Point-in-time restore: `aws dynamodb restore-table-to-point-in-time --source-table-name dharmasala-incidents-prod --target-table-name dharmasala-incidents-restored --restore-date-time <epoch>` |
| GSI hot partition (all incidents same severity) | Add jitter to `triage_severity` values or switch to Scan; DynamoDB on-demand handles burst automatically |
| DNS cutover issue | Update Route 53 alias back to previous endpoint |
| SQS DLQ fills | Re-drive: `aws sqs start-message-move-task --source-arn <dlq-arn> --destination-arn <main-queue-arn>` |
| Admin analytics regression | Fallback to `_fallback_classify()` (keyword matching) is always active if Bedrock call fails |

---

## Growth Path: Restoring Full SQL Analytics via Athena

When predefined queries are insufficient, enable DynamoDB Streams → S3 → Athena to restore
arbitrary NL-to-SQL capability without changing the core architecture:

```bash
# Step 1: Enable DynamoDB Streams on incidents table
aws dynamodb update-table \
  --table-name dharmasala-incidents-prod \
  --stream-specification StreamEnabled=true,StreamViewType=NEW_AND_OLD_IMAGES \
  --region ap-south-1

# Step 2: Add Kinesis Firehose → S3 Parquet delivery (via Lambda or native integration)
# Step 3: Create Athena table over S3 Parquet
# Step 4: Update admin_analytics.py to call athena.start_query_execution() for complex queries
```

The `execute_readonly_sql()` function in `database.py` can be re-implemented against Athena,
restoring the full NL-to-SQL system prompt from `admin_analytics.py` with only the backend
execution target changed.

---

## Timeline Summary

| Week | Phase | Deliverable |
|---|---|---|
| 1 | Foundation | DynamoDB tables + GSIs, S3 bucket, Secrets Manager |
| 2 | Lambda + SAM | Code changes (database.py, admin_analytics.py, app.py), SAM template, deploy + validate |
| 3 | Networking + Auth | CloudFront + WAF, Amplify Hosting, Cognito, Route 53 |
| 4 | Observability | Lambda Powertools, CloudWatch dashboard + DynamoDB-specific alarms |
| 5 | CI/CD | GitHub Actions OIDC deploy, staging stack, PR previews |
| 6 | Testing + Go-Live | DynamoDB validation, load test, admin analytics smoke test, DNS cutover |
