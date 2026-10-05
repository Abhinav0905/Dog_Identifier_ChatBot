# Dharamsala Animal Rescue Chatbot

Ask Dorjee provides humane community-dog education, practical safety guidance,
photo assessment and animal-care discovery across India. It can look for veterinary
hospitals, colleges, clinics, public services and welfare organisations according
to the user's need. A particular town is a test example, not a routing rule.

The current release covers the web application. The retired WhatsApp webhook is
not mounted and its background worker is not started. Location is optional for
general guidance and photo assessment; a precise, verified local location is
required before recording a Dharamsala rescue report. Notification delivery,
operator acknowledgement and rescue dispatch are separate facts.

See [WEB_RELEASE_CHECKLIST.md](WEB_RELEASE_CHECKLIST.md) and
[EC2_DEPLOYMENT.md](EC2_DEPLOYMENT.md) before deployment. Run the fifteen-scenario
live web review with `.venv/bin/python scripts/run_india_web_acceptance.py`;
controlled failures and previous attempts are recorded separately. This local
review does not certify the public deployment or a real rescue-team handoff.

For a prompt review or code walkthrough, start with
[Ask Dorjee prompt and request flow](docs/ASK_DORJEE_PROMPT_FLOW.md). All runtime
model instructions are centralized in `services/prompts.py` under `PromptCatalog`.

## Working Demo

