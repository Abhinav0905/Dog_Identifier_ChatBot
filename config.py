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


STORAGE_DIR = _env_path("STORAGE_DIR", BASE_DIR / "storage")
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = _env_path("DB_PATH", BASE_DIR / "dharamsala.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

MODEL_PROVIDER = os.getenv("MODEL_PROVIDER", "openai").lower()
if MODEL_PROVIDER == "claude":
    raise ValueError("MODEL_PROVIDER 'claude' is not supported.")
OPENAI_API_KEY = _env_file_first("OPENAI_API_KEY", "")
OPENAI_MODEL = _env_file_first("OPENAI_MODEL", "gpt-5.4-mini")
OPENAI_VISION_MODEL = _env_file_first("OPENAI_VISION_MODEL", OPENAI_MODEL)
OPENAI_CHAT_MODEL = _env_file_first("OPENAI_CHAT_MODEL", OPENAI_MODEL)
OPENAI_ADMIN_MODEL = _env_file_first("OPENAI_ADMIN_MODEL", OPENAI_MODEL)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "changeme")

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

# Severity thresholds
DAR_PHONE_NUMBER = "+91 98828 58631"

ESCALATION_SEVERITY_THRESHOLD = 7  # 1-10 scale, >=7 triggers alert
SIMILARITY_PHASH_THRESHOLD = 10     # Hamming distance, <=10 is "similar"
SIMILARITY_EMBEDDING_THRESHOLD = 0.85

MAX_IMAGE_SIZE_MB = 10
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
