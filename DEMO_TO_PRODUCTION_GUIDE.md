# Dharamsala Animal Rescue Chatbot - Demo to Production Transition Guide

## Overview

This guide provides step-by-step instructions for transitioning the local prototype into the
AWS production architecture. Each section maps a demo component to its production replacement,
with specific tasks, configuration, and validation steps.

---

## Prerequisites

- AWS account with admin access
- AWS CLI v2 configured
- Docker installed locally
- Terraform or AWS CDK installed (IaC recommended)
- Anthropic API key (production tier)
- Slack workspace with webhook configured
- Domain name registered (e.g., `rescue.dharmasala.org`)

---

## Phase 1: Foundation (Week 1)

### 1.1 Infrastructure Setup (IaC)

**Create VPC and networking:**

```bash
# Recommended: use Terraform or CDK for all infrastructure
# Key resources to create:
# - VPC with 2+ AZs
# - Public subnets (ALB, NAT Gateway)
# - Private subnets (ECS tasks, RDS, ElastiCache)
# - NAT Gateway for outbound internet from private subnets
# - Security groups for each tier
```

**Checklist:**
- [ ] VPC with CIDR `10.0.0.0/16`
- [ ] 2 public subnets (for ALB)
- [ ] 2 private subnets (for ECS, RDS)
- [ ] NAT Gateway in each AZ
- [ ] Security groups: ALB (80/443 inbound), ECS (8000 from ALB), RDS (3306 from ECS)

### 1.2 Secrets Manager Setup

**Migrate from `.env` to Secrets Manager:**

```bash
aws secretsmanager create-secret \
  --name dharmasala/prod/api-keys \
  --secret-string '{
    "ANTHROPIC_API_KEY": "sk-ant-...",
    "SLACK_WEBHOOK_URL": "https://hooks.slack.com/...",
    "ADMIN_PASSWORD": "<strong-generated-password>"
  }'
```

**Code change required:** Update `config.py` to read from Secrets Manager instead of `.env`:

```python
# Replace dotenv loading with:
import boto3
import json

def get_secrets():
    client = boto3.client("secretsmanager", region_name="ap-south-1")
    response = client.get_secret_value(SecretId="dharmasala/prod/api-keys")
    return json.loads(response["SecretString"])
```

### 1.3 Database Migration (SQLite → RDS MySQL)

**Create RDS instance:**
```bash
aws rds create-db-instance \
  --db-instance-identifier dharmasala-db \
  --db-instance-class db.t3.micro \
  --engine mysql \
  --master-username admin \
  --manage-master-user-password \
  --allocated-storage 20 \
  --multi-az \
  --vpc-security-group-ids sg-xxx \
  --db-subnet-group-name dharmasala-db-subnet
```

**Schema migration:** Convert SQLite schema to MySQL DDL:

| SQLite | MySQL Change |
|--------|-------------|
| `TEXT PRIMARY KEY` for UUIDs | `CHAR(36) PRIMARY KEY` |
| `TEXT` for timestamps | `DATETIME` with default `CURRENT_TIMESTAMP` |
| `REAL` | `DOUBLE` |
| `INTEGER` for booleans | `TINYINT(1)` |
| `PRAGMA foreign_keys` | Default in InnoDB |

**Code change required:** Replace `database.py` SQLite calls with MySQL connector:

```python
# Replace sqlite3 with:
import mysql.connector
# Or use SQLAlchemy for ORM abstraction (recommended for production)
```

**Recommended approach:** Introduce SQLAlchemy as ORM layer - this abstracts the database
and makes the same code work with both SQLite (dev) and MySQL (prod).

### 1.4 S3 Blob Storage

**Create S3 bucket:**
```bash
aws s3 mb s3://dharmasala-rescue-images-prod --region ap-south-1

aws s3api put-bucket-encryption \
  --bucket dharmasala-rescue-images-prod \
  --server-side-encryption-configuration '{
    "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
  }'

aws s3api put-public-access-block \
  --bucket dharmasala-rescue-images-prod \
  --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

**Code change required:** Replace local filesystem writes in `app.py`:

```python
# Replace:
#   blob_path = config.STORAGE_DIR / blob_filename
#   blob_path.write_bytes(image_bytes)

# With:
import boto3
s3 = boto3.client("s3")
s3.put_object(
    Bucket="dharmasala-rescue-images-prod",
    Key=f"incidents/{sha256}/{image.filename}",
    Body=image_bytes,
    ContentType=image.content_type,
)
```

---

## Phase 2: Containerization (Week 2)

### 2.1 Create Dockerfile

Create `Dockerfile` in the chatbot directory:

```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
```

**Add production dependencies** to `requirements.txt`:
```
boto3
mysql-connector-python
# or: sqlalchemy[mysql] pymysql
gunicorn
```

### 2.2 ECR Repository

```bash
aws ecr create-repository --repository-name dharmasala-chatbot

