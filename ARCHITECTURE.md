# Dharamsala Animal Rescue Chatbot - Local Demo Architecture

## Overview

This document describes the architecture of the local prototype for the Dharamsala Animal
Rescue Chatbot. The prototype implements all Phase 1 functional requirements using lightweight
local substitutes for production services, enabling full end-to-end workflow validation before
cloud deployment.

---

## System Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                        Browser (User)                        │
│  ┌──────────────────────┐   ┌────────────────────────────┐  │
│  │  Chat Widget (UI)    │   │  Admin Dashboard           │  │
│  │  index.html + app.js │   │  admin.html                │  │
│  └──────────┬───────────┘   └─────────────┬──────────────┘  │
└─────────────┼─────────────────────────────┼──────────────────┘
              │ HTTP (localhost:8000)        │
              ▼                             ▼
┌─────────────────────────────────────────────────────────────┐
│                    FastAPI Application (app.py)               │
│                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │ Public APIs   │  │ Admin APIs   │  │ Integration APIs │  │
│  │ /v1/triage/*  │  │ /v1/admin/*  │  │ /v1/integrations │  │
│  │ /v1/chat/*    │  │              │  │                  │  │
│  │ /v1/location  │  │              │  │                  │  │
│  │ /v1/incidents │  │              │  │                  │  │
│  └──────┬───────┘  └──────┬───────┘  └────────┬─────────┘  │
│         │                 │                    │             │
│  ┌──────▼─────────────────▼────────────────────▼─────────┐  │
│  │                  Service Layer                         │  │
│  │  ┌─────────────┐ ┌──────────────┐ ┌────────────────┐ │  │
│  │  │  Guardrails  │ │   Triage     │ │  Similarity    │ │  │
│  │  │  Service     │ │   Service    │ │  Detection     │ │  │
│  │  └─────────────┘ └──────┬───────┘ └────────────────┘ │  │
│  │  ┌─────────────┐ ┌──────▼───────┐ ┌────────────────┐ │  │
│  │  │  Location    │ │   Alerts     │ │  Admin         │ │  │
│  │  │  Service     │ │   Service    │ │  Analytics     │ │  │
│  │  └─────────────┘ └─────────────┘  └────────────────┘ │  │
│  └───────────────────────────────────────────────────────┘  │
└──────────────┬──────────────────┬────────────────────────────┘
               │                  │
    ┌──────────▼──────┐  ┌───────▼────────┐
    │  SQLite DB      │  │  Local Filesystem│
    │  dharmasala.db  │  │  storage/        │
    │                 │  │  (image blobs)   │
    └─────────────────┘  └────────────────-┘
               │
    ┌──────────▼──────────┐
    │  OpenAI Responses   │
    │  API (External)     │
    │  - Vision triage    │
    │  - Chat responses   │
    │  - NL-to-SQL        │
    └─────────────────────┘
```

---

## Component Details

### 1. UI Layer

| Component | File | Purpose |
|-----------|------|---------|
| Chat Widget | `static/index.html`, `static/app.js`, `static/style.css` | Public-facing chatbot UI with image upload, geolocation sharing, text chat |
| Admin Dashboard | `static/admin.html` | NL query interface, incident table, dashboard stats |

The UI communicates with the backend via REST API calls over localhost.

### 2. API Layer (app.py)

FastAPI application exposing all endpoints defined in the HLD:

**Public APIs:**
- `POST /v1/triage/image` - Image upload + distress assessment (UC-1)
- `POST /v1/chat/query` - Text rescue question (UC-2)
- `POST /v1/location/update` - Update incident location (UC-3 support)
- `GET /v1/incidents/{id}` - Retrieve incident details

**Admin APIs:**
- `POST /v1/admin/query` - NL analytics (UC-5)
- `GET /v1/admin/incidents` - List/filter incidents
- `GET /v1/admin/alerts` - List alerts
- `POST /v1/admin/incidents/{id}/status` - Update status

**Integration APIs:**
- `POST /v1/integrations/slack/alert` - Manual Slack alert trigger
- `POST /v1/integrations/events` - Generic event endpoint

### 3. Service Layer

| Service | File | Responsibility |
|---------|------|----------------|
| **Guardrails** | `services/guardrails.py` | Domain relevance filtering, harmful input detection, prompt injection protection, medical-certainty limitation |
| **Triage** | `services/triage.py` | OpenAI vision/chat integration for distress assessment, chat response generation, fallback responses when API is unavailable |
| **Similarity** | `services/similarity.py` | SHA-256 exact duplicate detection, perceptual hash (pHash) near-duplicate detection |
| **Location** | `services/location.py` | EXIF GPS extraction from images, coordinate precision truncation for privacy |
| **Alerts** | `services/alerts.py` | Console logging of alerts, optional Slack/webhook dispatch, alert record persistence |
| **Admin Analytics** | `services/admin_analytics.py` | NL-to-SQL via OpenAI, template-based fallback, read-only query execution with safety validation |

### 4. Data Layer

**SQLite Database (`dharmasala.db`):**
- `incidents` - Core incident records with triage results, location, similarity links
- `alerts` - Alert dispatch records with acknowledgment tracking
- `triage_events` - Model inference audit trail
- `admin_query_audit` - Admin NL query audit log
- `chat_history` - Conversation history per session

**Local Filesystem (`storage/`):**
- Image blobs stored as `{sha256}_{filename}`
- Serves as local substitute for S3/blob storage

### 5. External Dependencies

| Dependency | Purpose | Demo Substitute |
|------------|---------|-----------------|
| OpenAI Responses API | Vision triage, chat, NL-to-SQL | Falls back to template responses if API key not configured |
| Slack | Operational alerts | Console logging |
| MySQL/RDS | Relational data | SQLite |
| S3 | Blob storage | Local filesystem |
| Vector DB | Embedding similarity | pHash only (no embedding search) |

---

## End-to-End Request Flows

### Flow A: Image Triage (UC-1)

```
User uploads image → API validates file/size → Guardrails check context text
→ Save blob to storage/ → Compute SHA-256 + pHash → Extract EXIF GPS
→ Call OpenAI vision model for triage → Check duplicates/similarity
→ Create incident record → Evaluate severity threshold
→ (If urgent) Send alert → Build response → Return to user
```

### Flow B: Text Chat (UC-2)

```
User sends text → Guardrails check → Load chat history
→ Call OpenAI model for response → Sanitize response → Persist to history → Return
```

### Flow C: Admin NL Query (UC-5)

```
Admin enters question → Validate password → Send to OpenAI for SQL generation
→ Validate SQL safety (read-only, allowed tables) → Execute query
→ Summarize results → Log to audit table → Return
```

---

## Security Controls (Demo Level)

| Control | Implementation |
|---------|----------------|
| Admin auth | Simple password check (config-based) |
| SQL injection prevention | Read-only validation, keyword blocklist |
| Input guardrails | Regex-based domain/toxicity/injection filters |
| Response safety | Post-processing sanitization of AI responses |
| File validation | MIME type and size checks |
| Privacy | Location precision truncation, no PII storage |

---

## Running the Prototype

```bash
cd gaia_ai/chatbot
pip install -r requirements.txt
cp .env.example .env          # Edit with your OpenAI API key
python app.py                 # Starts on http://localhost:8000
```

- Chat UI: http://localhost:8000/
- Admin: http://localhost:8000/admin.html
- Health: http://localhost:8000/health
- API docs: http://localhost:8000/docs