[![Watch the working app demo](https://img.youtube.com/vi/_7xBCLXIK7U/hqdefault.jpg)](https://youtu.be/_7xBCLXIK7U)

Watch the working video of the app: https://youtu.be/_7xBCLXIK7U

## What This App Does

This app supports an animal rescue NGO by combining:

- **Vision analysis**: Reviews uploaded animal photos and estimates rescue urgency
- **Location-aware intake**: Extracts streets, localities, cities, districts, states, PIN codes, photo GPS, or browser location before enforcing India-only scope and Dharamsala routing
- **Rescue guidance**: Gives safe, community-first instructions for injured, sick, lost, or vulnerable animals
- **Incident tracking**: Stores report details, duplicate checks, severity scores, and alert status
- **NGO escalation support**: Can notify rescue staff when a case appears high severity

## Features

- **Image triage**: Upload a photo and get an AI-powered distress severity assessment (1–10 scale)
- **Smartphone photo support**: Accepts HEIC/HEIF and high-resolution uploads, then creates a vision-safe JPEG copy for assessment
- **Contextual humane guidance**: Addresses the current situation and audience; urgent care precedes community education
- **Text chat**: Ask rescue questions, get guidance on dog bites, incident reporting, and more
- **Google Maps quick links**: Open nearby animal rescue/NGO help searches after sharing location
- **Duplicate detection**: Prevents redundant reports using perceptual image hashing
- **Location tracking**: Extracts GPS from image EXIF data or browser geolocation
- **India-only case scope**: Explicit foreign cases are outside scope; missing GPS does not block general photo assessment
- **Model-directed text search**: Interprets the question and conversation, searches relevant animal-help providers and sources, and shows cited answers without the NGO-only directory cache
- **Truthful notification status**: Eligible local reports attempt configured alerts; failed delivery remains visible, retries are bounded, and receipt acknowledgement never promises dispatch
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
OPENAI_CHAT_MODEL=gpt-5.4
OPENAI_ADMIN_MODEL=

# Optional — unconfigured alerts are recorded as not_configured, not delivered
SLACK_WEBHOOK_URL=
ALERT_WEBHOOK_URL=

# Admin dashboard access
ADMIN_PASSWORD=<set-a-strong-password>

# Public rescue contact shown in guidance responses
DAR_PHONE_NUMBER=<public-contact-number>

# Feature flags
# Legacy setting; location is required for reporting, not general assessment
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

Photo assessment can run without GPS. Unknown or unclear photos retain their
uncertainty, and known urgent symptoms in the caption still receive immediate
guidance. An eligible location is required before recording a local rescue
incident. EXIF and conflicting named locations require clarification; browser
location cannot silently override photo GPS. Other Indian locations receive
need-led searches across veterinary and welfare providers. `STRICT_LOCATION_GATE`
is retained as a deprecated environment setting and no longer blocks assessment.
An alert is described as delivered only after an external channel accepts it;
acceptance does not mean a rescue team has agreed to dispatch.

The active service-area gate is Deb's route-map loop plus a small buffer around
the named checkpoints in `services/location.py`: DAR/Rakkar, Kharota, Khanyara,
Gamru Village Road, Chakban Gharoh, Gaggal, Chakban Banwala, Yol, and Chamunda
Devi Temple. Tune the checkpoint buffer with
`DHARAMSALA_SERVICE_POINT_RADIUS_KM`; the default is `3`.

For image reports, the server emits a `location_gate_decision` log line showing
each available GPS source, detected coordinates, nearest service-area checkpoint,
selected source, and final decision.

For text requests, a model selects **answer**, **search**, or **clarify** using the
current question and recent conversation. Search uses the configured
`OPENAI_WEB_SEARCH_MODEL` with the Responses web-search tool. It can research
veterinary colleges, hospitals, public services, private clinics, rescue groups,
and other relevant animal-welfare information. Sources are not restricted to NGO
websites; authoritative sources are preferred and current claims should be cited.
Citations are model-attributed evidence, not a guarantee of independently verified
phone numbers, availability, or service coverage.

Contact answers receive a separate evidence check against the fetched source text
and the requested institution. Unsupported numbers are withheld; an unavailable
source produces uncertainty. This applies equally to colleges, clinics, public
services, and rescue groups. The check adds page fetches and a model call to contact
lookups. GPT-5 search configurations use low reasoning effort and medium search
context to improve institution matching.

The text path does not read or write the city-level NGO answer cache or select a
previous NGO automatically. Original wording and corrections reach the search
model, with prior replies supplied as reference data, and source-linked answers
remain in conversation history. A request for
another institution triggers a new search. The model must distinguish a provider's
physical address from evidence that it serves the requested place; the app does
not replace that description with the requested city or add a Verified heading.

Explicit geographic text can be resolved for case context and India scope, but
unavailable geocoding does not block a named-institution search. Ordinary behavior
follow-ups do not go through location parsing. Current explicit places supersede
old case locations and browser hints. Photo-intake jurisdiction is separate and
retains the configured Dharamsala polygon and India checks.

Protected safety guidance handles actual bites, injuries and motorbike pursuits,
including recurring encounters with community dogs. A feared bite is not treated
as an actual bite. If the action model fails, the general model receives the
original conversation with optional web search; model/search failures retain
available immediate safety guidance and do not restore an old NGO directory.

Anonymous conversations use the existing server-issued ownership cookie and
SQLite history. A reload restores the owned messages; **New chat** starts a new
conversation. Each text reply logs its chosen action, routing source, whether a
web search actually completed, and result kind. Search itself retains configured
request timeouts and tool-call limits.

Guest conversation ownership provides reload-safe short-term memory without
putting a credential in JavaScript. It is not an account system and does not
provide cross-device history. If account login is required, use an identity
provider such as Amazon Cognito with Authorization Code + PKCE, keep tokens in
server-managed HttpOnly cookies, and bind each conversation to the verified
user identifier. Do not expose the conversation-history API on the basis of a
client-supplied session ID alone.

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
TWILIO_VALIDATE_SIGNATURES=true
TWILIO_WEBHOOK_BASE_URL=https://askdorjee.dharamsalaanimalrescue.org
WHATSAPP_DEMO_LOCATION_FALLBACK=false
WHATSAPP_DEMO_LAT=32.2196
WHATSAPP_DEMO_LNG=76.3234
```

Signature validation is fail-closed by default. Keep it enabled and set the
public HTTPS base URL exactly as configured in Twilio so proxy rewriting does
not invalidate legitimate signatures.

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

## Isolated local validation

The `feature/model-directed-search` work is for local review before deployment.
Prepare an isolated database and copy of the existing knowledge store:

```bash
.venv/bin/python scripts/run_local_validation.py --prepare-only
.venv/bin/python scripts/run_local_validation.py --state-dir /path/printed/by/prepare
```

Open **http://localhost:8001**. Reuse the printed state directory when restarting.
The runner copies knowledge only, keeps conversations/uploads separate, enables
local HTTP cookies, and disables outbound Slack, rescue webhooks and WhatsApp.
It does not edit `.env`. Preparation uses no model calls; live chat testing uses
the configured API providers. Do not run deployment scripts as part of this flow.

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
│   ├── query_router.py      # Structured text intent and address extraction
│   ├── place_resolver.py    # Address geocoding and India country verification
│   ├── region_scope.py      # India-only and Dharamsala routing decisions
│   ├── web_search.py        # Open text research and legacy photo NGO lookup
│   ├── search_evidence.py   # Contact-source and institution checks
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
| `INDIA_ONLY_SCOPE_ENABLED` | `true` | Reject explicitly verified text/photo cases outside India |
| `OPENAI_QUERY_ROUTER_MODEL` | `OPENAI_GEOGRAPHY_MODEL` | Structured text intent and address extraction model |
| `OPENAI_ROUTING_TIMEOUT_SECONDS` | `12` | Intent/location model deadline before deterministic fallback; SDK retries are disabled |
| `OPENAI_VISION_TIMEOUT_SECONDS` | `45` | Per-attempt image/chat model deadline; SDK retries are disabled and image triage retains its bounded application retry |
| `DOG_WEB_SEARCH_ENABLED` | `true` | Enable current research across veterinary and welfare resources |
| `DOG_WEB_SEARCH_MAX_TOOL_CALLS` | `8` | Maximum web-search tool calls within the research budget |
| `DOG_WEB_SEARCH_TOTAL_TIMEOUT_SECONDS` | `60` | Search deadline, also bounded by the shared turn deadline |
| `DOG_WEB_SEARCH_OPENAI_TIMEOUT_SECONDS` | `35` | Maximum duration of one OpenAI web-research call; SDK retries are disabled |
| `DOG_WEB_PAGE_VERIFY_TIMEOUT_SECONDS` | `6` | Per-read timeout for bounded official-site HTTPS verification |
| `DOG_WEB_PAGE_VERIFY_MAX_BYTES` | `1000000` | Maximum bytes accepted from one HTML source |
| `DOG_WEB_PDF_VERIFY_MAX_BYTES` | `8388608` | Maximum PDF download size; scanned PDFs without text require a separate OCR workflow |
| `DOG_WEB_PDF_VERIFY_MAX_PAGES` | `60` | Maximum PDF pages parsed within the fetch deadline |
| `DOG_WEB_PDF_VERIFY_MAX_CHARS` | `150000` | Maximum extracted PDF text retained |
| `DOG_WEB_SEARCH_CACHE_HOURS` | `24` | Legacy NGO-cache setting; model-directed search does not read this cache |
| `DOG_WEB_SEARCH_STALE_CACHE_HOURS` | `168` | Legacy NGO-cache setting; model-directed search does not read this cache |
| `PLACE_GEOCODING_ENABLED` | `true` | Resolve named text locations before routing |
| `PLACE_GEOCODER_PROVIDER` | LocationIQ when token exists, otherwise Nominatim | Primary named-place provider |
| `LOCATIONIQ_API_KEY` | empty | Server-side LocationIQ token; never expose it to the browser |
| `LOCATIONIQ_SEARCH_URL` | LocationIQ US v1 Search | LocationIQ forward-geocoding endpoint |
| `LOCATIONIQ_AUTOCOMPLETE_URL` | LocationIQ v1 Autocomplete | India-constrained spelling-candidate endpoint; candidates are reverified through Search |
| `LOCATIONIQ_CACHE_DAYS` | `2` | Conservative free-plan LocationIQ cache lifetime |
| `PLACE_GEOCODER_URL` | Nominatim search API | Named-place geocoder endpoint |
| `PLACE_GEOCODER_USER_AGENT` | AskDorjee identifier | Required descriptive geocoder User-Agent |
| `PLACE_GEOCODER_CACHE_DAYS` | `30` | Persistent named-place cache lifetime |
| `PLACE_FUZZY_GEOCODING_ENABLED` | `true` | Enable validated India-only fuzzy place fallback |
| `PLACE_FUZZY_GEOCODER_URL` | Photon API | Fuzzy locality geocoder endpoint |
| `PLACE_FUZZY_MIN_SCORE` | `0.84` | Minimum spelling/location token similarity |
| `PLACE_LOCALITY_HINT_MAX_KM` | `120` | Maximum browser-distance hint for ambiguous localities |
| `INDIA_POSTAL_LOOKUP_ENABLED` | `true` | Verify obscure bare Indian localities when map search is inconclusive |
| `INDIA_POSTAL_LOOKUP_URL` | Postal PIN lookup API | Exact post-office locality endpoint; `{place}` is URL encoded |
| `INDIA_POSTAL_LOOKUP_TIMEOUT_SECONDS` | `8` | Postal locality fallback timeout |
| `CONVERSATION_GUEST_TTL_HOURS` | `24` | Guest conversation inactivity lifetime |
| `CONVERSATION_HISTORY_LIMIT` | `100` | Maximum history rows loaded by the server; model paths use up to 20 recent messages within their input budgets |
| `CONVERSATION_UI_HISTORY_LIMIT` | `10` | Maximum messages restored into the browser |
| `CONVERSATION_COOKIE_NAME` | `askdorjee_guest` | HttpOnly guest-ownership cookie name |
| `CONVERSATION_COOKIE_SECURE` | `true` | Send the ownership cookie only over HTTPS |
| `CASE_LOCATION_TTL_MINUTES` | `1440` | Structured case-location lifetime for follow-ups |
| `SERVICE_CITY_MAX_DISTANCE_KM` | `60` | Maximum plausible distance between an explicitly named parent city and the incident |
| `MAX_CHAT_MESSAGE_CHARS` | `6000` | Maximum text request or photo caption length |
| `PUBLIC_REQUESTS_PER_MINUTE` | `30` | Shared SQLite request budget per client; signed WhatsApp requests use sender budgets |
| `CHAT_TURN_TIMEOUT_SECONDS` | `90` | Shared routing, lookup and response budget; notification delivery has separate transport timeouts |
| `WHATSAPP_LOCATION_TTL_MINUTES` | `60` | Lifetime of a saved WhatsApp location pin |

The focused acceptance review uses 15 scenarios and writes a JSON transcript:
`.venv/bin/python scripts/run_acceptance_review.py`. It uses project credentials
for live model/search calls, an isolated database and current local knowledge
documents. Notifications are disabled; explicitly labeled fault cases simulate
search and delivery failures. `--case C02` reruns one scenario and retains earlier
attempts. Automated checks do not assign stakeholder acceptance; the JSON includes
separate review fields for that assessment.

## Further Documentation

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — Component breakdown and local demo architecture
- [`AWS_PRODUCTION_ARCHITECTURE.md`](AWS_PRODUCTION_ARCHITECTURE.md) — Production deployment on AWS (ECS, RDS, S3)
- [`DEMO_TO_PRODUCTION_GUIDE.md`](DEMO_TO_PRODUCTION_GUIDE.md) — Step-by-step migration from local SQLite to AWS
- [`EC2_DEPLOYMENT.md`](EC2_DEPLOYMENT.md) — Fast path for a public EC2 deployment and shareable link
