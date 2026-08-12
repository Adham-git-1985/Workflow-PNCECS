import os
import secrets


class BaseConfig:
    # Security
    SECRET_KEY = os.getenv("SECRET_KEY", "change-me")

    # 🗄Database
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Archive
    ARCHIVE_PURGE_DAYS = int(
        os.getenv("ARCHIVE_PURGE_DAYS", 30)
    )

    # In-system chat assistant. External AI is opt-in; the local,
    # permission-aware help mode works without any external service.
    ASSISTANT_AI_ENABLED = os.getenv("ASSISTANT_AI_ENABLED", "0")
    ASSISTANT_OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    ASSISTANT_OPENAI_MODEL = os.getenv("OPENAI_CHAT_MODEL")
    ASSISTANT_AI_TIMEOUT = float(os.getenv("ASSISTANT_AI_TIMEOUT", "20"))
    # Fail-closed privacy boundary for external AI:
    # - LOCAL_ONLY: never call an external model.
    # - PUBLIC_ONLY: external AI receives only short, non-sensitive general chat.
    # Any unknown value is treated as LOCAL_ONLY by the service.
    ASSISTANT_AI_PRIVACY_MODE = os.getenv("ASSISTANT_AI_PRIVACY_MODE", "LOCAL_ONLY")
    ASSISTANT_AI_PUBLIC_MAX_CHARS = int(os.getenv("ASSISTANT_AI_PUBLIC_MAX_CHARS", "600"))
    ASSISTANT_AI_CONTEXT_CHARS = int(os.getenv("ASSISTANT_AI_CONTEXT_CHARS", "16000"))
    ASSISTANT_AI_MAX_OUTPUT_TOKENS = int(os.getenv("ASSISTANT_AI_MAX_OUTPUT_TOKENS", "1100"))
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