# Build and push
aws ecr get-login-password | docker login --username AWS --password-stdin <account>.dkr.ecr.<region>.amazonaws.com
docker build -t dharmasala-chatbot .
docker tag dharmasala-chatbot:latest <account>.dkr.ecr.<region>.amazonaws.com/dharmasala-chatbot:latest
docker push <account>.dkr.ecr.<region>.amazonaws.com/dharmasala-chatbot:latest
```

### 2.3 ECS Fargate Service

**Task definition** key settings:
- CPU: 512 (0.5 vCPU)
- Memory: 1024 MB
- Port: 8000
- Environment: pull from Secrets Manager
- Log driver: awslogs → CloudWatch

**Service configuration:**
- Desired count: 2 (minimum for HA)
- Auto-scaling: target CPU 70%, min 2, max 10
- ALB target group: health check on `/health`
- Rolling deployment with circuit breaker

---

## Phase 3: Networking and Security (Week 3)

### 3.1 Application Load Balancer

- Listener: HTTPS (443) with ACM certificate
- HTTP (80) → redirect to HTTPS
- Target group: ECS tasks on port 8000
- Health check: `GET /health`, healthy threshold 2, interval 30s

### 3.2 CloudFront + S3 (Static Assets)

```bash
# Upload static assets
aws s3 sync static/ s3://dharmasala-static-prod/static/

# Create CloudFront distribution with:
# - Origin 1: S3 bucket (static assets, path pattern: /static/*)
# - Origin 2: ALB (API requests, default)
# - WAF WebACL attached
```

### 3.3 AWS WAF Rules

- **Rate limiting**: 1000 requests per 5 minutes per IP
- **AWS Managed Rules**: Core rule set (CRS), SQL injection, XSS
- **Custom rule**: Block requests with prompt-injection patterns in body
- **Geo-restriction**: Optional, allow India + select countries

### 3.4 Amazon Cognito (Replace Password Auth)

```bash
aws cognito-idp create-user-pool --pool-name dharmasala-users
aws cognito-idp create-user-pool-client --user-pool-id <pool-id> --client-name chatbot-web
```

**Code changes:**
- Add JWT verification middleware to FastAPI
- Replace `admin_password` parameter with Cognito JWT token validation
- Create user groups: `public`, `volunteer`, `admin`
- Admin endpoints require `admin` group membership

---

## Phase 4: Alert Pipeline (Week 4)

### 4.1 SQS Queue

```bash
aws sqs create-queue --queue-name dharmasala-alerts
aws sqs create-queue --queue-name dharmasala-alerts-dlq
```

### 4.2 Lambda Alert Dispatcher

**Code change:** Replace direct Slack/webhook calls in `services/alerts.py`:

```python
# Replace _send_slack() with SQS publish:
import boto3
sqs = boto3.client("sqs")

def send_alert(incident_id, triage_result, location, similar_id):
    payload = build_alert_payload(incident_id, triage_result, location, similar_id)
    sqs.send_message(
        QueueUrl="https://sqs.<region>.amazonaws.com/<account>/dharmasala-alerts",
        MessageBody=json.dumps(payload),
    )
```

Lambda function (triggered by SQS) handles Slack delivery with retry logic.

### 4.3 SNS Backup Channel

- Create SNS topic for email/SMS alerts when Slack fails
- Lambda publishes to SNS on Slack delivery failure

---

## Phase 5: Observability (Week 5)

### 5.1 CloudWatch Logs

ECS tasks auto-send stdout/stderr to CloudWatch via `awslogs` driver.

**Structured logging change:** Update Python logging to JSON format:

```python
import json
import logging

class JSONFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        })
```

### 5.2 CloudWatch Metrics and Alarms

**Custom metrics to emit:**
- `triage_latency_ms` (per request)
- `severity_distribution` (counter by severity level)
- `guardrail_trigger_count` (counter by category)
- `duplicate_detection_count`
- `alert_dispatch_count`

**Alarms:**
- API error rate > 5% → SNS notification
- P95 latency > 4s (text) or > 8s (image)
- ECS task count < 2
- RDS CPU > 80%
- SQS DLQ depth > 0

### 5.3 X-Ray Tracing

Add X-Ray SDK to trace requests end-to-end:

```python
from aws_xray_sdk.core import xray_recorder
from aws_xray_sdk.ext.fastapi.middleware import XRayMiddleware

