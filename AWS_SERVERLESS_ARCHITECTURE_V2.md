# Dharamsala Animal Rescue Chatbot — Serverless AWS Architecture (Config 2)

## Overview

This document describes the serverless-first production architecture for the Dharamsala Animal
Rescue Chatbot. It replaces the container-based ECS Fargate proposal with API Gateway HTTP API +
Lambda, eliminates ElastiCache and OpenSearch entirely, and uses Aurora Serverless v2 PostgreSQL
with the pgvector extension as the single data tier for both relational data and vector similarity
search.

**Design targets:** 99.5%+ availability, P95 <4s text / <8s image, <$80/month at MVP traffic,
zero persistent infrastructure to manage beyond the database tier.

---

## Architecture Diagram

```
                              ┌─────────────────┐
                              │    Route 53      │
                              └────────┬─────────┘
                                       │
                              ┌────────▼─────────┐
                              │   CloudFront      │
                              │   + WAF           │
                              └────────┬──────────┘
                                       │
               ┌───────────────────────┼────────────────────────┐
               │                                                 │
      ┌────────▼─────────┐                         ┌────────────▼────────────┐
      │  Amplify Hosting  │                         │   API Gateway           │
      │  (Static UI)      │                         │   HTTP API (v2)         │
      │                   │                         │                         │
      │  index.html       │                         │   Built-in JWT          │
      │  admin.html       │                         │   Authorizer            │
      │  app.js / CSS     │                         └────────────┬────────────┘
      │                   │                                      │
      │  Auto-deploys on  │                         ┌────────────▼────────────┐
      │  git push to main │                         │   Cognito User Pool     │
      └───────────────────┘                         │   (Volunteer / Admin    │
                                                    │    groups)              │
                                                    └────────────┬────────────┘
                                                                 │ JWT validation
                                                    ┌────────────▼────────────┐
                                                    │         Lambda           │
                                                    │   FastAPI + Mangum       │
                                                    │   python3.12             │
                                                    │   512 MB / 60s timeout   │
                                                    │   [VPC Private Subnet]   │
                                                    └────┬──────────┬──────────┘
                                                         │          │
                           ┌─────────────────────────────┘          └──────────────────────────────┐
                           │                                                                        │
              ┌────────────▼──────────────┐                                         ┌──────────────▼──────────────┐
              │        RDS Proxy           │                                         │         S3 Bucket            │
              │  (Connection pooling for   │                                         │   dharmasala-images-{env}    │
              │   Lambda → Aurora)         │                                         │                              │
              └────────────┬──────────────┘                                         │   Client uploads via         │
                           │                                                         │   pre-signed PUT URL         │
              ┌────────────▼──────────────┐                                         │   (bypasses Lambda payload   │
              │   Aurora Serverless v2     │                                         │    limit entirely)           │
              │   PostgreSQL               │                                         └──────────────────────────────┘
              │   (Multi-AZ)               │
              │                            │                                         ┌──────────────────────────────┐
              │   + pgvector extension     │                                         │      Amazon Bedrock           │
              │     replaces OpenSearch    │                                         │   Claude 3.5 Sonnet           │
              │     entirely              │                                         │   (vision triage,             │
              │                            │                                         │    chat, NL-to-SQL)           │
              │   Tables:                  │                                         └──────────────────────────────┘
              │   incidents (+ embedding   │
              │             vector col)    │                                         ┌──────────────────────────────┐
              │   alerts                   │                                         │       SQS Alert Queue         │
              │   triage_events            │◄────── Lambda writes ─────────────────►│   + Dead Letter Queue         │
              │   admin_query_audit        │                                         └──────────────┬───────────────┘
              │   chat_history             │                                                        │
              └────────────────────────────┘                                         ┌──────────────▼───────────────┐
                                                                                     │  Lambda (Alert Dispatcher)    │
                                                                                     │  SQS-triggered               │
                                                                                     │  3x retry, DLQ fallback      │
                                                                                     └──────────────┬───────────────┘
                                                                                                    │
                                                                                     ┌──────────────▼───────────────┐
                                                                                     │   Slack Webhook + SNS topic   │
                                                                                     │   (email/SMS backup)          │
                                                                                     └──────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                    Supporting Services                                            │
│                                                                                                   │
│   Secrets Manager + KMS        CloudWatch Logs + Metrics        X-Ray Tracing                    │
│   (API keys, DB creds)         Lambda Powertools integration     End-to-end request traces        │
│                                                                                                   │
│   CloudTrail (audit)           CloudWatch Alarms → SNS          GitHub Actions + SAM (CI/CD)     │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## Component Mapping: Demo to AWS

| Demo Component | AWS Service | Key Difference from ECS Plan |
|---|---|---|
| FastAPI on localhost | **Lambda + Mangum** behind **API Gateway HTTP API** | No containers, no task definitions, no ALB, scales to zero |
| Static HTML/CSS/JS served by FastAPI | **Amplify Hosting** (git-push deploy) | Decoupled from API; no manual S3 sync commands |
| SQLite (`dharmasala.db`) | **Aurora Serverless v2 PostgreSQL** | SQL-compatible; pgvector adds embedding similarity |
| `find_all_phashes()` in-memory scan | **pgvector `<=>` operator** (ANN index in Aurora) | O(1) nearest-neighbor vs O(n) full table scan |
| Local filesystem (`storage/`) | **S3** with pre-signed PUT URLs | Image upload bypasses Lambda entirely |
| `print()` / console alert logging | **SQS + Lambda** (Alert Dispatcher) | Async, retryable, DLQ-backed |
| Simple `admin_password` check | **Cognito User Pool** + **JWT Authorizer** | RBAC with `volunteer` and `admin` groups |
| Anthropic direct API key | **Amazon Bedrock** (Claude models) | No direct key management; IAM-controlled access |
| No session cache needed | *(no ElastiCache)* | Chat history in Aurora is sufficient at this scale |
| No vector DB needed | *(no OpenSearch cluster)* | pgvector in Aurora handles both SQL + similarity queries |
| `.env` file | **Secrets Manager + KMS** | Encrypted, rotatable, audited |
| No CI/CD | **GitHub Actions + AWS SAM** | Single `sam deploy` replaces CodePipeline + CodeBuild |

---

## Component Details

### Compute: Lambda + Mangum

The FastAPI application runs inside AWS Lambda using the
[Mangum](https://mangum.fastapiexpert.com/) ASGI adapter. Mangum translates API Gateway HTTP API
events into ASGI requests that FastAPI handles identically to a normal HTTP server. The
application code requires only one addition: `handler = Mangum(app)`.

| Parameter | Value | Rationale |
|---|---|---|
| Runtime | `python3.12` | Matches local dev environment |
| Memory | `512 MB` | Sufficient for image processing in-memory; increase to 1024 MB if cold start latency is a concern (more memory = more vCPU allocated) |
| Timeout | `60s` | Covers Claude Vision worst-case (30s) + DB operations |
| Concurrency | Unreserved (default) | Scales automatically; set reserved concurrency to 50 to protect Aurora connection limit |
| Provisioned concurrency | 2 on the triage function | Eliminates cold start latency on the primary user-facing endpoint |
| VPC | Private subnets (2 AZs) | Required for Aurora and RDS Proxy access |

**Lambda cold starts and VPC:** Placing Lambda in a VPC previously added 500ms–10s for ENI
attachment. Since 2020, AWS uses pre-allocated Hyperplane ENIs which reduce this to ~100ms.
Provisioned concurrency on the `/v1/triage/image` endpoint removes cold starts entirely for
that path.

**Static files:** `app.mount("/static", ...)` and the `FileResponse` returns for `index.html`
and `admin.html` are removed from FastAPI. Static assets are served by Amplify Hosting at the
CloudFront edge — Lambda never handles a static file request.

### API Layer: API Gateway HTTP API (v2)

HTTP API (v2) is chosen over REST API (v1) because it is 70% cheaper per request, has lower
latency (~6ms added overhead vs ~11ms for REST), and supports Lambda proxy integration and JWT
authorizers natively. Features not needed here (usage plans, API keys, request caching, custom
authorizer caching) are REST API-only and irrelevant for this workload.

| Concern | Configuration |
|---|---|
| Auth | Built-in JWT authorizer pointing at Cognito User Pool — no Lambda authorizer function needed |
| CORS | Configured on HTTP API to allow Amplify app origin |
| Rate limiting | WAF on CloudFront: 1000 requests per 5 minutes per IP |
| TLS | Automatic via API Gateway managed certificate |
| Custom domain | Route 53 → CloudFront → API Gateway (API Gateway domain mapped as CloudFront origin) |
| Payload limit | 10 MB max — image uploads use pre-signed S3 PUT URLs and never traverse this path |

### Image Upload: Two-Step Pre-Signed URL Pattern

The current `POST /v1/triage/image` endpoint receives raw image bytes as multipart/form-data
(`image_bytes = await image.read()`, `app.py:82`) and writes them to disk
(`blob_path.write_bytes(image_bytes)`, `app.py:97`). Both are incompatible with Lambda.

The replacement is a two-call flow that the frontend initiates:

```
Step 1:  GET /v1/triage/upload-url?filename=photo.jpg&content_type=image/jpeg
         ← { "upload_url": "https://s3.amazonaws.com/...", "s3_key": "uploads/{uuid}/photo.jpg", "expires_in": 300 }

