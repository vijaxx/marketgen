"""Application configuration.

Every secret is read from the process environment on the server side only. No
key is ever emitted to the browser: see `app/main.py` for the public config
endpoint, which deliberately exposes booleans instead of values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REPO_DIR = BASE_DIR.parent


@dataclass(frozen=True)
class Settings:
    database_url: str
    content_provider: str
    gemini_api_key: str | None
    gemini_model: str
    allow_live_publishing: bool
    seed_demo_tenants: bool

    @property
    def sqlite_path(self) -> str:
        url = self.database_url
        if url.startswith("sqlite:///"):
            return url[len("sqlite:///") :]
        return url

    @property
    def gemini_enabled(self) -> bool:
        return bool(self.gemini_api_key)


def _flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    return Settings(
        database_url=os.environ.get("DATABASE_URL", f"sqlite:///{REPO_DIR / 'marketgen.db'}"),
        content_provider=os.environ.get("CONTENT_PROVIDER", "stub").strip().lower(),
        gemini_api_key=(os.environ.get("GEMINI_API_KEY") or "").strip() or None,
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-2.0-flash"),
        # Hard off-switch. Even when true, the live adapters in this repository
        # raise LivePublishingDisabled -- there is no network posting code path.
        allow_live_publishing=_flag("ALLOW_LIVE_PUBLISHING"),
        seed_demo_tenants=_flag("SEED_DEMO_TENANTS", "true"),
    )


settings = load_settings()
