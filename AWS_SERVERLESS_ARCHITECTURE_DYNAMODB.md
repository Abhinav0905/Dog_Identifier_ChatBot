# Dharamsala Animal Rescue Chatbot — Serverless AWS Architecture (Config 1: DynamoDB, No VPC)

## Overview

This document describes the lowest-overhead serverless production architecture for the Dharamsala
Animal Rescue Chatbot. The defining characteristics of this configuration are:

- **No VPC** — Lambda runs in the AWS-managed network environment. No subnets, security groups,
  NAT Gateways, or VPC endpoints to configure. This is the single largest operational simplification.
- **DynamoDB** — Fully serverless, pay-per-request NoSQL. No minimum capacity, no connection
  pooling, no cluster sizing. Scales to zero cost at zero traffic.
- **DynamoDB TTL** for chat session history — eliminates ElastiCache Redis entirely.
- **pHash in-memory similarity** preserved at MVP scale — no OpenSearch cluster, no pgvector.
  Bedrock Knowledge Bases is the upgrade path when dataset exceeds ~10,000 incidents.
- **Admin analytics redesigned** — the NL-to-SQL system is replaced with predefined DynamoDB
  queries for common admin questions, with Athena as the growth path for arbitrary SQL.

**Trade-off vs Config 2 (Aurora):** Config 1 is cheaper and simpler to operate, but the relational
schema from SQLite cannot be migrated mechanically — the data model requires redesign. The admin
NL-to-SQL analytics feature loses its full flexibility at launch and is rebuilt incrementally.

**Design targets:** 99.5%+ availability, P95 <4s text / <8s image, ~$40–60/month at MVP traffic,
zero VPC infrastructure to manage.

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
      │                   │                                      │ JWT validation
      │  Auto-deploys on  │                         ┌────────────▼────────────┐
      │  git push to main │                         │   Cognito User Pool     │
      └───────────────────┘                         └────────────┬────────────┘
                                                                 │
                                                    ┌────────────▼────────────┐
                                                    │         Lambda           │
                                                    │   FastAPI + Mangum       │
                                                    │   python3.12             │
                                                    │   512 MB / 60s timeout   │
                                                    │                          │
                                                    │   NO VPC — runs in       │
                                                    │   AWS-managed network    │
                                                    │   Fastest cold starts    │
                                                    └────┬──────────┬──────────┘
                                                         │          │
                     ┌───────────────────────────────────┘          └────────────────────────────┐
                     │                                                                            │
      ┌──────────────▼──────────────┐                                              ┌─────────────▼────────────┐
      │         DynamoDB             │                                              │        S3 Bucket          │
      │   5 tables, pay-per-request  │                                              │  dharmasala-images-{env}  │
      │   No VPC, no connection mgmt │                                              │                           │
      │                              │                                              │  Client uploads via       │
      │   incidents     (+ 3 GSIs)   │                                              │  pre-signed PUT URL       │
      │   alerts        (+ 1 GSI)    │                                              │  (bypasses Lambda         │
      │   triage_events (+ 1 GSI)    │                                              │   payload limit)          │
      │   admin_audit                │                                              └───────────────────────────┘
      │   chat_history  (+ TTL)      │
      │     └── TTL replaces Redis   │                                              ┌───────────────────────────┐
      └──────────────────────────────┘                                              │      Amazon Bedrock        │
                                                                                    │   Claude 3.5 Sonnet        │
                                                                                    │   (vision triage,          │
                                                                                    │    chat, admin queries)    │
                                                                                    └───────────────────────────┘

                                                    ┌────────────────────────────┐
                                                    │       SQS Alert Queue       │
                                                    │   + Dead Letter Queue       │
                                                    └────────────┬───────────────┘
                                                                 │
                                                    ┌────────────▼───────────────┐
                                                    │  Lambda (Alert Dispatcher)  │
                                                    │  SQS-triggered              │
                                                    │  3x retry, DLQ fallback     │
                                                    └────────────┬───────────────┘
                                                                 │
                                                    ┌────────────▼───────────────┐
                                                    │   Slack Webhook + SNS topic │
                                                    │   (email/SMS backup)        │
                                                    └────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                    Supporting Services                                            │
