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
    ASSISTANT_MAX_MESSAGE_CHARS = int(os.getenv("ASSISTANT_MAX_MESSAGE_CHARS", "1200"))
    ASSISTANT_RATE_LIMIT = int(os.getenv("ASSISTANT_RATE_LIMIT", "20"))
    ASSISTANT_RATE_WINDOW_SECONDS = int(os.getenv("ASSISTANT_RATE_WINDOW_SECONDS", "60"))


class DevConfig(BaseConfig):
    DEBUG = True


class ProdConfig(BaseConfig):
    DEBUG = False
    # A persistent SECRET_KEY should be supplied by the server environment.
    # The generated fallback keeps production mode safe for an initial run,
    # but existing browser sessions will be invalidated after a restart.
    SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_urlsafe(48)
