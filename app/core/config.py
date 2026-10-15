"""Configuración del servidor; nunca se entregan sus secretos al navegador."""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")
DRIVE_ID = re.compile(r"^[A-Za-z0-9_-]{10,200}$")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    if value not in {"true", "false", "1", "0", "yes", "no"}:
        raise ValueError(f"{name} debe ser true o false")
    return value in {"true", "1", "yes"}


def env_path(name: str, default: str) -> Path:
    path = Path(os.getenv(name, default)).expanduser()
    return path if path.is_absolute() else ROOT / path


@dataclass(frozen=True)
class Settings:
    deployment_mode: str = field(default_factory=lambda: os.getenv("DEPLOYMENT_MODE", "cloud" if os.getenv("VERCEL") == "1" else "local").strip().lower())
    app_access_key: str = field(default_factory=lambda: os.getenv("APP_ACCESS_KEY", "").strip(), repr=False)
    document_manifest_json: str = field(default_factory=lambda: os.getenv("DOCUMENT_MANIFEST_JSON", ""), repr=False)
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", "").strip(), repr=False)
    openai_model: str = field(default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip())
    drive_folder_id: str = field(default_factory=lambda: os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip())
    vector_store_id: str = field(default_factory=lambda: os.getenv("VECTOR_STORE_ID", "").strip())
    local_admin_key: str = field(default_factory=lambda: os.getenv("LOCAL_ADMIN_KEY", ""), repr=False)
    drive_include_subfolders: bool = field(default_factory=lambda: env_bool("DRIVE_INCLUDE_SUBFOLDERS"))
    api_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("API_TIMEOUT_SECONDS", "45")))
    openai_max_retries: int = field(default_factory=lambda: int(os.getenv("OPENAI_MAX_RETRIES", "2")))
    index_poll_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("INDEX_POLL_TIMEOUT_SECONDS", "180")))
    oauth_timeout_seconds: int = field(default_factory=lambda: int(os.getenv("OAUTH_TIMEOUT_SECONDS", "180")))
    max_file_bytes: int = field(default_factory=lambda: int(os.getenv("MAX_FILE_BYTES", "20971520")))
    drive_max_files: int = field(default_factory=lambda: int(os.getenv("DRIVE_MAX_FILES", "200")))
    max_output_tokens: int = field(default_factory=lambda: int(os.getenv("MAX_OUTPUT_TOKENS", "3000")))
    search_max_results: int = field(default_factory=lambda: int(os.getenv("SEARCH_MAX_RESULTS", "12")))
    requests_per_minute: int = field(default_factory=lambda: int(os.getenv("REQUESTS_PER_MINUTE", "20")))
    google_credentials_path: Path = field(default_factory=lambda: env_path("GOOGLE_CREDENTIALS_PATH", "credentials.json"))
    google_token_path: Path = field(default_factory=lambda: env_path("GOOGLE_TOKEN_PATH", "token.json"))

    def __post_init__(self):
        if self.deployment_mode not in {"local", "cloud"}:
            raise ValueError("DEPLOYMENT_MODE debe ser local o cloud")
        limits = {
            "api_timeout_seconds": (1, 120), "openai_max_retries": (0, 3),
            "index_poll_timeout_seconds": (1, 600), "oauth_timeout_seconds": (10, 600),
            "max_file_bytes": (1024, 52428800), "drive_max_files": (1, 500),
            "max_output_tokens": (256, 16000), "search_max_results": (1, 50),
            "requests_per_minute": (1, 120),
        }
        for name, (low, high) in limits.items():
            if not low <= getattr(self, name) <= high:
                raise ValueError(f"{name} debe estar entre {low} y {high}")

    @property
    def is_cloud(self) -> bool:
        return self.deployment_mode == "cloud"

    def checks(self) -> dict[str, bool]:
        return {
            "openai": bool(self.openai_api_key),
            "drive_folder": bool(DRIVE_ID.fullmatch(self.drive_folder_id)),
            "google_credentials": not self.is_cloud and self.google_credentials_path.is_file(),
            "oauth_token": not self.is_cloud and self.google_token_path.is_file(),
        }


settings = Settings()
