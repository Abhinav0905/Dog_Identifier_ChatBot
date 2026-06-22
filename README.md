# Dharamsala Animal Rescue Chatbot

## Features

- **Image triage**: Upload a photo and get an AI-powered distress severity assessment (1–10 scale)
- **Smartphone photo support**: Accepts HEIC/HEIF and high-resolution uploads, then creates a vision-safe JPEG copy for assessment
- **Community-first guidance**: Starts with local questions about feeders, owners, and ongoing NGO sterilization/vaccination work
- **Text chat**: Ask rescue questions, get guidance on dog bites, incident reporting, and more
- **Google Maps quick links**: Open nearby animal rescue/NGO help searches after sharing location
- **Duplicate detection**: Prevents redundant reports using perceptual image hashing
- **Location tracking**: Extracts GPS from image EXIF data or browser geolocation
- **Strict jurisdiction gate**: Image reports are assessed only when EXIF GPS or shared browser location verifies the case is within the Dharamsala service area
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
OPENAI_MODEL=gpt-4o
OPENAI_VISION_MODEL=
OPENAI_CHAT_MODEL=
OPENAI_ADMIN_MODEL=

# Optional — alerts log to console if not set
SLACK_WEBHOOK_URL=
ALERT_WEBHOOK_URL=

# Admin dashboard access
ADMIN_PASSWORD=<set-a-strong-password>

# Public rescue contact shown in guidance responses
DAR_PHONE_NUMBER=<public-contact-number>

# Feature flags
STRICT_LOCATION_GATE=true
# Buffer around Deb's service-area route checkpoints, in km.
DHARAMSALA_SERVICE_POINT_RADIUS_KM=3
# Legacy fallback radius setting; route polygon/checkpoints are the active gate.
DHARAMSALA_REGION_RADIUS_KM=1000

# Server config (defaults shown)
HOST=0.0.0.0
PORT=8000

# Optional persistent paths (helpful for Docker/EC2)
DB_PATH=
STORAGE_DIR=

# Optional local Chroma RAG
RAG_VECTOR_BACKEND=chroma
CHROMA_PERSIST_DIR=
CHROMA_COLLECTION_NAME=dar-rag
CHROMA_HNSW_SPACE=cosine
CHROMA_HNSW_CONSTRUCTION_EF=200
CHROMA_HNSW_SEARCH_EF=100
CHROMA_HNSW_M=16
RAG_EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2

# Optional Pinecone hybrid RAG
PINECONE_API_KEY=
PINECONE_INDEX_NAME=dar-rag-hybrid
PINECONE_NAMESPACE=dharamsala-animal-rescue
RAG_DENSE_DIMENSION=384
RAG_HYBRID_ALPHA=0.65
```

## Running the App

**1. Ingest RAG knowledge documents (required before first run):**

```bash
python3 scripts/ingest_docs.py
```

This populates the knowledge base used by the chat assistant. Re-run whenever files in `rag_docs/` are updated.

To refresh the core DAR project pages and their relevant child pages:

```bash
python3 scripts/scrape_dar_site.py --scope projects --delay 10
python3 scripts/ingest_docs.py --chroma --clear-chroma
```

The project-scoped crawl follows links inside the page content for three levels,
skips donation/newsletter noise, and writes its coverage report to
`reports/projects_scrape_manifest.json`. Use the default site scope only when a
broader crawl is intentionally needed.

To ingest additional local PDFs into the same Chroma collection:

```bash
python3 scripts/ingest_docs.py --chroma --clear-chroma \
  --doc "/path/to/file.pdf"
```

For image-only PDFs on macOS, install the optional OCR helpers and add `--ocr-pdfs`:

```bash
pip install PyMuPDF ocrmac
```

This uses Apple Vision locally and lets scanned PDFs be chunked and embedded too.

The default vector path uses local Chroma with sentence-transformer dense embeddings persisted under `chroma_db/`. If Chroma is unavailable or empty, the app falls back to the local SQLite/BM25 retrieval path. Pinecone remains available with `RAG_VECTOR_BACKEND=pinecone` and `python3 scripts/ingest_docs.py --pinecone --clear-pinecone-namespace`.

`STRICT_LOCATION_GATE=true` requires either photo EXIF GPS or shared browser location inside the Dharamsala service area before image triage runs.

The active service-area gate is Deb's route-map loop plus a small buffer around
the named checkpoints in `services/location.py`: DAR/Rakkar, Kharota, Khanyara,
Gamru Village Road, Chakban Gharoh, Gaggal, Chakban Banwala, Yol, and Chamunda
Devi Temple. Tune the checkpoint buffer with
`DHARAMSALA_SERVICE_POINT_RADIUS_KM`; the default is `3`.

For image reports, the server emits a `location_gate_decision` log line showing
each available GPS source, detected coordinates, nearest service-area checkpoint,
selected source, and final decision.

For the EC2 Docker deployment, follow location-gate decisions with:

```bash
docker logs -f gaia-chatbot 2>&1 | grep location_gate_decision
```

## Twilio WhatsApp Sandbox

The WhatsApp webhook is:

```text
http://<ec2-public-ip>/v1/integrations/twilio/whatsapp
```

In Twilio Console, open **Messaging > Try it out > Send a WhatsApp message >
Sandbox settings**. Set **When a message comes in** to the webhook URL above,
choose `POST`, and save it. Twilio needs a public URL; an EC2 instance ID such
as `i-...` is not a valid webhook address.

Text messages use the normal RAG chat workflow. For a photo report, first share
a WhatsApp location pin and then send the photo. The app stores the latest
location for that pseudonymous WhatsApp sender because WhatsApp may remove
photo EXIF metadata.

Set these environment variables on the server:

```env
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_VALIDATE_SIGNATURES=false
TWILIO_WEBHOOK_BASE_URL=http://<ec2-public-ip>
WHATSAPP_DEMO_LOCATION_FALLBACK=false
WHATSAPP_DEMO_LAT=32.2196
WHATSAPP_DEMO_LNG=76.3234
```

Once the public webhook URL and credentials are stable, set
`TWILIO_VALIDATE_SIGNATURES=true`.

For demos, `WHATSAPP_DEMO_LOCATION_FALLBACK=true` lets WhatsApp image reports
proceed even when WhatsApp strips photo EXIF GPS metadata. Disable it before
strict production intake.

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
- A populated `.env` with your OpenAI key, admin password, and public rescue contact number

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
| `MAX_IMAGE_SIZE_MB` | `100` | Maximum original upload size |
| `VISION_IMAGE_MAX_DIMENSION` | `2048` | Longest side sent to the vision model |
| `ALLOWED_IMAGE_TYPES` | JPEG, PNG, WebP, GIF, HEIC, HEIF | Accepted MIME types |
| `WHATSAPP_DEMO_LOCATION_FALLBACK` | `false` | Demo-only fallback location for WhatsApp photos with stripped EXIF |

## Further Documentation

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — Component breakdown and local demo architecture
- [`AWS_PRODUCTION_ARCHITECTURE.md`](AWS_PRODUCTION_ARCHITECTURE.md) — Production deployment on AWS (ECS, RDS, S3)
- [`DEMO_TO_PRODUCTION_GUIDE.md`](DEMO_TO_PRODUCTION_GUIDE.md) — Step-by-step migration from local SQLite to AWS
- [`EC2_DEPLOYMENT.md`](EC2_DEPLOYMENT.md) — Fast path for a public EC2 deployment and shareable link
