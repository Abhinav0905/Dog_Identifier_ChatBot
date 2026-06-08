import os
from pathlib import Path
from dotenv import dotenv_values, load_dotenv

BASE_DIR = Path(__file__).parent

ENV_FILE_VALUES = dotenv_values(BASE_DIR / ".env")
load_dotenv(BASE_DIR / ".env")


def _env_path(name: str, default: Path) -> Path:
    value = (os.getenv(name) or "").strip()
    return Path(value) if value else default


def _env_file_first(name: str, default: str = "") -> str:
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
OPENAI_CHAT_MODEL = _env_file_first("OPENAI_CHAT_MODEL", OPENAI_MODEL)
OPENAI_ADMIN_MODEL = _env_file_first("OPENAI_ADMIN_MODEL", OPENAI_MODEL)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
ADMIN_PASSWORD = _env_file_first("ADMIN_PASSWORD", "")

# Twilio WhatsApp integration
TWILIO_ACCOUNT_SID = _env_file_first("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = _env_file_first("TWILIO_AUTH_TOKEN", "")
TWILIO_VALIDATE_SIGNATURES = _env_bool("TWILIO_VALIDATE_SIGNATURES", False)
TWILIO_WEBHOOK_BASE_URL = _env_file_first("TWILIO_WEBHOOK_BASE_URL", "").rstrip("/")
WHATSAPP_DEMO_LOCATION_FALLBACK = _env_bool("WHATSAPP_DEMO_LOCATION_FALLBACK", False)
WHATSAPP_DEMO_LAT = float(os.getenv("WHATSAPP_DEMO_LAT", "32.2196"))
WHATSAPP_DEMO_LNG = float(os.getenv("WHATSAPP_DEMO_LNG", "76.3234"))

# Feature flags
STRICT_LOCATION_GATE = _env_bool("STRICT_LOCATION_GATE", True)
DHARAMSALA_REGION_RADIUS_KM = float(os.getenv("DHARAMSALA_REGION_RADIUS_KM", "1000"))

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
