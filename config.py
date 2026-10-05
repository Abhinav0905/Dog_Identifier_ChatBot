import os
from pathlib import Path
from dotenv import dotenv_values, load_dotenv

BASE_DIR = Path(__file__).parent

ENV_FILE = Path(os.getenv("GAIA_ENV_FILE", str(BASE_DIR / ".env")))
ENV_FILE_VALUES = dotenv_values(ENV_FILE)
load_dotenv(ENV_FILE)


def _env_path(name: str, default: Path) -> Path:
    value = (os.getenv(name) or "").strip()
    return Path(value) if value else default


def _env_file_first(name: str, default: str = "") -> str:
    # Preserve the project's explicit configuration over an unrelated inherited
    # shell value. Isolated validation opts out with GAIA_ENV_FILE=/dev/null.
    file_value = str(ENV_FILE_VALUES.get(name) or "").strip()
    if file_value:
        return file_value
    env_value = (os.getenv(name) or "").strip()
    return env_value if env_value else default


def _env_bool(name: str, default: bool = False) -> bool:
    value = _env_file_first(name, str(default)).strip().lower()
    return value in {"1", "true", "yes", "on"}


STORAGE_DIR = _env_path("STORAGE_DIR", BASE_DIR / "storage")
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = _env_path("DB_PATH", BASE_DIR / "dharamsala.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

MODEL_PROVIDER = os.getenv("MODEL_PROVIDER", "openai").lower()
if MODEL_PROVIDER == "claude":
    raise ValueError("MODEL_PROVIDER 'claude' is not supported.")
OPENAI_API_KEY = _env_file_first("OPENAI_API_KEY", "")
OPENAI_MODEL = _env_file_first("OPENAI_MODEL", "gpt-4o")
OPENAI_VISION_MODEL = _env_file_first("OPENAI_VISION_MODEL", OPENAI_MODEL)
OPENAI_CHAT_MODEL = _env_file_first("OPENAI_CHAT_MODEL", "gpt-5.4")
OPENAI_ADMIN_MODEL = _env_file_first("OPENAI_ADMIN_MODEL", OPENAI_MODEL)
OPENAI_WEB_SEARCH_MODEL = _env_file_first("OPENAI_WEB_SEARCH_MODEL", "gpt-5.4-mini")
OPENAI_GEOGRAPHY_MODEL = _env_file_first("OPENAI_GEOGRAPHY_MODEL", "gpt-5.4-mini")
OPENAI_QUERY_ROUTER_MODEL = _env_file_first(
    "OPENAI_QUERY_ROUTER_MODEL",
    OPENAI_GEOGRAPHY_MODEL,
)
OPENAI_ROUTING_TIMEOUT_SECONDS = max(
    5.0,
    min(30.0, float(os.getenv("OPENAI_ROUTING_TIMEOUT_SECONDS", "12"))),
)
OPENAI_VISION_TIMEOUT_SECONDS = max(
    15.0,
    min(90.0, float(os.getenv("OPENAI_VISION_TIMEOUT_SECONDS", "45"))),
)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
ADMIN_PASSWORD = _env_file_first("ADMIN_PASSWORD", "")

# Twilio WhatsApp integration
TWILIO_ACCOUNT_SID = _env_file_first("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = _env_file_first("TWILIO_AUTH_TOKEN", "")
TWILIO_VALIDATE_SIGNATURES = _env_bool("TWILIO_VALIDATE_SIGNATURES", True)
TWILIO_WEBHOOK_BASE_URL = _env_file_first("TWILIO_WEBHOOK_BASE_URL", "").rstrip("/")
WHATSAPP_DEMO_LOCATION_FALLBACK = _env_bool("WHATSAPP_DEMO_LOCATION_FALLBACK", False)
WHATSAPP_DEMO_LAT = float(os.getenv("WHATSAPP_DEMO_LAT", "32.2196"))
WHATSAPP_DEMO_LNG = float(os.getenv("WHATSAPP_DEMO_LNG", "76.3234"))

# Feature flags
# Deprecated compatibility setting: assessment can run without GPS; reporting
# still requires usable location and a confirmed service-area match.
STRICT_LOCATION_GATE = _env_bool("STRICT_LOCATION_GATE", True)
INDIA_ONLY_SCOPE_ENABLED = _env_bool("INDIA_ONLY_SCOPE_ENABLED", True)
DOG_WEB_SEARCH_ENABLED = _env_bool("DOG_WEB_SEARCH_ENABLED", True)
DOG_WEB_SEARCH_MAX_RESULTS = int(os.getenv("DOG_WEB_SEARCH_MAX_RESULTS", "5"))
DOG_WEB_SEARCH_MAX_TOOL_CALLS = max(
    3,
    min(12, int(os.getenv("DOG_WEB_SEARCH_MAX_TOOL_CALLS", "8"))),
)
DOG_WEB_SEARCH_TOTAL_TIMEOUT_SECONDS = max(
    20.0,
    min(90.0, float(os.getenv("DOG_WEB_SEARCH_TOTAL_TIMEOUT_SECONDS", "60"))),
)
DOG_WEB_SEARCH_OPENAI_TIMEOUT_SECONDS = max(
    10.0,
    min(
        DOG_WEB_SEARCH_TOTAL_TIMEOUT_SECONDS,
        float(os.getenv("DOG_WEB_SEARCH_OPENAI_TIMEOUT_SECONDS", "35")),
    ),
)
DOG_WEB_PAGE_VERIFY_TIMEOUT_SECONDS = max(
    2.0,
    min(15.0, float(os.getenv("DOG_WEB_PAGE_VERIFY_TIMEOUT_SECONDS", "6"))),
)
DOG_WEB_PAGE_VERIFY_MAX_BYTES = max(
    100_000,
    min(2_000_000, int(os.getenv("DOG_WEB_PAGE_VERIFY_MAX_BYTES", "1000000"))),
)
DOG_WEB_PDF_VERIFY_MAX_BYTES = max(
    1_000_000,
    min(8 * 1024 * 1024, int(os.getenv("DOG_WEB_PDF_VERIFY_MAX_BYTES", "8388608"))),
)
DOG_WEB_PDF_VERIFY_MAX_PAGES = max(
    1, min(100, int(os.getenv("DOG_WEB_PDF_VERIFY_MAX_PAGES", "60"))),
)
DOG_WEB_PDF_VERIFY_MAX_CHARS = max(
    10_000, min(250_000, int(os.getenv("DOG_WEB_PDF_VERIFY_MAX_CHARS", "150000"))),
)
PLACE_GEOCODING_ENABLED = _env_bool("PLACE_GEOCODING_ENABLED", True)
LOCATIONIQ_API_KEY = _env_file_first("LOCATIONIQ_API_KEY", "")
PLACE_GEOCODER_PROVIDER = _env_file_first(
    "PLACE_GEOCODER_PROVIDER",
    "locationiq" if LOCATIONIQ_API_KEY else "nominatim",
).lower()
PLACE_GEOCODER_URL = _env_file_first(
    "PLACE_GEOCODER_URL",
    "https://nominatim.openstreetmap.org/search",
)
LOCATIONIQ_SEARCH_URL = _env_file_first(
    "LOCATIONIQ_SEARCH_URL",
    "https://us1.locationiq.com/v1/search",
)
LOCATIONIQ_AUTOCOMPLETE_URL = _env_file_first(
    "LOCATIONIQ_AUTOCOMPLETE_URL",
    "https://api.locationiq.com/v1/autocomplete",
)
PLACE_GEOCODER_USER_AGENT = _env_file_first(
    "PLACE_GEOCODER_USER_AGENT",
    "AskDorjee/1.0 (https://dharamsalaanimalrescue.org/)",
)
PLACE_GEOCODER_TIMEOUT_SECONDS = float(os.getenv("PLACE_GEOCODER_TIMEOUT_SECONDS", "10"))
PLACE_GEOCODER_CACHE_DAYS = int(os.getenv("PLACE_GEOCODER_CACHE_DAYS", "30"))
# LocationIQ's free plan permits request-response caching for at most 48 hours.
LOCATIONIQ_CACHE_DAYS = min(
    2,
    max(1, int(os.getenv("LOCATIONIQ_CACHE_DAYS", "2"))),
)
PLACE_FUZZY_GEOCODING_ENABLED = _env_bool("PLACE_FUZZY_GEOCODING_ENABLED", True)
PLACE_FUZZY_GEOCODER_URL = _env_file_first(
    "PLACE_FUZZY_GEOCODER_URL",
    "https://photon.komoot.io/api/",
)
PLACE_FUZZY_MIN_SCORE = float(os.getenv("PLACE_FUZZY_MIN_SCORE", "0.84"))
PLACE_LOCALITY_HINT_MAX_KM = float(os.getenv("PLACE_LOCALITY_HINT_MAX_KM", "120"))
INDIA_POSTAL_LOOKUP_ENABLED = _env_bool("INDIA_POSTAL_LOOKUP_ENABLED", True)
INDIA_POSTAL_LOOKUP_URL = _env_file_first(
    "INDIA_POSTAL_LOOKUP_URL",
    "https://api.postalpincode.in/postoffice/{place}",
)
INDIA_POSTAL_LOOKUP_TIMEOUT_SECONDS = float(
    os.getenv("INDIA_POSTAL_LOOKUP_TIMEOUT_SECONDS", "8")
)
CONVERSATION_GUEST_TTL_HOURS = max(
    1,
    int(os.getenv("CONVERSATION_GUEST_TTL_HOURS", "24")),
)
CASE_LOCATION_TTL_MINUTES = max(
    1,
    int(
        os.getenv(
            "CASE_LOCATION_TTL_MINUTES",
            str(CONVERSATION_GUEST_TTL_HOURS * 60),
        )
    ),
)
CONVERSATION_HISTORY_LIMIT = max(
    10,
    min(200, int(os.getenv("CONVERSATION_HISTORY_LIMIT", "100"))),
)
CONVERSATION_UI_HISTORY_LIMIT = max(
    2,
    min(50, int(os.getenv("CONVERSATION_UI_HISTORY_LIMIT", "10"))),
)
CONVERSATION_COOKIE_NAME = os.getenv(
    "CONVERSATION_COOKIE_NAME",
    "askdorjee_guest",
).strip() or "askdorjee_guest"
CONVERSATION_COOKIE_SECURE = _env_bool("CONVERSATION_COOKIE_SECURE", True)
SERVICE_CITY_MAX_DISTANCE_KM = max(
    1.0,
    float(os.getenv("SERVICE_CITY_MAX_DISTANCE_KM", "60")),
)
DOG_WEB_SEARCH_CACHE_ENABLED = _env_bool("DOG_WEB_SEARCH_CACHE_ENABLED", True)
DOG_WEB_SEARCH_CACHE_HOURS = max(
    1,
    int(os.getenv("DOG_WEB_SEARCH_CACHE_HOURS", "24")),
)
DOG_WEB_SEARCH_STALE_CACHE_HOURS = max(
    DOG_WEB_SEARCH_CACHE_HOURS,
    int(os.getenv("DOG_WEB_SEARCH_STALE_CACHE_HOURS", "168")),
)
DHARAMSALA_REGION_RADIUS_KM = float(os.getenv("DHARAMSALA_REGION_RADIUS_KM", "1000"))
DHARAMSALA_SERVICE_POINT_RADIUS_KM = float(os.getenv("DHARAMSALA_SERVICE_POINT_RADIUS_KM", "3"))

# RAG / Pinecone configuration
RAG_VECTOR_BACKEND = os.getenv("RAG_VECTOR_BACKEND", "chroma").lower()
RAG_SQLITE_EMBEDDINGS = os.getenv("RAG_SQLITE_EMBEDDINGS", "false").lower() == "true"
RAG_EMBEDDING_MODEL = os.getenv("RAG_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
RAG_DENSE_DIMENSION = int(os.getenv("RAG_DENSE_DIMENSION", "384"))
RAG_HYBRID_ALPHA = float(os.getenv("RAG_HYBRID_ALPHA", "0.65"))
RAG_SPARSE_DIMENSION = int(os.getenv("RAG_SPARSE_DIMENSION", "262144"))

CHROMA_PERSIST_DIR = _env_path("CHROMA_PERSIST_DIR", BASE_DIR / "chroma_db")
CHROMA_COLLECTION_NAME = os.getenv("CHROMA_COLLECTION_NAME", "dar-rag")
CHROMA_HNSW_SPACE = os.getenv("CHROMA_HNSW_SPACE", "cosine")
CHROMA_HNSW_CONSTRUCTION_EF = int(os.getenv("CHROMA_HNSW_CONSTRUCTION_EF", "200"))
CHROMA_HNSW_SEARCH_EF = int(os.getenv("CHROMA_HNSW_SEARCH_EF", "100"))
CHROMA_HNSW_M = int(os.getenv("CHROMA_HNSW_M", "16"))

PINECONE_API_KEY = _env_file_first("PINECONE_API_KEY", "")
PINECONE_INDEX_NAME = os.getenv("PINECONE_INDEX_NAME", "dar-rag-hybrid")
PINECONE_NAMESPACE = os.getenv("PINECONE_NAMESPACE", "dharamsala-animal-rescue")
PINECONE_CLOUD = os.getenv("PINECONE_CLOUD", "aws")
PINECONE_REGION = os.getenv("PINECONE_REGION", "us-east-1")

DAR_SCRAPE_BASE_URL = os.getenv("DAR_SCRAPE_BASE_URL", "https://dharamsalaanimalrescue.org/")
DAR_SCRAPE_MAX_PAGES = int(os.getenv("DAR_SCRAPE_MAX_PAGES", "80"))
DAR_CONTACT_URL = os.getenv("DAR_CONTACT_URL", "https://dharamsalaanimalrescue.org/contact/")

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

# Severity thresholds
DAR_PHONE_NUMBER = _env_file_first("DAR_PHONE_NUMBER", "")

ESCALATION_SEVERITY_THRESHOLD = 7  # 1-10 scale, >=7 triggers alert
SIMILARITY_PHASH_THRESHOLD = 10     # Hamming distance, <=10 is "similar"
SIMILARITY_EMBEDDING_THRESHOLD = 0.85

MAX_IMAGE_SIZE_MB = int(os.getenv("MAX_IMAGE_SIZE_MB", "100"))
MAX_CHAT_MESSAGE_CHARS = int(os.getenv("MAX_CHAT_MESSAGE_CHARS", "6000"))
PUBLIC_REQUESTS_PER_MINUTE = max(1, int(os.getenv("PUBLIC_REQUESTS_PER_MINUTE", "30")))
CHAT_TURN_TIMEOUT_SECONDS = max(20.0, min(110.0, float(os.getenv("CHAT_TURN_TIMEOUT_SECONDS", "90"))))
WHATSAPP_LOCATION_TTL_MINUTES = max(1, int(os.getenv("WHATSAPP_LOCATION_TTL_MINUTES", "60")))
VISION_IMAGE_MAX_DIMENSION = int(os.getenv("VISION_IMAGE_MAX_DIMENSION", "2048"))
VISION_IMAGE_JPEG_QUALITY = int(os.getenv("VISION_IMAGE_JPEG_QUALITY", "88"))
ALLOWED_IMAGE_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/heic",
    "image/heif",
}
