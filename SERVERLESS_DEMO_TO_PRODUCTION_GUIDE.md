# Dharamsala Animal Rescue Chatbot — Serverless Demo to Production Guide

## Overview

This guide transitions the local FastAPI prototype to the serverless AWS architecture:
API Gateway HTTP API + Lambda (FastAPI/Mangum) + Aurora Serverless v2 PostgreSQL + pgvector.

It is structured as sequential phases. Each phase is independently deployable and testable before
moving to the next. The application continues to run locally (against SQLite) throughout the
migration — only production infrastructure changes between phases.

---

## Prerequisites

- AWS account with admin access, AWS CLI v2 configured
- AWS SAM CLI installed (`brew install aws-sam-cli`)
- Python 3.12, `pip`
- GitHub repository with the chatbot code
- Domain name registered in Route 53 (e.g., `rescue.dharmasala.org`)
- Slack workspace with an incoming webhook configured

---

## Phase 1: Foundation — VPC, Aurora, S3, Secrets Manager (Week 1)

### 1.1 VPC and Networking

Lambda and Aurora must share a private VPC. Create this first; every other resource references it.

```bash
# Use SAM or the AWS CLI. Minimum required:
# - 1 VPC
# - 2 private subnets (different AZs) — for Lambda and Aurora
# - 2 public subnets (different AZs) — for NAT Gateway or VPC endpoints
# - Security groups: one for Lambda (outbound only), one for Aurora (inbound from Lambda SG)

aws ec2 create-vpc --cidr-block 10.0.0.0/16 --region ap-south-1
# Create subnets, internet gateway, route tables per standard VPC setup
# OR use the SAM template in Phase 2 which provisions VPC resources automatically
```

**Recommended:** Define the VPC in `template.yaml` from Phase 2 and deploy everything together.
The checklist below applies regardless:

- [ ] VPC with CIDR `10.0.0.0/16`
- [ ] 2 private subnets in separate AZs (Lambda and Aurora reside here)
- [ ] 2 public subnets (for NAT Gateway if not using VPC endpoints)
- [ ] Security group `sg-lambda`: allows all outbound, no inbound
- [ ] Security group `sg-aurora`: allows inbound `5432` from `sg-lambda` only

**Cost note:** NAT Gateway costs ~$32/month. Replace it with VPC Interface Endpoints for S3,
Secrets Manager, SQS, and Bedrock to eliminate this cost entirely. Add endpoints via:

```bash
# S3 Gateway endpoint (free)
aws ec2 create-vpc-endpoint --vpc-id vpc-xxx --service-name com.amazonaws.ap-south-1.s3 --route-table-ids rtb-xxx

# Interface endpoints for Secrets Manager, SQS, Bedrock (~$7/month each — evaluate trade-off)
aws ec2 create-vpc-endpoint --vpc-endpoint-type Interface --vpc-id vpc-xxx \
  --service-name com.amazonaws.ap-south-1.secretsmanager \
  --subnet-ids subnet-xxx subnet-yyy --security-group-ids sg-lambda
```

### 1.2 Secrets Manager

Migrate all values from `.env` into a single secret. Lambda will fetch this at cold-start.

```bash
aws secretsmanager create-secret \
  --name dharmasala/prod/secrets \
  --region ap-south-1 \
  --secret-string '{
    "ANTHROPIC_API_KEY": "sk-ant-...",
    "SLACK_WEBHOOK_URL": "https://hooks.slack.com/...",
    "DB_PASSWORD": "<strong-generated-password>"
  }'
```

Note the secret ARN. It is referenced in the SAM template and injected into Lambda as
`SECRET_ARN` environment variable. The `ADMIN_PASSWORD` from `config.py:15` is replaced by
Cognito in Phase 3 — do not migrate it here.

### 1.3 Aurora Serverless v2 PostgreSQL

Create the Aurora cluster in the private subnets. Aurora requires a DB subnet group first.

```bash
# Create DB subnet group
aws rds create-db-subnet-group \
  --db-subnet-group-name dharmasala-db-subnets \
  --db-subnet-group-description "Aurora subnets" \
  --subnet-ids subnet-private-az1 subnet-private-az2

# Create Aurora Serverless v2 cluster
aws rds create-db-cluster \
  --db-cluster-identifier dharmasala-cluster \
  --engine aurora-postgresql \
  --engine-version 15.4 \
  --serverless-v2-scaling-configuration MinCapacity=0.5,MaxCapacity=8 \
  --master-username dbadmin \
  --manage-master-user-password \
  --db-subnet-group-name dharmasala-db-subnets \
  --vpc-security-group-ids sg-aurora \
  --enable-iam-database-authentication \
  --region ap-south-1

# Create the writer instance (required even for Serverless v2)
aws rds create-db-instance \
  --db-instance-identifier dharmasala-writer \
  --db-cluster-identifier dharmasala-cluster \
  --db-instance-class db.serverless \
  --engine aurora-postgresql
```

**Enable pgvector extension** (run once after cluster is available):

