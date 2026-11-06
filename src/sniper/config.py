"""Runtime settings, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    db_path: Path = field(default_factory=lambda: Path(_env("SNIPER_DB_PATH", "data/sniper.sqlite")))
    data_api_url: str = field(default_factory=lambda: _env("SNIPER_DATA_API_URL", "https://data-api.polymarket.com/v2"))
    gamma_url: str = field(default_factory=lambda: _env("SNIPER_GAMMA_URL", "https://gamma-api.polymarket.com"))
    clob_url: str = field(default_factory=lambda: _env("SNIPER_CLOB_URL", "https://clob.polymarket.com"))
    http_timeout_s: float = field(default_factory=lambda: float(_env("SNIPER_HTTP_TIMEOUT_S", "30")))
    # Fraction of the published per-IP rate limits we allow ourselves to use.
    rate_limit_share: float = field(default_factory=lambda: float(_env("SNIPER_RATE_LIMIT_SHARE", "0.6")))
    user_agent: str = "sniper-polymaster/0.1 (+https://github.com/kanuk331/sniper.polymaster)"