│                                                                                                   │
│   Secrets Manager + KMS         CloudWatch Logs + Metrics       X-Ray Tracing                    │
│   (API keys only — no DB creds)  Lambda Powertools integration   End-to-end request traces        │
│                                                                                                   │
│   CloudTrail (audit)            CloudWatch Alarms → SNS         GitHub Actions + SAM (CI/CD)     │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## Component Mapping: Demo to AWS

| Demo Component | AWS Service | Key Difference vs Config 2 (Aurora) |
|---|---|---|
| FastAPI on localhost | **Lambda + Mangum** + **API Gateway HTTP API** | Identical — same Lambda/Mangum approach |
| Static HTML/CSS/JS served by FastAPI | **Amplify Hosting** | Identical |
| SQLite (`dharmasala.db`) — relational schema | **DynamoDB** (5 tables, pay-per-request) | Schema redesigned as key-value/document model; no SQL |
| `find_all_phashes()` in-memory scan | **DynamoDB Scan** + in-memory pHash comparison | Same algorithm; DynamoDB Scan replaces SQLite full table read |
| `execute_readonly_sql()` NL-to-SQL | **Predefined DynamoDB queries** + Athena growth path | SQL flexibility replaced with curated query set; full NL-to-SQL restorable via Athena |
| Local filesystem (`storage/`) | **S3** with pre-signed PUT URLs | Identical |
| `print()` / console alert logging | **SQS + Lambda** (Alert Dispatcher) | Identical |
| Simple `admin_password` check | **Cognito User Pool** + **JWT Authorizer** | Identical |
| Anthropic direct API key | **Amazon Bedrock** | Identical |
| ElastiCache Redis for session cache | *(none — DynamoDB TTL handles it)* | **Eliminated entirely** — DynamoDB TTL on chat_history replaces Redis |
| No VPC needed | *(no VPC configured)* | **Key advantage** — no subnets, NAT Gateways, security groups, or ENI management |
| `.env` file | **Secrets Manager + KMS** (Slack webhook only — no DB password) | Simpler than Config 2 — no DB credentials to store or rotate |

---

## DynamoDB Data Model

This is the most significant architectural difference from both the demo and Config 2. The five
SQLite tables become five DynamoDB tables, each designed around its primary access patterns.

### Table 1: `incidents`

**Primary key:** `incident_id` (String, UUID)

**Access patterns and supporting indexes:**

| Access Pattern | How Served |
|---|---|
| Get single incident by ID | Table primary key |
| Check for exact duplicate by SHA-256 | GSI: `sha256-index` (PK: `image_sha256`) |
| List incidents filtered by status, newest first | GSI: `status-created_at-index` (PK: `status`, SK: `created_at`) |
| List incidents filtered by severity, newest first | GSI: `severity-created_at-index` (PK: `triage_severity`, SK: `created_at`) |
| Load all phashes for similarity scan | Table Scan with ProjectionExpression (MVP acceptable; Bedrock KB at scale) |

**Item attributes:**

```
incident_id            String  (UUID, partition key)
created_at             String  (ISO-8601 UTC)
updated_at             String  (ISO-8601 UTC)
reporter_session_id    String
image_s3_key           String  (replaces image_blob_path — S3 object key)
image_sha256           String
image_phash            String
lat                    Number
lng                    Number
location_source        String
triage_severity        String  ('low' | 'moderate' | 'high' | 'critical')
triage_severity_score  Number
triage_confidence      Number
triage_summary         String
distress_flags         List    (DynamoDB List of Strings)
similar_incident_id    String
similarity_score       Number
status                 String  ('new' | 'in_progress' | 'alerted' | 'resolved')
```

### Table 2: `chat_history`

