from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PDFSTUDIO_", extra="ignore")

    store_root: Path = Path("./data")
    db_path: Path = Path("./data/pdfstudio.sqlite3")
    bind_host: str = "127.0.0.1"
    allowed_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    max_upload_bytes: int = 200_000_000
    max_pages: int = 2000
    max_render_dpi: int = 600
    job_timeout_seconds: int = 900


@lru_cache
def get_settings() -> Settings:
    return Settings()
