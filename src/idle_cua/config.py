from __future__ import annotations

import os
from pathlib import Path


DEFAULT_DATA_DIR = Path.home() / ".idle-cua"
PRESEEDED_ALLOWLIST = [
    "x.com",
    "reddit.com",
    "youtube.com",
    "github.com",
    "news.ycombinator.com",
    "arxiv.org",
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "tiktok.com",
    "bsky.app",
    "threads.net",
    "mastodon.social",
    "google.com",
]


def resolve_data_dir(explicit: str | Path | None = None) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("IDLE_CUA_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return DEFAULT_DATA_DIR


class IdleCuaConfig:
    """Minimal config per spec — keeps data dir and limits."""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        readonly: bool = True,
        idle_threshold_minutes: int = 10,
        session_max_minutes: int = 45,
        session_max_actions: int = 200,
        daily_llm_call_limit: int = 150,
        allowlist: list[str] | None = None,
    ) -> None:
        self.data_dir = resolve_data_dir(data_dir)
        self.readonly = readonly
        self.idle_threshold_minutes = idle_threshold_minutes
        self.session_max_minutes = session_max_minutes
        self.session_max_actions = session_max_actions
        self.daily_llm_call_limit = daily_llm_call_limit
        self.allowlist = list(allowlist) if allowlist is not None else list(PRESEEDED_ALLOWLIST)

    @property
    def profile_path(self) -> Path:
        return self.data_dir / "profile.json"

    @property
    def config_path(self) -> Path:
        return self.data_dir / "config.json"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "idle_cua.db"