**Primary key:** `session_id` (String) + `message_id` (String, sort key)

`message_id` is a timestamp-prefixed UUID (`{ISO-8601}#{uuid}`) so items sort chronologically
without a separate index. DynamoDB TTL on `expires_at` (Unix epoch, 24 hours) automatically
purges old sessions — this replaces ElastiCache Redis entirely.

```
session_id   String  (partition key)
message_id   String  (sort key: "{created_at}#{uuid4}" for chronological ordering)
role         String  ('user' | 'assistant')
content      String
created_at   String  (ISO-8601)
expires_at   Number  (Unix epoch timestamp — DynamoDB TTL, item deleted after 24 hours)
```

**Why this replaces Redis:** Redis was proposed for session/chat history storage with a 24-hour TTL.
DynamoDB TTL provides the same expiry mechanism natively at zero additional cost and no cluster
to manage. The chat_history table is the session store.

### Table 3: `alerts`

**Primary key:** `alert_id` (String, UUID)
**GSI:** `incident_id-sent_at-index` (PK: `incident_id`, SK: `sent_at`)

```
alert_id       String  (partition key)
incident_id    String
alert_channel  String  ('sqs' | 'slack' | 'console')
trigger_reason String
sent_at        String  (ISO-8601)
ack_status     String  ('pending' | 'acknowledged')
ack_by         String
ack_at         String
```

### Table 4: `triage_events`

**Primary key:** `event_id` (String, UUID)
**GSI:** `incident_id-created_at-index` (PK: `incident_id`, SK: `created_at`)

```
event_id               String  (partition key)
incident_id            String
model_version          String
raw_output             String
postprocessed_output   String
latency_ms             Number
created_at             String  (ISO-8601)
```

### Table 5: `admin_query_audit`

**Primary key:** `query_id` (String, UUID). Simple append log — no GSI required.

```
query_id       String  (partition key)
admin_user_id  String
nl_query       String
resolved_query String  (description of predefined query or PartiQL statement used)
executed_at    String  (ISO-8601)
row_count      Number
status         String  ('success' | 'failed' | 'blocked')
```

---

## Component Details

### Compute: Lambda + Mangum (No VPC)

Lambda runs outside any VPC. This is the primary operational advantage of Config 1:

| Without VPC (Config 1) | With VPC (Config 2) |
|---|---|
| No ENI provisioning delay | ~100ms VPC ENI attachment on cold start |
| No subnet/SG configuration | 2 private subnets, 2+ security groups |
| No NAT Gateway or VPC endpoints | NAT Gateway (~$32/month) or Interface Endpoints |
| DynamoDB accessed over IAM-gated HTTPS endpoint | Aurora accessed via RDS Proxy in private subnet |
| Secrets Manager accessed over IAM-gated HTTPS | Secrets Manager via VPC endpoint or NAT |

Lambda connects to DynamoDB and Secrets Manager via AWS service endpoints over HTTPS with IAM
authentication. DynamoDB has no separate network access control; the Lambda execution role IAM
policy is the only access gate — no credentials, no connection string, no password rotation.

| Parameter | Value |
|---|---|
| Runtime | `python3.12` |
| Memory | `512 MB` |
| Timeout | `60s` |
| Reserved concurrency | 50 (caps DynamoDB on-demand burst to predictable cost) |
| Provisioned concurrency | 2 on triage function (eliminates cold starts on primary path) |
| VPC | None |

### API Layer: API Gateway HTTP API (v2)

Identical to Config 2. HTTP API (v2) over REST API (v1) for 70% lower cost per request and
lower latency. JWT authorizer validates Cognito tokens at the Gateway layer before Lambda is
invoked — Lambda itself never handles token verification.

### Image Upload: Two-Step Pre-Signed URL Pattern

Identical to Config 2. The `app.py:63` multipart upload endpoint is replaced with:

