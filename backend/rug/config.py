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
    embed_dim: int = 768  # must equal the schema's vector size (rug.db.models.EMBED_DIM)
    embed_batch: int = 32
    # nomic-embed-text expects task prefixes; set both to "" for models that don't.
    embed_doc_prefix: str = "search_document: "
    embed_query_prefix: str = "search_query: "

    chunk_chars: int = 3200  # ~800 tokens at ~4 chars/token
    chunk_overlap: int = 400
    ocr_min_px: int = 100

    # Per-file resource budgets: a document exceeding them fails alone instead of
    # stalling or exhausting the indexer.
    max_uncompressed_mb: int = 512
    max_images_per_doc: int = 100
    ocr_max_pixels: int = 25_000_000  # larger images are downscaled before OCR
    ocr_timeout_s: int = 60

    # Chat model. The tag is a default and has not been verified against the Ollama library.
    chat_model: str = "qwen2.5:7b-instruct-q4_K_M"
    # Set explicitly on every request: the server-side default window is small, and an
    # oversized prompt would be cut off without an error.
    chat_num_ctx: int = 8192
    chat_timeout_s: int = 300

    # Retrieval
    retrieval_top_k: int = 8
    retrieval_candidates: int = 50  # per branch (keyword / vector) before fusion
    rrf_k: int = 60
    # Ceiling on the WHOLE prompt in characters (system prompt, question, overview, excerpt
    # headers and bodies; ~4 chars/token), leaving room for the answer inside chat_num_ctx.
    context_char_budget: int = 16000
    max_question_chars: int = 2000  # longer questions are rejected, not silently truncated
    summary_prompt_chars: int = 1500
    # Scopes with at most this many chunks are scored exactly; larger ones use the HNSW
    # index (approximate when filtered) and fall back to exact if it under-delivers.
    exact_scan_max_chunks: int = 100_000
    hnsw_ef_search: int = 1000

    # Document-name resolution (thresholds are tuned on the synthetic corpus; re-check
    # them against real filenames).
    resolver_accept_named: float = 0.75  # query names a document without an ID
    resolver_accept_id: float = 0.5  # query contains a document ID
    resolver_margin: float = 0.12  # runners-up this close to the winner make it ambiguous
    resolver_max_candidates: int = 5
    id_pattern: str = r"(?<![A-Za-z0-9])([A-Za-z]{2,6})([-_ ]?)(\d{2,})(?![A-Za-z0-9])"

    summary_window_chars: int = 9000

    # --- Authentication (Active Directory over LDAP) ---------------------------------------
    # Users are verified by binding to the directory as themselves (no service-account
    # secret to store): as DOMAIN\user when ldap_netbios_domain is set, else user@ldap_upn_suffix.
    ldap_url: str = ""  # e.g. ldaps://dc1.corp.local; empty means login is not configured
    ldap_start_tls: bool = False  # for ldap:// URLs; ldaps:// is already encrypted
    ldap_allow_insecure: bool = False  # permit an unencrypted ldap:// bind (development only)
    ldap_ca_certs_file: str = ""  # PEM bundle to trust for the directory's certificate
    ldap_timeout_s: int = 8
    ldap_base_dn: str = ""  # where user entries are searched, e.g. dc=corp,dc=local
    ldap_upn_suffix: str = ""
    ldap_netbios_domain: str = ""
    ldap_admin_group_dn: str = ""  # members may manage folder access; it grants no documents

    # --- Sessions and API ------------------------------------------------------------------
    session_secret: str = ""  # required to serve; at least 32 characters
    session_ttl_s: int = 8 * 3600
    cookie_secure: bool = True  # set false only for plain-http development
    max_upload_mb: int = 50
    login_max_failures_user: int = 5  # failed logins per username per window
    login_max_failures_ip: int = 20  # failed logins per client address per window
    login_window_s: int = 600
    trusted_proxies: list[str] = []  # reverse-proxy addresses whose X-Forwarded-For is believed

    scan_interval_s: int = 600
    # Refuse a scan that would delete more than this share of the catalog (e.g. the NAS
    # share is not mounted and the mount point is empty). Override with --allow-mass-delete.
    max_delete_fraction: float = 0.5


@lru_cache
def get_settings() -> Settings:
    return Settings()