```bash
# Connect via psql or the RDS Query Editor
psql -h <cluster-endpoint> -U dbadmin -d postgres -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

### 1.4 PostgreSQL Schema Migration

The SQLite schema from `database.py:29–98` maps to PostgreSQL with mechanical type changes.
Run this DDL against the new Aurora cluster to create all five tables:

```sql
-- Enable pgvector (if not already done above)
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS incidents (
    incident_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reporter_session_id TEXT,
    image_s3_key    TEXT,                        -- replaces image_blob_path (local path)
    image_sha256    TEXT,
    image_phash     TEXT,
    lat             DOUBLE PRECISION,
    lng             DOUBLE PRECISION,
    location_source TEXT,
    location_accuracy DOUBLE PRECISION,
    triage_severity TEXT,
    triage_severity_score INTEGER,
    triage_confidence DOUBLE PRECISION,
    triage_summary  TEXT,
    distress_flags  JSONB,                       -- replaces TEXT JSON string
    similar_incident_id UUID,
    similarity_score DOUBLE PRECISION,
    status          TEXT DEFAULT 'new',
    embedding       vector(1536)                 -- NEW: pgvector column for image embeddings
);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id     UUID NOT NULL REFERENCES incidents(incident_id),
    alert_channel   TEXT NOT NULL,
    trigger_reason  TEXT,
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ack_status      TEXT DEFAULT 'pending',
    ack_by          TEXT,
    ack_at          TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS triage_events (
    event_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id     UUID NOT NULL REFERENCES incidents(incident_id),
    model_version   TEXT,
    raw_output      TEXT,
    postprocessed_output TEXT,
    latency_ms      INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS admin_query_audit (
    query_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    admin_user_id   TEXT,
    nl_query        TEXT,
    resolved_sql    TEXT,
    executed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    row_count       INTEGER,
    status          TEXT
);

CREATE TABLE IF NOT EXISTS chat_history (
    id              BIGSERIAL PRIMARY KEY,
    session_id      TEXT NOT NULL,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes (same as SQLite, PostgreSQL syntax)
CREATE INDEX IF NOT EXISTS idx_incidents_sha256   ON incidents(image_sha256);
CREATE INDEX IF NOT EXISTS idx_incidents_phash    ON incidents(image_phash);
CREATE INDEX IF NOT EXISTS idx_incidents_status   ON incidents(status);
CREATE INDEX IF NOT EXISTS idx_incidents_severity ON incidents(triage_severity);
CREATE INDEX IF NOT EXISTS idx_chat_session       ON chat_history(session_id);

-- pgvector ANN index (IVFFlat — build after first ~1000 rows exist for best list tuning)
-- CREATE INDEX ON incidents USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
-- Run the above index creation separately once you have data to train the quantizer.
```

**Key schema change:** `image_blob_path TEXT` (local filesystem path) becomes `image_s3_key TEXT`
(S3 object key). Update any references in existing code that read `incident["image_blob_path"]`.

### 1.5 RDS Proxy

RDS Proxy pools Lambda → Aurora connections. Without it, connection exhaustion under concurrent
Lambda invocations will cause query failures.

```bash
aws rds create-db-proxy \
  --db-proxy-name dharmasala-proxy \
  --engine-family POSTGRESQL \
  --auth '[{"AuthScheme":"SECRETS","SecretArn":"<aurora-master-secret-arn>","IAMAuth":"REQUIRED"}]' \
  --role-arn arn:aws:iam::<account>:role/rds-proxy-role \
  --vpc-subnet-ids subnet-private-az1 subnet-private-az2 \
  --vpc-security-group-ids sg-aurora \
  --region ap-south-1

# Register Aurora cluster as proxy target
aws rds register-db-proxy-targets \
  --db-proxy-name dharmasala-proxy \
  --db-cluster-identifiers dharmasala-cluster
```

Lambda connects to `<proxy-endpoint>:5432` rather than the Aurora cluster endpoint. The proxy
endpoint is injected as `DB_PROXY_ENDPOINT` environment variable.

### 1.6 S3 Image Bucket

```bash
aws s3 mb s3://dharmasala-images-prod-<account-id> --region ap-south-1

# Block all public access
aws s3api put-public-access-block \
  --bucket dharmasala-images-prod-<account-id> \
  --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

# Enable KMS encryption
aws s3api put-bucket-encryption \
  --bucket dharmasala-images-prod-<account-id> \
  --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"aws:kms"}}]}'

# Lifecycle: move to cheaper tiers after time
aws s3api put-bucket-lifecycle-configuration \
  --bucket dharmasala-images-prod-<account-id> \
  --lifecycle-configuration '{
    "Rules": [{
      "ID": "Archive",
      "Status": "Enabled",
      "Transitions": [
        {"Days": 90, "StorageClass": "STANDARD_IA"},
        {"Days": 365, "StorageClass": "GLACIER"}
      ]
    }]
  }'
```

---

## Phase 2: Lambda + SAM (Week 2)

### 2.1 Code Changes

Five files require changes. No new files except `template.yaml` and the alert dispatcher.

#### `config.py` — Replace dotenv with Secrets Manager

```python
# Replace the entire config.py with:
import os
import json
import boto3

def _load_secrets() -> dict:
    """Fetch secrets from Secrets Manager. Cached at module level (per Lambda container)."""
    secret_arn = os.environ.get("SECRET_ARN")
    if not secret_arn:
        # Local development fallback
        from dotenv import load_dotenv
        load_dotenv()
        return {}
    client = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", "ap-south-1"))
    return json.loads(client.get_secret_value(SecretId=secret_arn)["SecretString"])

_secrets = _load_secrets()

