from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DATA_DIR = Path.home() / ".idlecua"
DEFAULT_CONFIG_NAME = "config.json"

@dataclass
class IdleCuaConfig:
    """Public configuration for IdleCua.

    Only the owner can mutate policy; the agent never self-expands the allowlist.
    Secrets (API keys) are not stored here — use env / credential store.
    """

    data_dir: Path = field(default_factory=lambda: Path(
        os.environ.get("IDLECUA_DATA_DIR", str(DEFAULT_DATA_DIR))
    ).expanduser())
    readonly: bool = True
    require_idle: bool = True
    max_duration_minutes: int = 45
    max_actions: int = 200
    max_llm_calls_per_day: int = 150
    idle_threshold_seconds: int = 600
    allowlist: list[str] = field(default_factory=lambda: [
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
    ])

    def __post_init__(self) -> None:
        if isinstance(self.data_dir, str):
            self.data_dir = Path(self.data_dir).expanduser()
        if self.max_duration_minutes <= 0 or self.max_duration_minutes > 45:
            raise ValueError("max_duration_minutes must be 1..45")
        if self.max_actions <= 0 or self.max_actions > 200:
            raise ValueError("max_actions must be 1..200")

    @property
    def config_path(self) -> Path:
        return self.data_dir / DEFAULT_CONFIG_NAME

    def to_dict(self) -> dict:
        return {
            "data_dir": str(self.data_dir),
            "readonly": self.readonly,
            "require_idle": self.require_idle,
            "max_duration_minutes": self.max_duration_minutes,
            "max_actions": self.max_actions,
            "max_llm_calls_per_day": self.max_llm_calls_per_day,
            "idle_threshold_seconds": self.idle_threshold_seconds,
            "allowlist": self.allowlist,
        }

    def save(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        p = self.config_path
        p.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        return p

    @classmethod
    def load(cls, data_dir: Path | str | None = None) -> "IdleCuaConfig":
        base = Path(data_dir).expanduser() if data_dir is not None else Path(
            os.environ.get("IDLECUA_DATA_DIR", str(DEFAULT_DATA_DIR))
        ).expanduser()
        p = base / DEFAULT_CONFIG_NAME
        if not p.exists():
            return cls(data_dir=base)
        raw = json.loads(p.read_text(encoding="utf-8"))
        # data_dir in file is authoritative but constructor's data_dir wins if explicitly passed
        raw.pop("data_dir", None)
        return cls(data_dir=base, **raw)

    @classmethod
    def from_dict(cls, data: dict) -> "IdleCuaConfig":
        d = dict(data)
        if "data_dir" in d:
            d["data_dir"] = Path(d["data_dir"]).expanduser()
        return cls(**d)
