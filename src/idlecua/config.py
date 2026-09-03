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
        os.environ.get("IDLECUA_DATA_DIR") or os.environ.get("IDLE_CUA_DATA_DIR") or str(DEFAULT_DATA_DIR)
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
    deny_zones: list[str] = field(default_factory=lambda: [
        "/messages",
        "/inbox",
        "/dm",
        "/direct",
        "/chat",
        "/settings",
        "/account",
        "/password",
        "/2fa",
        "/two-factor",
        "/billing",
        "/payment",
        "/reauth",
        "/re-auth",
        "/notifications",
    ])

    # Driver selection — env IDLECUA_USE_REAL_DRIVER=1 forces real driver when available
    use_real_driver: bool = field(default_factory=lambda: os.environ.get("IDLECUA_USE_REAL_DRIVER", "").lower() in ("1", "true", "yes"))

    # Idle detector selection — env IDLECUA_USE_REAL_IDLE=1 forces Quartz HID detector
    use_real_idle_detector: bool = field(default_factory=lambda: os.environ.get("IDLECUA_USE_REAL_IDLE", "").lower() in ("1", "true", "yes"))

    # Browser main-profile consent (issue #12) — mirrored from Profile for config-level explicit consent.
    # True only when owner has explicitly granted main-profile attachment (profile interview or `profile grant-browser`).
    # This is the product-level consent; the driver also requires `cua-driver --grant existing-profile` at runtime.
    browser_main_profile_granted: bool = False
    browser_main_profile_browser: str = "chrome"
    browser_main_profile_granted_at: str | None = None

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
            "deny_zones": self.deny_zones,
            "use_real_driver": self.use_real_driver,
            "use_real_idle_detector": self.use_real_idle_detector,
            "browser_main_profile_granted": self.browser_main_profile_granted,
            "browser_main_profile_browser": self.browser_main_profile_browser,
            "browser_main_profile_granted_at": self.browser_main_profile_granted_at,
        }

    def save(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        p = self.config_path
        p.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        return p

    @classmethod
    def load(cls, data_dir: Path | str | None = None) -> IdleCuaConfig:
        base = Path(data_dir).expanduser() if data_dir is not None else Path(
            os.environ.get("IDLECUA_DATA_DIR") or os.environ.get("IDLE_CUA_DATA_DIR") or str(DEFAULT_DATA_DIR)
        ).expanduser()
        p = base / DEFAULT_CONFIG_NAME
        if not p.exists():
            return cls(data_dir=base)
        raw = json.loads(p.read_text(encoding="utf-8"))
        # data_dir in file is authoritative but constructor's data_dir wins if explicitly passed
        raw.pop("data_dir", None)
        # Filter to known fields for back-compat (ignore stale/unknown keys)
        allowed = set(cls.__dataclass_fields__.keys())
        filtered = {k: v for k, v in raw.items() if k in allowed}
        return cls(data_dir=base, **filtered)

    def has_browser_main_profile_consent(self) -> bool:
        """Check config-level consent; profile is authoritative but this mirrors for quick checks."""
        return bool(self.browser_main_profile_granted)

    def record_browser_consent(self, granted: bool, browser: str = "chrome", granted_at: str | None = None) -> None:
        self.browser_main_profile_granted = bool(granted)
        self.browser_main_profile_browser = browser
        self.browser_main_profile_granted_at = granted_at
        self.save()

    @classmethod
    def from_dict(cls, data: dict) -> IdleCuaConfig:
        d = dict(data)
        if "data_dir" in d:
            d["data_dir"] = Path(d["data_dir"]).expanduser()
        # Back-compat: ignore unknown keys gracefully but map browser consent if present
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})  # type: ignore