# AWS environment variables (injected by SAM template)
IMAGE_BUCKET       = os.environ.get("IMAGE_BUCKET", "")
DB_PROXY_ENDPOINT  = os.environ.get("DB_PROXY_ENDPOINT", "")
SQS_ALERT_QUEUE_URL = os.environ.get("SQS_ALERT_QUEUE_URL", "")
AWS_REGION         = os.environ.get("AWS_REGION", "ap-south-1")
ENVIRONMENT        = os.environ.get("ENVIRONMENT", "dev")

# Secrets (from Secrets Manager in prod, from .env locally)
ANTHROPIC_API_KEY  = _secrets.get("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_API_KEY", "")
SLACK_WEBHOOK_URL  = _secrets.get("SLACK_WEBHOOK_URL") or os.getenv("SLACK_WEBHOOK_URL", "")
DB_PASSWORD        = _secrets.get("DB_PASSWORD", "")

# Local dev database (ignored in production — Lambda uses DB_PROXY_ENDPOINT)
import sqlite3
from pathlib import Path
BASE_DIR  = Path(__file__).parent
DB_PATH   = BASE_DIR / "dharmasala.db"
STORAGE_DIR = BASE_DIR / "storage"

# Unchanged thresholds
ESCALATION_SEVERITY_THRESHOLD  = 7
SIMILARITY_PHASH_THRESHOLD      = 10
SIMILARITY_EMBEDDING_THRESHOLD  = 0.85
MAX_IMAGE_SIZE_MB               = 10
ALLOWED_IMAGE_TYPES             = {"image/jpeg", "image/png", "image/webp", "image/gif"}
```

#### `database.py` — Replace sqlite3 with psycopg2 + SQLAlchemy pool

The full replacement keeps identical function signatures so the rest of the app is unaffected.

```python
# Add to requirements.txt: psycopg2-binary, sqlalchemy

import os
import uuid
import json
from datetime import datetime, timezone
from contextlib import contextmanager
from sqlalchemy import create_engine, text
from sqlalchemy.pool import QueuePool
import config

def _build_engine():
    """
    Returns a SQLAlchemy engine.
    - In production: connects to Aurora via RDS Proxy using IAM auth token.
    - In local dev: connects to SQLite as before.
    """
    if config.DB_PROXY_ENDPOINT:
        import boto3
        rds = boto3.client("rds", region_name=config.AWS_REGION)
        token = rds.generate_db_auth_token(
            DBHostname=config.DB_PROXY_ENDPOINT,
            Port=5432,
            DBUsername="dbadmin",
            Region=config.AWS_REGION,
        )
        url = (
            f"postgresql+psycopg2://dbadmin:{token}@"
            f"{config.DB_PROXY_ENDPOINT}:5432/dharmasala"
            f"?sslmode=require"
        )
        return create_engine(
            url,
            poolclass=QueuePool,
            pool_size=5,          # Lambda container holds up to 5 connections
            max_overflow=2,
            pool_pre_ping=True,   # Detect stale connections from RDS Proxy
            pool_recycle=1800,    # Rotate connections before IAM token expires (15 min)
        )
    else:
        # Local SQLite for development
        return create_engine(f"sqlite:///{config.DB_PATH}", connect_args={"check_same_thread": False})

# Module-level engine — created once per Lambda container, reused across invocations
engine = _build_engine()

@contextmanager
def get_db():
    with engine.connect() as conn:
        yield conn
        conn.commit()

def init_db():
    """Run schema DDL. In production, schema is applied manually (Phase 1.4).
    Kept here for local SQLite development only."""
    if config.DB_PROXY_ENDPOINT:
        return  # Schema already applied to Aurora
    with get_db() as conn:
        # ... existing SQLite DDL unchanged for local dev ...

# All other functions (create_incident, get_incident, etc.) replace:
#   conn.execute("... ? ...", (val,))
# with:
#   conn.execute(text("... :param ..."), {"param": val})
#
# And replace sqlite3.Row dict conversion (dict(row)) with:
#   row._mapping (SQLAlchemy RowMapping, also dict-like)
```

**Full function-by-function changes:** Replace `?` placeholders with `:name` named parameters
and wrap all SQL strings in `text()`. The logic in every function (`create_incident`,
`find_by_sha256`, `find_all_phashes`, etc.) is otherwise identical.

**`find_all_phashes` replacement:** This function loads every phash into Python memory for
in-memory Hamming distance comparison. With pgvector, replace it with a SQL query. Update
`services/similarity.py` to call the new function:

```python
# New function in database.py — replaces find_all_phashes() for embedding search
def find_similar_by_embedding(embedding: list[float], exclude_id: str, limit: int = 5) -> list[dict]:
    """Find similar incidents using pgvector cosine similarity."""
    with get_db() as conn:
        rows = conn.execute(text("""
            SELECT incident_id, image_sha256, triage_severity,
                   1 - (embedding <=> :emb::vector) AS similarity_score
            FROM incidents
            WHERE embedding IS NOT NULL
              AND incident_id != :exclude
            ORDER BY embedding <=> :emb::vector
            LIMIT :limit
        """), {"emb": str(embedding), "exclude": exclude_id, "limit": limit}).fetchall()
        return [dict(r._mapping) for r in rows]