app = FastAPI()
XRayMiddleware(app, recorder=xray_recorder)
```

---

## Phase 6: CI/CD Pipeline (Week 6)

### 6.1 CodePipeline Setup

```
Source (GitHub) → Build (CodeBuild) → Deploy (ECS)
```

**CodeBuild `buildspec.yml`:**
```yaml
version: 0.2
phases:
  pre_build:
    commands:
      - pip install -r requirements.txt
      - python -m pytest tests/
      - aws ecr get-login-password | docker login --username AWS --password-stdin $ECR_URI
  build:
    commands:
      - docker build -t $ECR_URI:$CODEBUILD_RESOLVED_SOURCE_VERSION .
      - docker push $ECR_URI:$CODEBUILD_RESOLVED_SOURCE_VERSION
  post_build:
    commands:
      - printf '[{"name":"app","imageUri":"%s"}]' $ECR_URI:$CODEBUILD_RESOLVED_SOURCE_VERSION > imagedefinitions.json
artifacts:
  files: imagedefinitions.json
```

### 6.2 Test Gates

- **Unit tests**: Service-level tests for guardrails, similarity, triage parsing
- **API contract tests**: Validate all endpoint request/response schemas
- **Guardrail regression tests**: Curated test cases for off-topic, injection, harmful inputs
- **Integration tests**: End-to-end flows against staging environment

---

## Code Changes Summary

| File | Changes Required |
|------|-----------------|
| `config.py` | Replace dotenv with Secrets Manager; add AWS region config |
| `database.py` | Replace SQLite with MySQL connector or SQLAlchemy |
| `app.py` | Add Cognito JWT middleware; update blob storage to S3 |
| `services/alerts.py` | Replace direct dispatch with SQS publish |
| `services/triage.py` | Add X-Ray subsegment tracing; optional Bedrock client |
| `services/similarity.py` | Add OpenSearch client for embedding similarity |
| `requirements.txt` | Add boto3, mysql-connector, aws-xray-sdk, python-jose |
| New: `Dockerfile` | Container build instructions |
| New: `buildspec.yml` | CI/CD build specification |
| New: `tests/` | Unit, integration, and guardrail test suites |
| New: `infra/` | Terraform/CDK infrastructure definitions |

---

## Migration Checklist

### Pre-Migration
- [ ] AWS account set up with appropriate IAM roles
- [ ] Domain registered, hosted zone in Route 53
- [ ] ACM certificate issued for domain
- [ ] Anthropic API key with production rate limits
- [ ] Slack webhook URL for production channel

### Infrastructure
- [ ] VPC, subnets, security groups created
- [ ] RDS MySQL provisioned and accessible from private subnets
- [ ] S3 buckets created (images + static assets)
- [ ] ElastiCache Redis provisioned
- [ ] ECR repository created
- [ ] ECS cluster, task definition, and service created
- [ ] ALB configured with HTTPS listener
- [ ] CloudFront distribution created
- [ ] WAF WebACL attached to CloudFront and ALB
- [ ] Secrets Manager secrets stored
- [ ] Cognito user pool and app client created

### Application
- [ ] Dockerfile tested locally
- [ ] Config updated for AWS services (Secrets Manager, S3, RDS)
- [ ] Database schema migrated to MySQL
- [ ] Alert pipeline switched to SQS + Lambda
- [ ] Cognito JWT auth integrated
- [ ] Structured JSON logging enabled
- [ ] X-Ray tracing added

### Testing
- [ ] Unit tests passing
- [ ] API contract tests passing
- [ ] Guardrail regression tests passing
- [ ] Load test: 50 concurrent users, P95 latency within targets
- [ ] Security scan: no critical/high vulnerabilities
- [ ] UAT sign-off from rescue operations team

### Go-Live
- [ ] DNS cutover: Route 53 → CloudFront
- [ ] Monitoring dashboards configured
- [ ] Alerting alarms active
- [ ] Runbook documented for on-call team
- [ ] Rollback plan tested
- [ ] Data retention policy configured (S3 lifecycle, RDS backups)

---

## Rollback Plan

If issues are detected after production cutover:

1. **DNS rollback**: Update Route 53 to point back to previous environment (if applicable)
2. **ECS rollback**: Redeploy previous task definition revision
3. **Database**: RDS point-in-time restore (within backup retention window)
4. **S3**: Versioned bucket allows object restoration

---

## Timeline Summary

| Week | Phase | Deliverables |
|------|-------|-------------|
| 1 | Foundation | VPC, RDS, S3, Secrets Manager |
| 2 | Containerization | Dockerfile, ECR, ECS Fargate |
| 3 | Networking + Security | ALB, CloudFront, WAF, Cognito |
| 4 | Alert Pipeline | SQS, Lambda, Slack integration |
| 5 | Observability | CloudWatch, X-Ray, dashboards, alarms |
| 6 | CI/CD + Testing | CodePipeline, test suites, UAT |
| 7 | Hardening + Go-Live | Load testing, security scan, DNS cutover |
