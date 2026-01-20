import os


class BaseConfig:
    # Security
    SECRET_KEY = os.getenv("SECRET_KEY", "change-me")

    # 🗄Database
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Archive
    ARCHIVE_PURGE_DAYS = int(
        os.getenv("ARCHIVE_PURGE_DAYS", 30)
    )


class DevConfig(BaseConfig):
    DEBUG = True


class ProdConfig(BaseConfig):
    DEBUG = False
