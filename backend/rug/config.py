from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RUG_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://rug:rug@localhost:5432/rug"
    # Root of the mounted NAS share. Top-level subfolders are permission folders.
    docs_dir: Path = Path("docs")

    ollama_url: str = "http://localhost:11434"
    embed_model: str = "nomic-embed-text"
    embed_dim: int = 768
    embed_batch: int = 32
    # nomic-embed-text expects task prefixes; set both to "" for models that don't.
    embed_doc_prefix: str = "search_document: "
    embed_query_prefix: str = "search_query: "

    chunk_chars: int = 3200  # ~800 tokens at ~4 chars/token
    chunk_overlap: int = 400
    ocr_min_px: int = 100

    scan_interval_s: int = 600


@lru_cache
def get_settings() -> Settings:
    return Settings()