Step 2:  PUT {upload_url}   (client → S3 directly, bypasses API Gateway and Lambda)
         Body: raw image bytes

Step 3:  POST /v1/triage/image
         Body: { "s3_key": "uploads/{uuid}/photo.jpg", "context": "...", "session_id": "...", "lat": ..., "lng": ... }
         ← triage result as before
```

In Step 3, Lambda reads the image from S3 (`s3.get_object(Bucket=..., Key=s3_key)`) and then
executes all existing logic (SHA-256, pHash, EXIF extraction, Claude Vision, pgvector similarity,
incident creation, alert dispatch). After processing, Lambda copies the object to its permanent
key (`incidents/{sha256}/{filename}`) and deletes the staging key.

This pattern keeps the synchronous request/response UX the frontend expects while eliminating the
payload size constraint entirely.

### Database: Aurora Serverless v2 PostgreSQL + pgvector

Aurora Serverless v2 provides a fully managed PostgreSQL-compatible database that scales ACUs
(Aurora Capacity Units) in 0.5 ACU increments from a configured minimum. Unlike v1, it scales
continuously rather than in discrete steps and supports all Aurora PostgreSQL features including
extensions.

**Configuration:**

| Parameter | Value |
|---|---|
| Engine | Aurora PostgreSQL 15.x |
| Min ACUs | 0.5 (scales down when idle) |
| Max ACUs | 8 (adjust based on load testing) |
| Multi-AZ | Enabled (reader instance in second AZ for HA) |
| Encryption | AES-256 via KMS |
| Backups | Automated, 7-day retention, continuous PITR |
| Extension | `pgvector` (installed via `CREATE EXTENSION vector`) |

**pgvector replaces OpenSearch for similarity search.** The incidents table gains one column:

```sql
ALTER TABLE incidents ADD COLUMN embedding vector(1536);
CREATE INDEX ON incidents USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
```

Embeddings are generated by Amazon Bedrock Titan Embeddings (or CLIP via a small Lambda) when
an incident is created. Nearest-neighbor queries replace the current `find_all_phashes()` full
table scan + in-memory Hamming distance loop:

```sql
-- Replaces: loading all phashes into Python and looping
SELECT incident_id, 1 - (embedding <=> %s::vector) AS similarity_score
FROM incidents
WHERE embedding IS NOT NULL
  AND incident_id != %s