1. `GET /v1/triage/upload-url` — Lambda issues a pre-signed S3 PUT URL (5-min TTL)
2. Client uploads image directly to S3, bypassing API Gateway's 10 MB limit entirely
3. `POST /v1/triage/image` — Lambda receives `s3_key`, reads image from S3, runs full triage

### Similarity Search: pHash via DynamoDB Scan

`similarity.py:36-64` loads all phashes via `db.find_all_phashes()` and computes Hamming
distance in Python. The algorithm is unchanged — only the data retrieval layer changes from
SQLite to a DynamoDB Scan with projection:

```python
# find_all_phashes() in DynamoDB mode:
table.scan(
    ProjectionExpression='incident_id, image_phash',
    FilterExpression=Attr('image_phash').exists() & Attr('image_phash').ne(''),
)
# Paginate through all pages if > 1 MB of results
```

**Scale threshold:** A projected Scan across 10,000 incidents reads ~1–2 MB and costs ~$0.0005
per call. The in-memory Hamming comparison across 10,000 hashes runs in < 50ms in Lambda.
Both are operationally acceptable through early growth.

**Upgrade path:** When incidents exceed ~10,000 rows, replace the Scan + in-memory comparison
with Amazon Bedrock Knowledge Bases (managed vector store on OpenSearch Serverless). The only
callsite to update is `check_similar_images()` in `similarity.py:36`.

### Admin Analytics: Predefined DynamoDB Query Library

The `admin_analytics.py:16-37` NL-to-SQL system generates SQLite SELECT with `GROUP BY`,
`COUNT`, and `date()` functions. DynamoDB does not support these natively. The service is
rebuilt as a two-layer system without changing the public API contract (`process_nl_query()`
returns the same dict structure as before).

**Layer 1 — Intent classifier + predefined queries (MVP launch):**

Claude classifies the admin's natural language question into one of six query types. A Python
dispatcher then runs the appropriate DynamoDB operation. Claude still generates the natural
language summary of results — only the SQL generation step is replaced.

| Admin question | DynamoDB implementation |
|---|---|
| Count by severity | Query each severity GSI, `Select=COUNT`, aggregate in Python |
| Count by status | Query each status GSI, `Select=COUNT`, aggregate in Python |
| Recent incidents | `get_incidents_list()` with optional status/severity filter |
| High/critical last N days | Query `severity-created_at-index` with `created_at >= cutoff` |
| Unacknowledged alerts | Scan alerts table, `FilterExpression=Attr('ack_status').eq('pending')` |
| Total count | Scan incidents with `Select=COUNT` |

The six predefined types cover every query in `_fallback_nl_to_sql` (`admin_analytics.py:118-160`).
Unknown queries fall back to keyword matching, exactly as the original fallback does.

**Layer 2 — Athena for arbitrary SQL (growth path):**

Enable DynamoDB Streams → Lambda/Firehose → S3 Parquet → Athena. The `execute_readonly_sql()`
function is re-implemented against `athena.start_query_execution()`. The full NL-to-SQL prompt
from `admin_analytics.py:16` is restored verbatim — only the execution backend changes.

### Authentication: Cognito + HTTP API JWT Authorizer

Identical to Config 2. `volunteer` and `admin` Cognito groups control access. The `admin_password`
query parameter checks in `app.py:281, 293, 313` are replaced with a single JWT group claim
validator in FastAPI middleware.

### AI: Amazon Bedrock

Identical to Config 2. IAM-controlled access to Claude models; `bedrock:InvokeModel` permission
on the Lambda execution role. Config 1 Secrets Manager stores only `SLACK_WEBHOOK_URL` — no
database password (DynamoDB uses IAM auth, not username/password).

### Session Management: DynamoDB TTL

The original ECS architecture proposed ElastiCache Redis for 24-hour session TTL. DynamoDB's
native TTL attribute provides identical behaviour:

- `chat_history` items written with `expires_at = int(time.time()) + 86400`
- DynamoDB background process deletes expired items within 48 hours of `expires_at`
- Zero configuration beyond enabling TTL on the table (one CLI command or SAM property)
- Zero additional cost — TTL deletes are not charged as write units

