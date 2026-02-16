# Dharmasala Animal Rescue Chatbot - AWS Production Architecture

## Overview

This document describes the target AWS architecture for the production deployment of the
Dharmasala Animal Rescue Chatbot, designed for scalability, reliability, and security as
outlined in the HLD (99.5%+ availability, P95 <4s text / <8s image).

---

## AWS Architecture Diagram

```
                            ┌──────────────┐
                            │  Route 53    │
                            │  DNS         │
                            └──────┬───────┘
                                   │
                            ┌──────▼───────┐
                            │  CloudFront  │
                            │  CDN         │
                            │  + WAF       │
                            └──────┬───────┘
                                   │
                    ┌──────────────┼──────────────┐
                    │              │              │
            ┌───────▼──────┐      │       ┌──────▼───────┐
            │  S3 Bucket   │      │       │  S3 Bucket   │
            │  (Static UI) │      │       │  (Image Blob)│
            └──────────────┘      │       └──────────────┘
                                  │
                           ┌──────▼───────┐
                           │     ALB      │
                           │ (App Load    │
                           │  Balancer)   │
                           └──────┬───────┘
                                  │
                    ┌─────────────┼─────────────┐
                    │             │             │
            ┌───────▼──────┐ ┌───▼──────┐ ┌───▼──────┐
            │  ECS Fargate │ │  ECS     │ │  ECS     │
            │  Task:       │ │  Task:   │ │  Task:   │
            │  API Service │ │  API     │ │  API     │
            │  (auto-scale)│ │  Service │ │  Service │
            └───────┬──────┘ └───┬──────┘ └───┬──────┘
                    │            │            │
         ┌──────────────────────────────────────────────┐
         │              Private VPC Subnet               │
         │                                               │
         │  ┌─────────────────────────────────────────┐ │
         │  │           Service Integrations            │ │
         │  │                                          │ │
         │  │  ┌──────────┐  ┌──────────┐            │ │
         │  │  │ Anthropic│  │  SQS     │            │ │
         │  │  │ Claude   │  │  Alert   │            │ │
         │  │  │ API      │  │  Queue   │            │ │
         │  │  │ (Bedrock │  └────┬─────┘            │ │
         │  │  │  or      │       │                   │ │
         │  │  │  direct) │  ┌────▼─────┐            │ │
         │  │  └──────────┘  │  Lambda  │            │ │
         │  │                │  Alert   │            │ │
         │  │                │  Dispatch│            │ │
         │  │                └────┬─────┘            │ │
         │  │                     │                   │ │
         │  │               ┌─────▼────┐             │ │
         │  │               │  Slack / │             │ │
         │  │               │  SNS     │             │ │
         │  │               └──────────┘             │ │
         │  └─────────────────────────────────────────┘ │
         │                                               │
         │  ┌──────────────────────────────────────────┐ │
         │  │            Data Tier                      │ │
         │  │                                           │ │
         │  │  ┌──────────────┐  ┌──────────────────┐  │ │
         │  │  │  RDS MySQL   │  │  ElastiCache     │  │ │
         │  │  │  (Multi-AZ)  │  │  Redis           │  │ │
         │  │  │              │  │  (Session/Cache)  │  │ │
         │  │  └──────────────┘  └──────────────────┘  │ │
         │  │                                           │ │
         │  │  ┌──────────────┐  ┌──────────────────┐  │ │
         │  │  │  OpenSearch  │  │  S3 (Images)     │  │ │
         │  │  │  (Vector     │  │  + Pre-signed    │  │ │
         │  │  │   Search)    │  │    URLs          │  │ │
         │  │  └──────────────┘  └──────────────────┘  │ │
         │  └──────────────────────────────────────────┘ │
         │                                               │
         │  ┌──────────────────────────────────────────┐ │
         │  │         Observability                     │ │
         │  │  CloudWatch Logs + Metrics                │ │
         │  │  X-Ray Traces                             │ │
         │  │  CloudWatch Alarms → SNS → PagerDuty     │ │
         │  └──────────────────────────────────────────┘ │
         └───────────────────────────────────────────────┘
                    │
         ┌──────────▼──────────┐
         │   Secrets Manager   │
         │   + KMS             │
         └─────────────────────┘
```

---

## Component Mapping: Demo to AWS

| Demo Component | AWS Production Service | Purpose |
|----------------|----------------------|---------|
| FastAPI on localhost | **ECS Fargate** behind **ALB** | Containerized API with auto-scaling |
| Static HTML/CSS/JS | **S3 + CloudFront** | Global CDN for static assets |
| SQLite | **RDS MySQL** (Multi-AZ) | Managed relational database with HA |
| Local filesystem (`storage/`) | **S3** with pre-signed URLs | Durable, scalable image blob storage |
| Console alert logging | **SQS + Lambda + Slack/SNS** | Async alert pipeline with retry |
| Simple password auth | **Cognito + JWT + ALB auth** | Federated identity, RBAC |
| Anthropic API (direct) | **Amazon Bedrock** or direct API | Managed AI model access |
| pHash similarity | **OpenSearch** with kNN | Vector similarity search at scale |
| In-process chat history | **ElastiCache Redis** | Session management and cache |
| `print()` logging | **CloudWatch + X-Ray** | Centralized logs, metrics, distributed traces |
| No WAF | **AWS WAF on CloudFront + ALB** | DDoS protection, rate limiting, IP filtering |
| `.env` file | **Secrets Manager + KMS** | Encrypted secret storage with rotation |