ORDER BY embedding <=> %s::vector
LIMIT 5;
```

The existing pHash exact-duplicate check (`find_by_sha256`) is preserved as a fast pre-filter
before the embedding query.

**Schema changes from SQLite DDL** (all five tables require only mechanical type substitutions):

| SQLite type | PostgreSQL equivalent |
|---|---|
| `TEXT PRIMARY KEY` (UUID) | `UUID PRIMARY KEY DEFAULT gen_random_uuid()` |
| `TEXT NOT NULL` (timestamp) | `TIMESTAMPTZ NOT NULL DEFAULT NOW()` |
| `INTEGER PRIMARY KEY AUTOINCREMENT` | `BIGSERIAL PRIMARY KEY` |
| `REAL` | `DOUBLE PRECISION` |
| `TEXT` (JSON blobs) | `JSONB` (or `TEXT`) |
| `?` placeholders | `%s` placeholders (psycopg2) |

### Connection Pooling: RDS Proxy

Lambda can scale to hundreds of concurrent invocations, each opening a database connection.
Aurora Serverless v2 has a connection limit proportional to ACU count (roughly 90 connections per
ACU). Without connection pooling, a traffic spike exhausts connections and causes query failures.

RDS Proxy sits between Lambda and Aurora, maintaining a warm pool of connections and multiplexing
Lambda invocations onto them. Lambda connects to the proxy endpoint rather than the Aurora
cluster endpoint directly.

| Parameter | Value |
|---|---|
| Engine | PostgreSQL |
| Idle connection timeout | 1800s |
| Max connections | 90% of Aurora limit |
| IAM authentication | Enabled (no DB password in Lambda env) |
| Secrets Manager | Proxy retrieves DB credentials automatically |

**Lambda connects to the proxy, not Aurora directly.** The proxy endpoint is an environment
variable injected at deploy time.

### Frontend: Amplify Hosting

Amplify Hosting replaces the manual `aws s3 sync` + CloudFront distribution configuration from
the ECS plan. It connects directly to the GitHub repository and rebuilds/deploys static assets on
every push to the configured branch.

| Feature | Behaviour |
|---|---|
| Build trigger | Push to `main` → automatic deploy |
| Environment variables | Injected at build time (API endpoint URL, Cognito pool ID) |
| Custom domain | Configured in Amplify console, provisions ACM cert automatically |
| PR previews | Each pull request gets an ephemeral preview URL |
| CloudFront | Built-in, no separate distribution to manage |

The `app.mount("/static", ...)` call in `app.py:48` and the `serve_ui()` / `serve_admin()`
route handlers (`app.py:51–57`) are deleted. The frontend makes API calls to the API Gateway
endpoint rather than the same origin.

### Authentication: Cognito + HTTP API JWT Authorizer

The HTTP API JWT authorizer validates Cognito-issued JWTs at the API Gateway layer before the
request reaches Lambda. Lambda itself does not verify tokens — it reads claims from the
request context injected by API Gateway.

**User groups:**

| Group | Access |
|---|---|
| (none / unauthenticated) | `POST /v1/triage/image`, `POST /v1/chat/query`, `GET /v1/triage/upload-url` |
| `volunteer` | Above + `GET /v1/incidents/{id}`, `POST /v1/location/update` |
| `admin` | All endpoints including `/v1/admin/*` |

The `admin_password` query parameter and form field checks throughout `app.py` are replaced with
a single middleware function that reads `event.requestContext.authorizer.jwt.claims['cognito:groups']`.

Volunteers log in via Cognito Hosted UI (no custom auth UI needed). Admin accounts are created
manually in the Cognito console.

### AI: Amazon Bedrock

Bedrock provides access to Claude models via IAM-controlled API calls with no API key to manage
in Secrets Manager. The Lambda execution role is granted `bedrock:InvokeModel` permission for
the specific Claude model ARN.

The `services/triage.py` and `services/admin_analytics.py` Anthropic SDK calls are replaced with
the `boto3` Bedrock Runtime client (`bedrock-runtime`). Request/response structure is equivalent;
the Anthropic SDK wraps the same API.

**Anthropic direct API** remains a fallback option: store the key in Secrets Manager and use
the existing SDK. Bedrock is preferred because it eliminates credential rotation and provides
AWS-native cost attribution.

### Alert Pipeline: SQS + Lambda (Alert Dispatcher)

Identical to the ECS architecture plan. `services/alerts.py` publishes to SQS instead of
calling Slack directly. A separate Lambda function (not the FastAPI Lambda) is triggered by SQS,
sends the Slack webhook, and on failure publishes to an SNS topic for email/SMS backup.

| Component | Configuration |
|---|---|
| Queue visibility timeout | 300s (covers Slack retry time) |
| Max receive count | 3 |
| Dead Letter Queue | Separate SQS queue; CloudWatch alarm fires if DLQ depth > 0 |
| Lambda trigger | `BatchSize: 10`, `ReportBatchItemFailures: true` (partial batch retry) |

### Observability: Lambda Powertools

[AWS Lambda Powertools for Python](https://docs.powertools.aws.dev/lambda/python/) is a single
library that replaces CloudWatch structured logging setup, X-Ray tracing boilerplate, and custom
metric emission. It is added as a Lambda layer (no package size impact) and configured via
environment variables.

```python
from aws_lambda_powertools import Logger, Tracer, Metrics
from aws_lambda_powertools.metrics import MetricUnit

logger = Logger()      # Structured JSON logs → CloudWatch automatically
tracer = Tracer()      # X-Ray subsegments on every decorated function
metrics = Metrics()    # Custom metrics without CloudWatch SDK calls

@tracer.capture_method
def analyze_image(...):
    ...
    metrics.add_metric("TriageLatencyMs", MetricUnit.Milliseconds, latency_ms)
    metrics.add_metric("SeverityScore", MetricUnit.Count, severity_score)
```

**CloudWatch Alarms:**

| Metric | Threshold | Action |
|---|---|---|
| Lambda error rate | > 5% over 5 minutes | SNS → email |
| P95 duration | > 4000ms (text), > 8000ms (image) | SNS → email |
| SQS DLQ depth | > 0 | SNS → email |
| Aurora CPU | > 80% | SNS → email |
| Lambda concurrency | > 40 | SNS → warning |

### IaC: AWS SAM

AWS SAM (Serverless Application Model) is a CloudFormation superset that defines the entire
stack — API Gateway, Lambda, SQS, S3 bucket, IAM roles, CloudWatch alarms — in a single
`template.yaml`. Deployment is two commands:

```bash
sam build
sam deploy --config-env prod
```

Key SAM resources:

```yaml
AWSTemplateFormatVersion: '2010-09-09'
Transform: AWS::Serverless-2016-10-31

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

Resources:

  ChatApi:
    Type: AWS::Serverless::HttpApi
    Properties:
      Auth:
        DefaultAuthorizer: CognitoAuth
        Authorizers:
          CognitoAuth:
            JwtConfiguration:
              issuer: !Sub https://cognito-idp.${AWS::Region}.amazonaws.com/${CognitoUserPool}
              audience: [!Ref CognitoUserPoolClient]

  ChatbotFunction:
    Type: AWS::Serverless::Function
    Properties:
      Handler: app.handler              # Mangum entry point
      CodeUri: .
      VpcConfig:
        SecurityGroupIds: [!Ref LambdaSecurityGroup]
        SubnetIds: !Ref PrivateSubnetIds
      Environment:
        Variables:
          SECRET_ARN: !Ref AppSecrets
          IMAGE_BUCKET: !Ref ImageBucket
          DB_PROXY_ENDPOINT: !GetAtt RDSProxy.Endpoint
          SQS_ALERT_QUEUE_URL: !Ref AlertQueue
      Policies:
        - AWSSecretsManagerGetSecretValuePolicy: {SecretArn: !Ref AppSecrets}
        - S3CrudPolicy: {BucketName: !Ref ImageBucket}
        - SQSSendMessagePolicy: {QueueName: !GetAtt AlertQueue.QueueName}
        - Statement: [{Effect: Allow, Action: bedrock:InvokeModel, Resource: "arn:aws:bedrock:*::foundation-model/anthropic.claude-3-5-sonnet*"}]
      Events:
        AnyRoute:
          Type: HttpApi
          Properties: {ApiId: !Ref ChatApi, Path: /{proxy+}, Method: ANY}

  AlertDispatchFunction:
    Type: AWS::Serverless::Function
    Properties:
      Handler: services.alert_dispatcher.handler
      CodeUri: .
      Policies:
        - AWSSecretsManagerGetSecretValuePolicy: {SecretArn: !Ref AppSecrets}
        - SQSPollerPolicy: {QueueName: !GetAtt AlertQueue.QueueName}
      Events:
        SQSTrigger:
          Type: SQS
          Properties:
            Queue: !GetAtt AlertQueue.Arn
            BatchSize: 10
            FunctionResponseTypes: [ReportBatchItemFailures]

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

  AlertQueue:
    Type: AWS::SQS::Queue
    Properties:
      VisibilityTimeout: 300
      RedrivePolicy:
        deadLetterTargetArn: !GetAtt AlertDLQ.Arn
        maxReceiveCount: 3

  AlertDLQ:
    Type: AWS::SQS::Queue
```

### CI/CD: GitHub Actions + SAM

Replaces CodePipeline + CodeBuild + CodeDeploy. A single workflow file handles test, build, and
deploy with OIDC authentication (no long-lived AWS keys in GitHub secrets).

```yaml
# .github/workflows/deploy.yml
name: Deploy
on:
  push:
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
      - run: python -m pytest tests/ -v

  deploy:
    needs: test
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE_ARN }}
          aws-region: ap-south-1
      - uses: aws-actions/setup-sam@v2
      - run: sam build
      - run: sam deploy --config-env prod --no-confirm-changeset --no-fail-on-empty-changeset
```

No Dockerfile. No ECR push. No ECS task definition update. The deploy step uploads the Python
package directly to S3 and updates the Lambda function code.

---

## Security

| Control | Implementation |
|---|---|
| Authentication | Cognito User Pool; JWT validated by API Gateway before Lambda invocation |
| Authorization | RBAC via Cognito groups; enforced in FastAPI middleware reading JWT claims |
| Network | Lambda and Aurora in private VPC subnets; no public endpoints on DB or proxy |
| Secrets | Secrets Manager with KMS encryption; Lambda retrieves at cold-start only |
| Encryption in transit | TLS 1.3 (API Gateway, CloudFront, RDS Proxy all enforce) |
| Encryption at rest | KMS on Aurora, S3 (SSE-KMS), Secrets Manager |
| WAF | CloudFront WAF: OWASP core rule set, rate limiting (1000 req/5 min/IP), bot control |
| Image access | Pre-signed S3 PUT URLs (5-min TTL for upload); GET URLs issued by Lambda on demand |
| Lambda permissions | Least-privilege IAM roles per function; no `*` resource ARNs |
| DB access | Lambda connects to RDS Proxy using IAM auth token; no hardcoded password |
| Audit | CloudTrail for all AWS API calls; `admin_query_audit` table for NL-to-SQL queries |

---

## Cost Estimate (Monthly, MVP Scale)

Assumptions: ~1,000 triage image requests/month, ~5,000 chat requests/month, ~50 GB-hours
Aurora runtime (with scale-down during idle), India region (ap-south-1).

| Service | Configuration | Est. Cost |
|---|---|---|
| Lambda | ~6M invocations/month (generous) | ~$2 |
| API Gateway HTTP API | ~6M requests | ~$6 |
| Aurora Serverless v2 | 0.5 ACU min, ~50 GB-hours active | ~$20–35 |
| RDS Proxy | ~1 vCPU-equivalent | ~$11 |
| S3 | 10 GB storage + requests | ~$2 |
| CloudFront + WAF | 10 GB transfer | ~$5 |
| Amplify Hosting | Static hosting | ~$0–2 |
| Bedrock (Claude) | ~6,000 requests/month | ~$15–30 |
| SQS | ~1,000 alert messages | ~$0 |
| Secrets Manager | 3 secrets | ~$2 |
| CloudWatch + X-Ray | Standard Lambda Powertools usage | ~$8 |
| **Total** | | **~$70–103/month** |

**Compared to ECS plan (~$130–160/month):** Savings come from eliminating ALB ($20), ElastiCache
($15), OpenSearch ($40+ for a single-node cluster), and ECR storage. Aurora Serverless v2 +
RDS Proxy partially offset this.

**Cost floor note:** Aurora Serverless v2 does not auto-pause (that was v1). At 0.5 ACU minimum,
the DB costs ~$0.06/hour = ~$44/month at rest. For a low-traffic rescue org, evaluate whether
an `db.t4g.micro` RDS PostgreSQL instance (~$12/month) with RDS Proxy is a better starting
point, upgrading to Aurora Serverless v2 when traffic warrants autoscaling.

---

## Scalability Path

| Phase | Traffic | Configuration |
|---|---|---|
| MVP | < 100 req/day | 0.5 ACU Aurora, no provisioned Lambda concurrency, single AZ Amplify |
| Growth | 100–1,000 req/day | Provisioned concurrency on triage endpoint, Aurora scales ACUs automatically |
| Scale | > 1,000 req/day | Aurora max ACUs 16, reserved Lambda concurrency with auto-scaling, CloudFront caching on GET endpoints |
| Multi-region | Global rollout | Lambda@Edge for auth token validation at edge, Aurora Global Database, Amplify multi-region |

---

## What This Architecture Does Not Include

Compared to the ECS plan, the following are **intentionally absent**:

| Omitted | Reason |
|---|---|
| ECS Fargate cluster | Replaced by Lambda |
| Application Load Balancer | Replaced by API Gateway HTTP API |
| ECR repository / Docker | No containers — Lambda deploys Python zip directly |
| ElastiCache Redis | Chat history in Aurora is sufficient; no session cache cluster needed |
| OpenSearch cluster | pgvector in Aurora handles embedding similarity |
| CodePipeline / CodeBuild | Replaced by GitHub Actions + SAM (simpler, no artifact buckets) |
| NAT Gateway (optional) | Lambda can use VPC endpoints for S3, Secrets Manager, SQS, Bedrock — eliminating NAT Gateway saves ~$32/month |