# Keep find_all_phashes() returning [] in production (pgvector handles it)
# or remove it entirely once similarity.py is updated
```

#### `app.py` — Five targeted changes

**Change 1:** Add Mangum handler (last line, before `if __name__ == "__main__":`)
```python
from mangum import Mangum
handler = Mangum(app, lifespan="off")
```

**Change 2:** Remove static file serving (delete `app.py:48–57`):
```python
# DELETE these lines:
app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "static")), name="static")

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    return FileResponse(str(config.BASE_DIR / "static" / "index.html"))

@app.get("/admin.html", response_class=HTMLResponse)
async def serve_admin():
    return FileResponse(str(config.BASE_DIR / "static" / "admin.html"))
```

**Change 3:** Move `db.init_db()` out of the `startup` event (Lambda initializes at module
import, not via ASGI lifecycle):
```python
# Replace:
@app.on_event("startup")
def startup():
    db.init_db()
    ...

# With (module-level, runs once per Lambda container):
db.init_db()  # no-op in production (returns early if DB_PROXY_ENDPOINT is set)
logger.info("Config loaded. Bucket: %s, Region: %s", config.IMAGE_BUCKET, config.AWS_REGION)
```

**Change 4:** Replace the `POST /v1/triage/image` multipart upload with an S3-key-based endpoint.
Add a new `GET /v1/triage/upload-url` endpoint alongside it:

```python
import boto3
s3_client = boto3.client("s3", region_name=config.AWS_REGION) if config.IMAGE_BUCKET else None

@app.get("/v1/triage/upload-url")
async def get_upload_url(filename: str = Query(...), content_type: str = Query("image/jpeg")):
    """Issue a pre-signed S3 PUT URL. Client uploads directly; Lambda never sees raw bytes."""
    if content_type not in config.ALLOWED_IMAGE_TYPES:
        raise HTTPException(400, f"Unsupported content type: {content_type}")
    staging_key = f"uploads/{uuid.uuid4()}/{filename}"
    url = s3_client.generate_presigned_url(
        "put_object",
        Params={"Bucket": config.IMAGE_BUCKET, "Key": staging_key, "ContentType": content_type},
        ExpiresIn=300,
    )
    return {"upload_url": url, "s3_key": staging_key, "expires_in": 300}


@app.post("/v1/triage/image")
async def triage_image(
    s3_key: str = Form(...),       # replaces: image: UploadFile = File(...)
    context: str = Form(""),
    session_id: str = Form(""),
    lat: float = Form(None),
    lng: float = Form(None),
    location_source: str = Form(""),
):
    """UC-1: Image triage. Client has already uploaded image to S3 via pre-signed URL."""
    if not session_id:
        session_id = str(uuid.uuid4())

    if context:
        guard = guardrails.check_input(context)
        if not guard.allowed:
            return ChatResponse(response=guard.reason)

    # Read image from S3 (replaces: image_bytes = await image.read())
    obj = s3_client.get_object(Bucket=config.IMAGE_BUCKET, Key=s3_key)
    image_bytes = obj["Body"].read()
    filename = s3_key.split("/")[-1]
    content_type = obj["ContentType"]

    if len(image_bytes) > config.MAX_IMAGE_SIZE_MB * 1024 * 1024:
        raise HTTPException(400, f"Image exceeds {config.MAX_IMAGE_SIZE_MB}MB limit")

    # Compute hashes
    sha256 = similarity.compute_sha256(image_bytes)
    phash = similarity.compute_phash(image_bytes)

    # Move from staging key to permanent key (replaces: blob_path.write_bytes(image_bytes))
    permanent_key = f"incidents/{sha256}/{filename}"
    s3_client.copy_object(
        Bucket=config.IMAGE_BUCKET,
        CopySource={"Bucket": config.IMAGE_BUCKET, "Key": s3_key},
        Key=permanent_key,
    )
    s3_client.delete_object(Bucket=config.IMAGE_BUCKET, Key=s3_key)

    # All remaining logic (EXIF, triage, similarity, incident creation, alert) is UNCHANGED
    # Pass permanent_key as image_blob_path → stored as image_s3_key in Aurora schema
    ...
```

**Change 5:** Replace `admin_password` checks with Cognito group check. Add a middleware helper:

```python
from fastapi import Request

def require_admin(request: Request):
    """Validates that the JWT (set by API Gateway) contains the 'admin' group claim."""
    claims = request.scope.get("aws.event", {}).get("requestContext", {}) \
                          .get("authorizer", {}).get("jwt", {}).get("claims", {})
    groups = claims.get("cognito:groups", "")
    if "admin" not in groups:
        raise HTTPException(403, "Admin group membership required")

# Replace: if request.admin_password != config.ADMIN_PASSWORD: raise HTTPException(403, ...)
# With:    require_admin(request)
```

#### `services/alerts.py` — Publish to SQS instead of calling Slack directly

```python
# Replace the _send_slack() direct call with SQS publish:
import boto3
import json
import config

_sqs = boto3.client("sqs", region_name=config.AWS_REGION) if config.SQS_ALERT_QUEUE_URL else None

def send_alert(incident_id: str, triage_result: dict, location: dict = None, similar_id: str = None) -> str:
    alert_id = db.create_alert(incident_id, channel="sqs", reason=triage_result.get("severity"))
    payload = {
        "alert_id": alert_id,
        "incident_id": incident_id,
        "severity": triage_result.get("severity"),
        "severity_score": triage_result.get("severity_score"),
        "summary": triage_result.get("triage_summary"),
        "location": location,
        "similar_id": similar_id,
    }
    if _sqs:
        _sqs.send_message(
            QueueUrl=config.SQS_ALERT_QUEUE_URL,
            MessageBody=json.dumps(payload),
        )
    else:
        # Local dev: log to console as before
        logger.info("ALERT (local): %s", json.dumps(payload))
    return alert_id