---

## AWS Service Details

### Compute: ECS Fargate

- **Container**: Docker image of the FastAPI application
- **Auto-scaling**: Target tracking on CPU/request count (min 2, max 10 tasks)
- **Health checks**: ALB health check on `/health` endpoint
- **Deployment**: Rolling update with circuit breaker

### Networking

- **VPC**: Isolated VPC with public/private subnets across 2+ AZs
- **ALB**: Application Load Balancer with TLS termination (ACM certificate)
- **CloudFront**: CDN for static assets and API caching (cache-control headers)
- **WAF**: Rate limiting (1000 req/min per IP), geo-blocking, SQL injection rules

### Database: RDS MySQL

- **Instance**: db.t3.medium (scalable)
- **Multi-AZ**: Automatic failover for HA
- **Backups**: Automated daily snapshots, 7-day retention
- **Encryption**: AES-256 at rest via KMS
- **Schema**: Same as demo SQLite schema, migrated to MySQL DDL

### Blob Storage: S3

- **Bucket**: `dharmasala-rescue-images-{env}`
- **Access**: Pre-signed URLs (15-min TTL) for image retrieval
- **Lifecycle**: Transition to S3-IA after 90 days, Glacier after 365 days
- **Encryption**: SSE-S3 or SSE-KMS

### AI/ML: Anthropic Claude

- **Option A**: Direct Anthropic API via Secrets Manager key
- **Option B**: Amazon Bedrock (Claude models available via Bedrock)
- **Timeout**: 30s for vision, 15s for text
- **Retry**: Exponential backoff with 3 retries
- **Fallback**: Template-based responses if model is unavailable

### Alert Pipeline: SQS + Lambda

```
Severity threshold met
  → Publish to SQS alert queue
    → Lambda function processes message
      → Send to Slack webhook
      → Send to SNS topic (email/SMS backup)
      → Update alert record in RDS
```

- **Dead letter queue**: Failed alerts after 3 retries go to DLQ for manual review
- **CloudWatch alarm**: Alert if DLQ depth > 0

### Vector Search: OpenSearch

- **Purpose**: Replace pHash-only similarity with embedding-based nearest-neighbor search
- **Index**: Image embeddings from a CLIP or similar model
- **kNN plugin**: Approximate nearest neighbor for fast similarity queries
- **Capacity**: Single-node for MVP, multi-node for scale

### Session/Cache: ElastiCache Redis

- **Chat session history**: TTL 24 hours
- **Rate limiting counters**: Per-IP request tracking
- **Triage cache**: Avoid re-processing identical images

### Observability

| Concern | Service | Configuration |
|---------|---------|---------------|
| Logs | CloudWatch Logs | Structured JSON logs from ECS tasks |
| Metrics | CloudWatch Metrics | Custom metrics: triage latency, severity distribution, guardrail triggers |
| Traces | X-Ray | End-to-end request tracing through API → services → external calls |
| Alerts | CloudWatch Alarms → SNS | API error rate >5%, P95 latency >4s, model failure spikes |
| Dashboards | CloudWatch Dashboards | Ops dashboard with key metrics |
| Audit | CloudTrail + RDS audit log | API access audit, admin query audit |

### Security

| Control | Implementation |
|---------|----------------|
| Authentication | Amazon Cognito user pools with JWT tokens |
| Authorization | RBAC: public (chat), volunteer (view), admin (analytics, status updates) |
| Network | Private subnets for ECS/RDS, security groups, NACLs |
| Secrets | Secrets Manager with automatic rotation |
| Encryption | TLS 1.3 in transit, AES-256 at rest (KMS-managed) |
| WAF | OWASP top-10 rule set, rate limiting, bot control |
| Image access | Pre-signed S3 URLs (short TTL), no public bucket access |

---

## CI/CD Pipeline

```
GitHub Push → CodePipeline
  → CodeBuild: lint, unit tests, guardrail regression tests
    → ECR: build + push Docker image
      → CodeDeploy: rolling update to ECS Fargate
        → Post-deploy: API contract tests against staging
          → Manual approval gate for production
```

### Environments

| Environment | Purpose | Infrastructure |
|-------------|---------|---------------|
| `dev` | Rapid iteration | Single-AZ, minimal capacity |
| `staging` | UAT, pre-prod validation | Multi-AZ, production-like config |
| `prod` | Live traffic | Multi-AZ, auto-scaling, full observability |

---

## Cost Estimate (Monthly, MVP Scale)

| Service | Configuration | Est. Cost |
|---------|--------------|-----------|
| ECS Fargate | 2 tasks, 0.5 vCPU, 1GB RAM | ~$30 |
| RDS MySQL | db.t3.micro, Multi-AZ | ~$30 |
| S3 | 10GB storage + requests | ~$2 |
| CloudFront | 10GB transfer | ~$2 |
| ElastiCache | cache.t3.micro | ~$15 |
| ALB | Standard | ~$20 |
| Anthropic API | ~1000 requests/month | ~$20-50 |
| CloudWatch | Standard | ~$10 |
| Secrets Manager | 5 secrets | ~$3 |
| **Total** | | **~$130-160/month** |

---

## Scalability Path

1. **Phase 1 MVP**: 2 ECS tasks, single RDS instance, basic CloudWatch
2. **Growth**: Auto-scaling to 10 tasks, RDS read replica, OpenSearch cluster
3. **Phase 2**: Multi-region (if needed for Hindi/regional deployment), Bedrock integration
