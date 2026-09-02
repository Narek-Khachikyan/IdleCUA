"""Single source for browser main-profile consent check.

Product-level explicit consent for attaching to the owner's main Chrome profile.
Stored in profile.json (autonomy_boundaries.browser_consent) and mirrored to
config.json (browser_main_profile_granted). Both must agree; profile is authoritative.

This module is the single place that knows the file layout, so other modules
do not duplicate the 7-file scatter.
"""
from __future__ import annotations

import json
from pathlib import Path


def has_consent(data_dir: Path | None) -> bool:
    if data_dir is None:
        return False
    p = Path(data_dir).expanduser()
    # Check config.json mirror first
    try:
        cfg_path = p / "config.json"
        if cfg_path.exists():
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            if raw.get("browser_main_profile_granted") is True:
                return True
    except Exception:
        pass
    # Check profile.json authoritative
    try:
        ppath = p / "profile.json"
        if ppath.exists():
            data = json.loads(ppath.read_text(encoding="utf-8"))
            ab = data.get("autonomy_boundaries", {})
            bc = ab.get("browser_consent", {})
            if isinstance(bc, dict) and bc.get("main_profile_granted") is True:
                return True
            bc2 = data.get("browser_consent", {})
            if isinstance(bc2, dict) and bc2.get("main_profile_granted") is True:
                return True
    except Exception:
        pass
    return False


def record_consent(data_dir: Path, granted: bool, browser: str = "chrome", granted_at: str | None = None) -> None:
    """Record consent to both profile.json and config.json via their owning modules."""
    # Use config and profile modules to avoid direct JSON writes here
    try:
        from .config import IdleCuaConfig
        cfg = IdleCuaConfig.load(data_dir)
        cfg.record_browser_consent(bool(granted), browser=browser, granted_at=granted_at)
    except Exception:
        pass
    try:
        from .profile.store import load_profile, save_profile
        from .profile.models import BrowserConsent

        ppath = Path(data_dir) / "profile.json"
        profile = load_profile(ppath)
        if profile is not None:
            bc = BrowserConsent(
                main_profile_granted=bool(granted),
                browser=browser,
                granted_at=granted_at,
                grant_method="browser_consent helper" if granted else None,
            )
            profile.autonomy_boundaries.browser_consent = bc
            profile.browser_consent = bc
            profile.touch()
            save_profile(profile, ppath)
    except Exception:
        pass