```

Create `services/alert_dispatcher.py` — a separate Lambda handler triggered by SQS:

```python
"""Standalone Lambda handler for SQS-triggered alert dispatch (not part of FastAPI app)."""
import json
import os
import urllib.request

SLACK_WEBHOOK_URL = None  # loaded from Secrets Manager on cold start

def handler(event, context):
    for record in event["Records"]:
        payload = json.loads(record["body"])
        _send_to_slack(payload)
    return {"batchItemFailures": []}   # Return failures for partial batch retry

def _send_to_slack(payload: dict):
    webhook = _get_slack_url()
    message = {
        "text": f"*{payload['severity'].upper()} Alert* — Incident `{payload['incident_id']}`\n"
                f"{payload.get('summary', '')}"
    }
    req = urllib.request.Request(webhook, json.dumps(message).encode(), {"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10)

def _get_slack_url() -> str:
    global SLACK_WEBHOOK_URL
    if SLACK_WEBHOOK_URL:
        return SLACK_WEBHOOK_URL
    import boto3
    client = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", "ap-south-1"))
    secrets = json.loads(client.get_secret_value(SecretId=os.environ["SECRET_ARN"])["SecretString"])
    SLACK_WEBHOOK_URL = secrets["SLACK_WEBHOOK_URL"]
    return SLACK_WEBHOOK_URL
```

#### `requirements.txt` — Add production dependencies

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
psycopg2-binary          # PostgreSQL driver
sqlalchemy               # Connection pooling + ORM abstraction
boto3                    # AWS SDK (S3, SQS, Secrets Manager, Bedrock, RDS IAM auth)
aws-lambda-powertools    # Structured logging, X-Ray tracing, custom metrics
python-jose[cryptography]  # JWT validation (for local testing of Cognito tokens)
```

Remove `gunicorn` and `mysql-connector-python` — neither is needed with Lambda.

### 2.2 SAM Template

Create `template.yaml` in the project root:

```yaml
AWSTemplateFormatVersion: '2010-09-09'
Transform: AWS::Serverless-2016-10-31

Parameters:
  Environment:
    Type: String
    Default: prod
    AllowedValues: [dev, staging, prod]
  VpcId:
    Type: AWS::EC2::VPC::Id
  PrivateSubnetIds:
    Type: List<AWS::EC2::Subnet::Id>
  LambdaSecurityGroupId:
    Type: AWS::EC2::SecurityGroup::Id
  SecretArn:
    Type: String
  RdsProxyEndpoint:
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
              audience:
                - !Ref CognitoUserPoolClientId
        EnableIamAuthorizer: false

  # ── Main Lambda (FastAPI + Mangum) ───────────────────────────────────────────
  ChatbotFunction:
    Type: AWS::Serverless::Function
    Properties:
      Handler: app.handler
      CodeUri: .
      VpcConfig:
        SecurityGroupIds: [!Ref LambdaSecurityGroupId]
        SubnetIds: !Ref PrivateSubnetIds
      Environment:
        Variables:
          SECRET_ARN:          !Ref SecretArn
          IMAGE_BUCKET:        !Ref ImageBucket
          DB_PROXY_ENDPOINT:   !Ref RdsProxyEndpoint
          SQS_ALERT_QUEUE_URL: !Ref AlertQueue
      Policies:
        - AWSSecretsManagerGetSecretValuePolicy:
            SecretArn: !Ref SecretArn
        - S3CrudPolicy:
            BucketName: !Ref ImageBucket
        - SQSSendMessagePolicy:
            QueueName: !GetAtt AlertQueue.QueueName
        - VPCAccessPolicy: {}
        - Statement:
            - Effect: Allow
              Action: bedrock:InvokeModel
              Resource:
                - arn:aws:bedrock:*::foundation-model/anthropic.claude-3-5-sonnet-20241022-v2:0
        - Statement:
            - Effect: Allow
              Action: rds-db:connect
              Resource: !Sub arn:aws:rds-db:${AWS::Region}:${AWS::AccountId}:dbuser:*/dbadmin
      Events:
        RootRoute:
          Type: HttpApi
          Properties:
            ApiId: !Ref ChatApi
            Path: /
            Method: ANY
        ProxyRoute:
          Type: HttpApi
          Properties:
            ApiId: !Ref ChatApi
            Path: /{proxy+}
            Method: ANY

  # Provisioned concurrency for the triage endpoint (eliminates cold starts)
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

  # ── S3 Image Bucket ───────────────────────────────────────────────────────────
  ImageBucket:
    Type: AWS::S3::Bucket
    Properties:
      BucketEncryption:
        ServerSideEncryptionConfiguration:
          - ServerSideEncryptionByDefault:
              SSEAlgorithm: aws:kms
      PublicAccessBlockConfiguration:
        BlockPublicAcls: true
        BlockPublicPolicy: true
        IgnorePublicAcls: true
        RestrictPublicBuckets: true
      LifecycleConfiguration:
        Rules:
          - Id: Archive
            Status: Enabled
            Transitions:
              - TransitionInDays: 90
                StorageClass: STANDARD_IA
              - TransitionInDays: 365
                StorageClass: GLACIER
      CorsConfiguration:
        CorsRules:
          - AllowedMethods: [PUT]
            AllowedOrigins: ['*']        # Tighten to Amplify domain after Phase 3
            AllowedHeaders: ['*']
            MaxAge: 300

  # ── SQS Alert Queue ───────────────────────────────────────────────────────────
  AlertQueue:
    Type: AWS::SQS::Queue
    Properties:
      VisibilityTimeout: 300
      RedrivePolicy:
        deadLetterTargetArn: !GetAtt AlertDLQ.Arn
        maxReceiveCount: 3

  AlertDLQ:
    Type: AWS::SQS::Queue

  # ── CloudWatch Alarms ─────────────────────────────────────────────────────────
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
```

### 2.3 Deploy and Validate

```bash
# First deploy (will prompt for parameter values)
sam build
sam deploy --guided --config-env prod

# Subsequent deploys
sam build && sam deploy --config-env prod --no-confirm-changeset

# Test Lambda directly (bypasses API Gateway)
aws lambda invoke \
  --function-name dharmasala-chatbot-prod-ChatbotFunction \
  --payload '{"version":"2.0","routeKey":"GET /health","rawPath":"/health","headers":{},"requestContext":{"http":{"method":"GET","path":"/health"}}}' \
  /tmp/response.json
cat /tmp/response.json
```

Expected response: `{"status":"ok","version":"1.0.0-prototype","ai_configured":true,...}`

---

## Phase 3: API Gateway, CloudFront, WAF, Amplify, Cognito (Week 3)

### 3.1 Cognito User Pool

```bash
# Create user pool
aws cognito-idp create-user-pool \
  --pool-name dharmasala-users-prod \
  --admin-create-user-config AllowAdminCreateUserOnly=true \
  --schema '[{"Name":"custom:role","AttributeDataType":"String","Mutable":true}]' \
  --region ap-south-1

# Create app client (no secret — browser-side auth)
aws cognito-idp create-user-pool-client \
  --user-pool-id <pool-id> \
  --client-name chatbot-web \
  --generate-secret \
  --explicit-auth-flows ALLOW_USER_SRP_AUTH ALLOW_REFRESH_TOKEN_AUTH \
  --supported-identity-providers COGNITO

# Create groups
aws cognito-idp create-group --group-name volunteer --user-pool-id <pool-id>
aws cognito-idp create-group --group-name admin --user-pool-id <pool-id>

# Create first admin user
aws cognito-idp admin-create-user \
  --user-pool-id <pool-id> \
  --username admin@dharmasala.org \
  --temporary-password "Change-Me-1!" \
  --user-attributes Name=email,Value=admin@dharmasala.org Name=email_verified,Value=true

aws cognito-idp admin-add-user-to-group \
  --user-pool-id <pool-id> \
  --username admin@dharmasala.org \
  --group-name admin
```

Update `template.yaml` parameters with the Cognito pool ID and client ID, then redeploy.

### 3.2 CloudFront Distribution

Create a CloudFront distribution with two origins:

| Origin | Path Pattern | Purpose |
|---|---|---|
| API Gateway endpoint | `/v1/*` | All API requests |
| Amplify app (via Amplify domain) | Default (`/*`) | Static UI |

```bash
# Create WAF WebACL (attach to CloudFront)
aws wafv2 create-web-acl \
  --name dharmasala-waf-prod \
  --scope CLOUDFRONT \
  --region us-east-1 \           # WAF for CloudFront must be us-east-1
  --default-action Allow={} \
  --rules '[
    {
      "Name": "RateLimit",
      "Priority": 1,
      "Statement": {"RateBasedStatement": {"Limit": 1000, "AggregateKeyType": "IP"}},
      "Action": {"Block": {}},
      "VisibilityConfig": {"SampledRequestsEnabled": true, "CloudWatchMetricsEnabled": true, "MetricName": "RateLimit"}
    },
    {
      "Name": "AWSManagedRulesCommonRuleSet",
      "Priority": 2,
      "OverrideAction": {"None": {}},
      "Statement": {"ManagedRuleGroupStatement": {"VendorName": "AWS", "Name": "AWSManagedRulesCommonRuleSet"}},
      "VisibilityConfig": {"SampledRequestsEnabled": true, "CloudWatchMetricsEnabled": true, "MetricName": "CommonRuleSet"}
    }
  ]' \
  --visibility-config SampledRequestsEnabled=true,CloudWatchMetricsEnabled=true,MetricName=dharmasala-waf
```

### 3.3 Amplify Hosting

```bash
# Connect Amplify to GitHub repository
aws amplify create-app \
  --name dharmasala-chatbot-ui \
  --repository https://github.com/your-org/gaia-chatbot \
  --access-token <github-token> \
  --region ap-south-1

aws amplify create-branch \
  --app-id <amplify-app-id> \
  --branch-name main \
  --stage PRODUCTION \
  --environment-variables \
    API_ENDPOINT=https://rescue.dharmasala.org,COGNITO_POOL_ID=<pool-id>,COGNITO_CLIENT_ID=<client-id>
```

Create `amplify.yml` in the project root to configure the static build:

```yaml
version: 1
frontend:
  phases:
    build:
      commands:
        - echo "Static HTML/JS — no build step required"
  artifacts:
    baseDirectory: static
    files:
      - '**/*'
  cache:
    paths: []
```

Update `static/app.js` to use environment variables injected at Amplify build time:

```javascript
// Replace hardcoded localhost references:
const API_BASE = process.env.API_ENDPOINT || 'http://localhost:8000';
const COGNITO_POOL_ID = process.env.COGNITO_POOL_ID || '';

// Update image upload to use two-step pre-signed URL flow:
async function uploadImage(file, context, sessionId, lat, lng) {
    // Step 1: Get pre-signed URL
    const urlResp = await fetch(`${API_BASE}/v1/triage/upload-url?filename=${file.name}&content_type=${file.type}`);
    const { upload_url, s3_key } = await urlResp.json();

    // Step 2: Upload directly to S3
    await fetch(upload_url, { method: 'PUT', body: file, headers: { 'Content-Type': file.type } });

    // Step 3: Submit triage request with S3 key
    const form = new FormData();
    form.append('s3_key', s3_key);
    form.append('context', context);
    form.append('session_id', sessionId);
    if (lat) form.append('lat', lat);
    if (lng) form.append('lng', lng);
    return fetch(`${API_BASE}/v1/triage/image`, { method: 'POST', body: form });
}
```

---

## Phase 4: Observability (Week 4)

### 4.1 Lambda Powertools

Replace the existing `logging.basicConfig` call in `app.py:25–29` with Powertools:

```python
from aws_lambda_powertools import Logger, Tracer, Metrics
from aws_lambda_powertools.metrics import MetricUnit

logger  = Logger()
tracer  = Tracer()
metrics = Metrics(namespace="DharmasalaChatbot")

# Decorate the Mangum handler to enable auto-flushing of metrics per request:
@metrics.log_metrics(capture_cold_start_metric=True)
@tracer.capture_lambda_handler
def handler(event, context):
    return mangum_handler(event, context)

mangum_handler = Mangum(app, lifespan="off")

# Emit custom metrics in service functions:
metrics.add_metric("TriageLatencyMs", MetricUnit.Milliseconds, triage_result.get("latency_ms", 0))
metrics.add_metric("SeverityScore",   MetricUnit.Count,        triage_result.get("severity_score", 0))
metrics.add_metric("GuardrailBlock",  MetricUnit.Count,        1 if not guard.allowed else 0)
```

All `logger.info(...)` calls in the existing code are automatically upgraded to structured JSON
with request ID, function name, and cold-start flag — no other changes needed.

### 4.2 CloudWatch Dashboard

```bash
aws cloudwatch put-dashboard --dashboard-name DharamsalaChatbot --dashboard-body '{
  "widgets": [
    {"type": "metric", "properties": {"title": "Lambda Errors", "metrics": [["AWS/Lambda", "Errors", "FunctionName", "dharmasala-chatbot-prod-ChatbotFunction"]], "period": 300}},
    {"type": "metric", "properties": {"title": "Lambda P95 Duration", "metrics": [["AWS/Lambda", "Duration", "FunctionName", "dharmasala-chatbot-prod-ChatbotFunction"]], "stat": "p95", "period": 300}},
    {"type": "metric", "properties": {"title": "Aurora ACUs", "metrics": [["AWS/RDS", "ServerlessDatabaseCapacity", "DBClusterIdentifier", "dharmasala-cluster"]], "period": 60}},
    {"type": "metric", "properties": {"title": "Alert DLQ Depth", "metrics": [["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", "AlertDLQ"]], "period": 60}}
  ]
}'
```

---

## Phase 5: CI/CD — GitHub Actions (Week 5)

### 5.1 OIDC Role

Configure GitHub Actions to authenticate via OIDC (no long-lived keys):

```bash
# Create OIDC identity provider (one-time per AWS account)
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1

# Create IAM role trusted by GitHub Actions OIDC
# Attach policies: AWSLambdaFullAccess (scope down in production), AmazonS3FullAccess, CloudFormationFullAccess
# Trust policy condition: token.actions.githubusercontent.com:sub = repo:your-org/gaia-chatbot:ref:refs/heads/main
```

### 5.2 Workflow File

Create `.github/workflows/deploy.yml`:

```yaml
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
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY_TEST }}

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
              RdsProxyEndpoint=${{ secrets.RDS_PROXY_ENDPOINT }} \
              CognitoUserPoolId=${{ secrets.COGNITO_USER_POOL_ID }} \
              CognitoUserPoolClientId=${{ secrets.COGNITO_CLIENT_ID }} \
              VpcId=${{ secrets.VPC_ID }} \
              PrivateSubnetIds=${{ secrets.PRIVATE_SUBNET_IDS }} \
              LambdaSecurityGroupId=${{ secrets.LAMBDA_SG_ID }}
```

**GitHub Secrets to configure:** `AWS_DEPLOY_ROLE_ARN`, `SECRET_ARN`, `RDS_PROXY_ENDPOINT`,
`COGNITO_USER_POOL_ID`, `COGNITO_CLIENT_ID`, `VPC_ID`, `PRIVATE_SUBNET_IDS`, `LAMBDA_SG_ID`,
`ANTHROPIC_API_KEY_TEST` (for running tests in CI without AWS).

---

## Phase 6: Testing and Go-Live (Week 6)

### 6.1 Test Gates

```bash
# Unit tests (run locally and in CI)
python -m pytest tests/test_unit.py -v

# System integration tests against staging Lambda
STAGE_API=https://<api-id>.execute-api.ap-south-1.amazonaws.com/staging \
python -m pytest tests/test_system.py -v --base-url=$STAGE_API

# Load test: 50 concurrent users, 5 minutes
# Use locust or k6:
k6 run --vus 50 --duration 5m tests/load_test.js
# Assert: P95 < 4000ms text, P95 < 8000ms image

# Guardrail regression (existing test_unit.py covers this)
python -m pytest tests/ -k "guardrail" -v

# Security scan
pip install bandit
bandit -r . -ll --exclude .venv,tests
```

### 6.2 SAM Environment Parity

Deploy a `staging` stack alongside `prod` for pre-merge validation:

```bash
sam deploy --config-env staging \
  --stack-name dharmasala-chatbot-staging \
  --parameter-overrides Environment=staging ...
```

Run the test suite against staging on every pull request (add a second `deploy` job in the
workflow with `environment: staging` and trigger on `pull_request`).

### 6.3 Go-Live Checklist

**Infrastructure:**
- [ ] Aurora cluster created, pgvector extension enabled, schema DDL applied
- [ ] RDS Proxy created and registered to Aurora cluster
- [ ] S3 bucket created with encryption, public access blocked, CORS configured
- [ ] Secrets Manager secret contains all keys
- [ ] Lambda function deploys successfully (`sam deploy`)
- [ ] API Gateway HTTP API endpoint returns `200` on `GET /health`
- [ ] Cognito user pool created; admin user added to `admin` group
- [ ] CloudFront distribution configured with WAF WebACL
- [ ] Amplify Hosting connected to GitHub; `main` branch deploys automatically
- [ ] Route 53 alias record points to CloudFront distribution

**Application:**
- [ ] Pre-signed URL flow works end-to-end (image lands in S3, triage returns result)
- [ ] Chat query endpoint returns Claude response
- [ ] Similarity search returns results (pgvector index built once > 10 rows exist)
- [ ] Admin NL query endpoint requires `admin` Cognito group (403 without it)
- [ ] Alert fires to Slack when severity >= 7
- [ ] DLQ alarm triggers if Slack webhook is deliberately broken

**Validation:**
- [ ] All existing `test_unit.py` tests pass against staging
- [ ] Load test: P95 < 4s text, P95 < 8s image at 50 concurrent users
- [ ] CloudWatch dashboard shows Lambda errors = 0, Aurora ACU < 2 at idle
- [ ] X-Ray trace confirms no unexpected high-latency subsegments

**Go-Live:**
- [ ] DNS cutover: update Route 53 record to CloudFront distribution
- [ ] Smoke test via production domain (image upload, chat query, admin query)
- [ ] Monitor error rate and latency for 30 minutes post-cutover

---

## Code Changes Summary

| File | Nature of change | Scope |
|---|---|---|
| `config.py` | Replace dotenv with Secrets Manager; add AWS env vars | Rewrite |
| `database.py` | Replace sqlite3 with SQLAlchemy + psycopg2; add `find_similar_by_embedding()` | Rewrite |
| `app.py` | Add Mangum; remove static serving; replace image upload with pre-signed URL flow; replace password auth with Cognito group check; move `init_db()` to module level | Targeted edits |
| `services/alerts.py` | Replace direct Slack call with SQS publish | Small edit |
| `services/similarity.py` | Use pgvector query for embedding similarity in addition to pHash | Small addition |
| `static/app.js` | Update API base URL from env; implement two-step image upload | Targeted edit |
| `requirements.txt` | Add mangum, psycopg2-binary, sqlalchemy, boto3, aws-lambda-powertools | Additions |
| New: `template.yaml` | SAM template defining all AWS resources | New file |
| New: `samconfig.toml` | SAM deployment configuration per environment | New file |
| New: `.github/workflows/deploy.yml` | GitHub Actions CI/CD pipeline | New file |
| New: `services/alert_dispatcher.py` | Standalone Lambda handler for SQS alert dispatch | New file |
| New: `amplify.yml` | Amplify Hosting build configuration | New file |

---

## Rollback Plan

| Scenario | Rollback action |
|---|---|
| Lambda regression | `sam deploy` with previous artifact version (SAM keeps versions in S3); or redeploy last working git commit |
| Aurora data corruption | Point-in-time restore: `aws rds restore-db-cluster-to-point-in-time` (continuous backup enabled) |
| DNS cutover issue | Update Route 53 alias back to previous target; propagation < 60s with low TTL |
| SQS DLQ fills | Re-drive DLQ messages after fix: `aws sqs start-message-move-task` |
| Amplify frontend regression | Redeploy previous Amplify build from console (one click) |

---

## Timeline Summary

| Week | Phase | Deliverable |
|---|---|---|
| 1 | Foundation | VPC, Aurora + pgvector schema, RDS Proxy, S3, Secrets Manager |
| 2 | Lambda + SAM | Code changes, SAM template, initial Lambda deploy and health check |
| 3 | Networking + Auth | CloudFront + WAF, Amplify Hosting, Cognito, Route 53 |
| 4 | Observability | Lambda Powertools, CloudWatch dashboard, alarms |
| 5 | CI/CD | GitHub Actions OIDC deploy, staging environment, PR preview deploys |
| 6 | Testing + Go-Live | Load test, security scan, DNS cutover, 30-min monitoring window |
