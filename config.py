import os
import secrets


class BaseConfig:
    # Security
    # An explicitly empty value (common in a freshly copied .env) must not
    # disable Flask sessions. Generate a process-local fallback; production
    # should still provide a persistent value so sessions survive restarts.
    SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_urlsafe(48)

    # 🗄Database
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Archive
    ARCHIVE_PURGE_DAYS = int(
        os.getenv("ARCHIVE_PURGE_DAYS", 30)
    )

    # Full-system automatic backup. The default destination is the Desktop
    # detected for the operating-system account running this application.
    AUTO_BACKUP_ENABLED = os.getenv("AUTO_BACKUP_ENABLED", "1")
    AUTO_BACKUP_HOUR = os.getenv("AUTO_BACKUP_HOUR", "15")
    AUTO_BACKUP_MINUTE = os.getenv("AUTO_BACKUP_MINUTE", "0")
    AUTO_BACKUP_DIR = os.getenv("AUTO_BACKUP_DIR", "")

    # Local smart intake for manually uploaded inbound correspondence.
    # The attachment is analyzed in memory and is never sent to an external AI.
    CORR_INTAKE_MAX_BYTES = int(os.getenv("CORR_INTAKE_MAX_BYTES", 25 * 1024 * 1024))
    CORR_INTAKE_MAX_TEXT_CHARS = int(os.getenv("CORR_INTAKE_MAX_TEXT_CHARS", 20_000))
    CORR_INTAKE_MAX_PDF_PAGES = int(os.getenv("CORR_INTAKE_MAX_PDF_PAGES", 40))
    CORR_INTAKE_OCR_ENABLED = os.getenv("CORR_INTAKE_OCR_ENABLED", "1").strip().lower() in {
        "1", "true", "yes", "on"
    }
    CORR_INTAKE_TESSERACT_CMD = os.getenv("CORR_INTAKE_TESSERACT_CMD", "tesseract")
    CORR_INTAKE_OCR_LANGUAGES = os.getenv("CORR_INTAKE_OCR_LANGUAGES", "ara+eng")
    CORR_INTAKE_OCR_MAX_PAGES = int(os.getenv("CORR_INTAKE_OCR_MAX_PAGES", 10))
    CORR_INTAKE_OCR_DPI = int(os.getenv("CORR_INTAKE_OCR_DPI", 200))
    CORR_INTAKE_OCR_TIMEOUT_SECONDS = float(
        os.getenv("CORR_INTAKE_OCR_TIMEOUT_SECONDS", 45)
    )
    CORR_INTAKE_OCR_MAX_IMAGE_PIXELS = int(
        os.getenv("CORR_INTAKE_OCR_MAX_IMAGE_PIXELS", 40_000_000)
    )

    # The assistant is always available.  Safe public questions may use the
    # external model when configured and reachable; every other case falls
    # back to the local, permission-aware assistant.
    ASSISTANT_AI_ENABLED = os.getenv("ASSISTANT_AI_ENABLED", "1")
    ASSISTANT_OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    ASSISTANT_OPENAI_MODEL = os.getenv("OPENAI_CHAT_MODEL")
    ASSISTANT_AI_TIMEOUT = float(os.getenv("ASSISTANT_AI_TIMEOUT", "20"))
    # Fail-closed privacy boundary for external AI:
    # - PUBLIC_ONLY (default): external AI receives only short, non-sensitive
    #   general chat; protected/internal requests always remain local.
    # - LOCAL_ONLY: never call an external model.
    # Any unknown value is treated as LOCAL_ONLY by the service.
    ASSISTANT_AI_PRIVACY_MODE = os.getenv("ASSISTANT_AI_PRIVACY_MODE", "PUBLIC_ONLY")
    ASSISTANT_AI_PUBLIC_MAX_CHARS = int(os.getenv("ASSISTANT_AI_PUBLIC_MAX_CHARS", "600"))
    # Public current-events questions can use the hosted web-search tool.  The
    # privacy gate still prevents system, government, or personal data from
    # leaving this server.
    ASSISTANT_AI_WEB_SEARCH_ENABLED = os.getenv("ASSISTANT_AI_WEB_SEARCH_ENABLED", "1")
    ASSISTANT_AI_CONTEXT_CHARS = int(os.getenv("ASSISTANT_AI_CONTEXT_CHARS", "16000"))
    ASSISTANT_AI_MAX_OUTPUT_TOKENS = int(os.getenv("ASSISTANT_AI_MAX_OUTPUT_TOKENS", "1100"))
    ASSISTANT_LOCAL_AI_ENABLED = os.getenv("ASSISTANT_LOCAL_AI_ENABLED", "1")
    ASSISTANT_LOCAL_AI_URL = os.getenv("ASSISTANT_LOCAL_AI_URL", "http://127.0.0.1:11434/api/chat")
    ASSISTANT_LOCAL_AI_MODEL = os.getenv("ASSISTANT_LOCAL_AI_MODEL", "")
    ASSISTANT_LOCAL_AI_TIMEOUT = float(os.getenv("ASSISTANT_LOCAL_AI_TIMEOUT", "60"))
    ASSISTANT_LOCAL_AI_CONTEXT_CHARS = int(os.getenv("ASSISTANT_LOCAL_AI_CONTEXT_CHARS", "12000"))
    ASSISTANT_MAX_MESSAGE_CHARS = int(os.getenv("ASSISTANT_MAX_MESSAGE_CHARS", "2000"))
    ASSISTANT_RATE_LIMIT = int(os.getenv("ASSISTANT_RATE_LIMIT", "20"))
    ASSISTANT_RATE_WINDOW_SECONDS = int(os.getenv("ASSISTANT_RATE_WINDOW_SECONDS", "60"))
    # Local retrieval over repository source/docs/templates plus admin-only DB
    # schema knowledge. Runtime data continues to use the normal permission gates.
    ASSISTANT_PROJECT_KNOWLEDGE_ENABLED = os.getenv("ASSISTANT_PROJECT_KNOWLEDGE_ENABLED", "1")
    ASSISTANT_INDEX_REFRESH_SECONDS = int(os.getenv("ASSISTANT_INDEX_REFRESH_SECONDS", "300"))
    ASSISTANT_INDEX_MAX_FILE_BYTES = int(os.getenv("ASSISTANT_INDEX_MAX_FILE_BYTES", "1500000"))
    ASSISTANT_INDEX_CHUNK_LINES = int(os.getenv("ASSISTANT_INDEX_CHUNK_LINES", "48"))
    ASSISTANT_INDEX_MAX_RESULTS = int(os.getenv("ASSISTANT_INDEX_MAX_RESULTS", "7"))


class DevConfig(BaseConfig):
    DEBUG = True


class ProdConfig(BaseConfig):
    DEBUG = False
    # A persistent SECRET_KEY should be supplied by the server environment.
    # The generated fallback keeps production mode safe for an initial run,
    # but existing browser sessions will be invalidated after a restart.
    SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_urlsafe(48)
