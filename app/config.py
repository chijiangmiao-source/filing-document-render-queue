from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Pinned LibreOffice inside the container: The Document Foundation build
# 26.2.6.3 installed at /opt/libreoffice26.2 (see Dockerfile), fetched and
# SHA-256 verified per architecture.
DEFAULT_SOFFICE_BIN = "/opt/libreoffice26.2/program/soffice"

TEN_MIB = 10 * 1024 * 1024


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=os.getenv("ENV_FILE", ".env"), extra="ignore")

    database_url: str = "sqlite:///./docx2pdf.db"
    storage_dir: str = "./data/storage"

    # Lease protocol (the acceptance contract pins these defaults).
    lease_seconds: float = 30.0
    renew_interval_seconds: float = 10.0
    max_attempts: int = 3
    poll_interval_seconds: float = 1.0

    max_upload_bytes: int = TEN_MIB
    conversion_timeout_seconds: float = 180.0

    soffice_bin: str = DEFAULT_SOFFICE_BIN

    @property
    def db_path(self) -> str:
        # sqlite:////abs/path -> /abs/path ; sqlite:///./rel -> rel path
        prefix = "sqlite:///"
        return self.database_url[len(prefix):] if self.database_url.startswith(prefix) else ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
