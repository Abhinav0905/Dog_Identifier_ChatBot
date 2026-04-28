import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent

load_dotenv(BASE_DIR / ".env")
STORAGE_DIR = BASE_DIR / "storage"
STORAGE_DIR.mkdir(exist_ok=True)
DB_PATH = BASE_DIR / "dharamsala.db"

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
# Selects the AI backend: "claude" (default) or "openai"
MODEL_PROVIDER = os.getenv("MODEL_PROVIDER", "claude").lower()

ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "changeme")

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

# Severity thresholds
ESCALATION_SEVERITY_THRESHOLD = 7  # 1-10 scale, >=7 triggers alert
SIMILARITY_PHASH_THRESHOLD = 10     # Hamming distance, <=10 is "similar"
SIMILARITY_EMBEDDING_THRESHOLD = 0.85

MAX_IMAGE_SIZE_MB = 10
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
