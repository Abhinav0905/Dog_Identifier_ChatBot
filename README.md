# Dharamsala Animal Rescue Chatbot

## Features

- **Image triage**: Upload a photo and get an AI-powered distress severity assessment (1–10 scale)
- **Community-first guidance**: Starts with local questions about feeders, owners, and ongoing NGO sterilization/vaccination work
- **Text chat**: Ask rescue questions, get guidance on dog bites, incident reporting, and more
- **Google Maps quick links**: Open nearby vets or animal-help searches after sharing location
- **Duplicate detection**: Prevents redundant reports using perceptual image hashing
- **Location tracking**: Extracts GPS from image EXIF data or browser geolocation
- **Automated alerts**: High-severity cases (score ≥ 7) trigger Slack/webhook notifications
- **Admin analytics**: Query incident data using natural language

## Prerequisites

- Python 3.11+
- An [OpenAI API key](https://platform.openai.com/api-keys)

## Setup

**1. Clone and enter the project directory:**

```bash
git clone <repo-url>
cd gaia-chatbot
```

**2. Create and activate a virtual environment:**

```bash
python3 -m venv .venv
source .venv/bin/activate        # macOS/Linux
# .venv\Scripts\activate         # Windows
```

**3. Install dependencies:**

```bash
pip install -r requirements.txt
```

**4. Configure environment variables:**

```bash
cp .env.example .env
```

Edit `.env` and set the following:

```env
# Required
OPENAI_API_KEY=sk-your-openai-key-here

# Optional model overrides
OPENAI_MODEL=gpt-5.4-mini
OPENAI_VISION_MODEL=
OPENAI_CHAT_MODEL=
OPENAI_ADMIN_MODEL=

# Optional — alerts log to console if not set
SLACK_WEBHOOK_URL=
ALERT_WEBHOOK_URL=

# Admin dashboard access
ADMIN_PASSWORD=changeme

# Server config (defaults shown)
HOST=0.0.0.0
PORT=8000

# Optional persistent paths (helpful for Docker/EC2)
DB_PATH=
STORAGE_DIR=
```

## Running the App

**1. Ingest RAG knowledge documents (required before first run):**

```bash
python3 scripts/ingest_docs.py
```

This populates the knowledge base used by the chat assistant. Re-run whenever files in `rag_docs/` are updated.

**2. Start the server:**

```bash
python app.py
```

The app will be available at:

| URL | Description |
|-----|-------------|
| http://localhost:8000 | Public chat UI |
| http://localhost:8000/admin.html | Admin dashboard |
| http://localhost:8000/health | Health check |
| http://localhost:8000/docs | Swagger API docs |

## Running Tests

```bash
# Unit tests
python test_unit.py

# System / integration tests
python test_system.py
```

## EC2 Deployment

For a shareable open link on EC2, use the Docker-based flow in [`EC2_DEPLOYMENT.md`](EC2_DEPLOYMENT.md).
At a minimum, the instance needs:

- A **public IPv4 or Elastic IP**
- A **security group allowing inbound HTTP (80)** from `0.0.0.0/0`
- Docker installed
- A populated `.env` with your OpenAI key and admin password

Once deployed, the app can be shared at `http://<ec2-public-ip>/` or a domain pointed at that IP.

## Project Structure

```
gaia-chatbot/
├── app.py                   # FastAPI application and route handlers
├── config.py                # Configuration and constants
├── database.py              # SQLite database layer
├── models.py                # Pydantic request/response models
├── requirements.txt
├── .env.example
│
├── services/
│   ├── triage.py            # Vision triage and chat response generation
│   ├── rag.py               # RAG retrieval (BM25 / semantic)
│   ├── ai_client.py         # Unified Anthropic/OpenAI client
│   ├── guardrails.py        # Input validation and safety filters
│   ├── similarity.py        # Duplicate/near-duplicate detection
│   ├── location.py          # EXIF GPS extraction and jurisdiction check
│   ├── alerts.py            # Slack/webhook alert dispatching
│   └── admin_analytics.py   # Natural language to SQL analytics
│
├── rag_docs/                # Markdown knowledge documents (DAR website content)
├── scripts/
│   └── ingest_docs.py       # Chunk, embed, and store rag_docs into SQLite
│
├── static/
│   ├── index.html           # Public chat UI
│   ├── admin.html           # Admin dashboard
│   ├── app.js
│   └── style.css
│
├── test_unit.py
└── test_system.py
```

## API Overview

### Public Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/triage/image` | Upload image for distress assessment |
| `POST` | `/v1/chat/query` | Send a text rescue question |
| `POST` | `/v1/location/update` | Update location for an incident |
| `GET`  | `/v1/incidents/{id}` | Retrieve incident details |

### Admin Endpoints (require `admin_password`)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/admin/query` | Natural language analytics query |
| `GET`  | `/v1/admin/incidents` | List and filter incidents |
| `GET`  | `/v1/admin/alerts` | List alerts |
| `POST` | `/v1/admin/incidents/{id}/status` | Update incident status |

## Configuration Reference

Key settings in `config.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| `ESCALATION_SEVERITY_THRESHOLD` | `7` | Score (1–10) at or above which alerts fire |
| `SIMILARITY_PHASH_THRESHOLD` | `10` | Hamming distance for near-duplicate images |
| `MAX_IMAGE_SIZE_MB` | `10` | Maximum upload size |
| `ALLOWED_IMAGE_TYPES` | JPEG, PNG, WebP, GIF | Accepted MIME types |

## Further Documentation

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — Component breakdown and local demo architecture
- [`AWS_PRODUCTION_ARCHITECTURE.md`](AWS_PRODUCTION_ARCHITECTURE.md) — Production deployment on AWS (ECS, RDS, S3)
- [`DEMO_TO_PRODUCTION_GUIDE.md`](DEMO_TO_PRODUCTION_GUIDE.md) — Step-by-step migration from local SQLite to AWS
- [`EC2_DEPLOYMENT.md`](EC2_DEPLOYMENT.md) — Fast path for a public EC2 deployment and shareable link