`get_chat_history(session_id, limit=20)` becomes a DynamoDB Query on the `session_id` partition
key with `ScanIndexForward=False` (newest first) and `Limit=20`, then reversed for chronological
order — identical return value to the SQLite version.

### Alert Pipeline: SQS + Lambda (Alert Dispatcher)

Identical to Config 2. `services/alerts.py` publishes to SQS; a separate `alert_dispatcher.py`
Lambda handles Slack delivery with retry logic and DLQ fallback.

### Observability: Lambda Powertools

Identical to Config 2. Structured JSON logging, X-Ray tracing, and custom metrics via Lambda
Powertools decorators. One additional DynamoDB-specific metric to monitor:

```python
metrics.add_metric("PhashScanItemCount", MetricUnit.Count, len(results))
```

Set a CloudWatch alarm when `PhashScanItemCount > 5000` as the signal to evaluate the Bedrock
Knowledge Bases upgrade. This makes the scale threshold operationally observable.

### IaC: AWS SAM

DynamoDB table definitions in SAM are significantly simpler than Aurora + RDS Proxy. The
entire `template.yaml` has no VPC resources, no DB subnet groups, no proxy configuration:

```yaml
# Compare: Config 2 Aurora + RDS Proxy = ~150 lines of VPC/subnet/SG/proxy YAML
# Config 1 DynamoDB = ~15 lines per table, no networking resources at all

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
    AttributeDefinitions:
      - {AttributeName: session_id, AttributeType: S}
      - {AttributeName: message_id, AttributeType: S}
    KeySchema:
      - {AttributeName: session_id, KeyType: HASH}
      - {AttributeName: message_id, KeyType: RANGE}
```

The Lambda function in `template.yaml` has no `VpcConfig` block — the most visible structural
difference from the Config 2 template.

### CI/CD: GitHub Actions + SAM

Identical workflow to Config 2. The `sam deploy` step passes fewer parameters (no VPC IDs,
subnet IDs, security groups, or RDS proxy endpoint). GitHub Secrets needed: 4 vs 7 in Config 2.

---

## Security

| Control | Implementation |
|---|---|
| Authentication | Cognito User Pool; JWT validated by API Gateway before Lambda invocation |
| Authorization | RBAC via Cognito groups; enforced in FastAPI middleware reading JWT claims |
| Network | No VPC — access controlled entirely by IAM; DynamoDB and Secrets Manager are IAM-gated |
| Secrets | Secrets Manager: `SLACK_WEBHOOK_URL` only (no DB password — DynamoDB uses IAM auth) |
| Encryption in transit | TLS 1.3 everywhere (API Gateway, CloudFront, DynamoDB SDK) |
| Encryption at rest | DynamoDB SSE enabled (KMS-managed), S3 SSE-KMS |
| WAF | CloudFront WAF: OWASP core rule set, rate limiting (1000 req/5 min/IP), bot control |
| Image access | Pre-signed S3 PUT URLs (5-min TTL); GET URLs issued by Lambda on demand |
| Lambda permissions | Least-privilege per-table DynamoDB IAM actions, scoped to specific table ARNs |
| Audit | CloudTrail for all AWS API calls; `admin_query_audit` DynamoDB table for admin queries |

**Least-privilege IAM policy for Lambda → DynamoDB:**

```json
{
  "Effect": "Allow",
  "Action": [
    "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
    "dynamodb:Query", "dynamodb:Scan"
  ],
  "Resource": [
    "arn:aws:dynamodb:ap-south-1:*:table/dharmasala-incidents-prod",
    "arn:aws:dynamodb:ap-south-1:*:table/dharmasala-incidents-prod/index/*",
    "arn:aws:dynamodb:ap-south-1:*:table/dharmasala-chat-history-prod",
    "arn:aws:dynamodb:ap-south-1:*:table/dharmasala-alerts-prod",
    "arn:aws:dynamodb:ap-south-1:*:table/dharmasala-alerts-prod/index/*",
    "arn:aws:dynamodb:ap-south-1:*:table/dharmasala-triage-events-prod",
    "arn:aws:dynamodb:ap-south-1:*:table/dharmasala-admin-audit-prod"
  ]
}
```

---

## Cost Estimate (Monthly, MVP Scale)

Assumptions: ~1,000 triage image requests/month, ~5,000 chat requests/month, India region.

| Service | Configuration | Est. Cost |
|---|---|---|
| Lambda | ~6M invocations (generous headroom) | ~$2 |
| API Gateway HTTP API | ~6M requests | ~$6 |
| DynamoDB | On-demand, ~100K write units + ~500K read units/month | ~$1–3 |
| S3 | 10 GB storage + requests | ~$2 |
| CloudFront + WAF | 10 GB transfer | ~$5 |
| Amplify Hosting | Static hosting | ~$0–2 |
| Bedrock (Claude) | ~6,000 API calls/month | ~$15–30 |
| SQS | ~1,000 alert messages | ~$0 |
| Secrets Manager | 1 secret | ~$1 |
| CloudWatch + X-Ray | Lambda Powertools standard usage | ~$8 |
| **Total** | | **~$40–59/month** |

**Vs Config 2 (Aurora, ~$70–103/month):** The data tier drops from ~$55/month (Aurora +
RDS Proxy) to ~$2/month (DynamoDB on-demand at MVP traffic). No VPC eliminates NAT Gateway
(~$32/month) and VPC Interface Endpoints.

**Vs original ECS plan (~$130–160/month):** ~$90–100/month savings — ECS, ALB, ElastiCache,
OpenSearch, and RDS are all eliminated.

**DynamoDB at scale:** At 1M triage requests/month (100× MVP), DynamoDB write costs are ~$15/month.
Dramatically cheaper than a provisioned RDS instance at any traffic level.

**DynamoDB true scale-to-zero:** At 0 requests, DynamoDB on-demand incurs $0 in request charges.
Storage costs are ~$0.25/GB-month (minimal for incident records). Aurora Serverless v2 has a
minimum 0.5 ACU floor of ~$44/month regardless of traffic.

---

## Scalability Path

| Phase | Traffic | Configuration |
|---|---|---|
| MVP | < 100 req/day | DynamoDB on-demand, pHash Scan for similarity, predefined admin queries |
| Growth | 100–1,000 req/day | Add Athena analytics (DynamoDB Streams → S3 → Athena); add Bedrock Knowledge Bases for embedding similarity |
| Scale | > 1,000 req/day | DynamoDB on-demand scales automatically; evaluate DynamoDB Accelerator (DAX) for read-heavy admin queries |
| Multi-region | Global deployment | DynamoDB Global Tables (active-active replication, minimal config change) |

---

## Honest Trade-offs vs Config 2

| Dimension | Config 1 (DynamoDB, No VPC) | Config 2 (Aurora + pgvector, VPC) |
|---|---|---|
| Operational overhead | Lowest | Low |
| Data migration effort | Highest — schema redesign required | Low — mechanical DDL type conversion |
| Admin NL-to-SQL at launch | 6 predefined query types | Full arbitrary SQL |
| Similarity search at launch | pHash Scan + in-memory (MVP acceptable) | pgvector ANN index (production-grade) |
| SQL query support | None without Athena | Full PostgreSQL |
| Monthly cost at MVP | ~$40–59 | ~$70–103 |
| Scale-to-zero cost | Yes ($0 at 0 requests) | No (~$44/month Aurora minimum) |
| Cold start performance | Fastest (no VPC ENI) | ~100ms VPC penalty |
| Networking to configure | Nothing | 2 subnets, 2 SGs, RDS Proxy |
| Secrets to manage | 1 (Slack webhook only) | 2 (Slack + DB password) |
| GitHub Secrets for CI/CD | 4 | 7 |
