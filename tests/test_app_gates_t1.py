"""T1: gates + idle threshold behind the Application API (spec #23, ticket #24)."""

from pathlib import Path

import pytest

from idlecua.app import IdleCua, ProfileNotConfirmedError
from idlecua.config import IdleCuaConfig
from idlecua.idle import FakeIdleDetector
from idlecua.profile.models import Profile
from idlecua.profile.store import save_profile


def _save(p: Profile, d: Path) -> None:
    save_profile(p, d / "profile.json")


def _confirmed(d: Path, thr: int = 600) -> Profile:
    p = Profile()
    p.confirmed = True
    p.autonomy_boundaries.allowed_sites = ["x.com", "reddit.com"]
    p.autonomy_boundaries.allowed_hours = "00:00-23:59"
    p.computer_usage.idle_threshold_seconds = thr
    _save(p, d)
    return p


def test_effective_threshold_profile_wins_fallback(tmp_path: Path):
    cfg = IdleCuaConfig(data_dir=tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    app = IdleCua(config=cfg, idle_detector=FakeIdleDetector(idle_seconds=1000, locked=False))
    assert app.get_effective_idle_threshold() == 600
    _confirmed(tmp_path, thr=650)
    assert app.get_effective_idle_threshold() == 650


def test_profile_gate_blocks_run_task_identically(tmp_path: Path):
    cfg = IdleCuaConfig(data_dir=tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    app = IdleCua(config=cfg, idle_detector=FakeIdleDetector(idle_seconds=1000, locked=False))
    with pytest.raises(ProfileNotConfirmedError, match="No profile found"):
        app.run_task("hi")
    p = Profile()
    p.confirmed = False
    p.autonomy_boundaries.allowed_sites = ["x.com"]
    _save(p, tmp_path)
    with pytest.raises(ProfileNotConfirmedError, match="unconfirmed"):
        app.run_task("hi")
    ok, reason = app.check_profile_confirmed()
    assert not ok and "unconfirmed" in reason
    can_ok, can_reason = app.can_start()
    assert not can_ok and "profile" in can_reason


def test_can_start_idle_and_screen_single_source(tmp_path: Path):
    cfg = IdleCuaConfig(data_dir=tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    _confirmed(tmp_path, thr=600)
    idle_app = IdleCua(config=cfg, idle_detector=FakeIdleDetector(idle_seconds=10, locked=False))
    ok, reason = idle_app.can_start()
    assert not ok and "idle gate blocked" in reason
    locked_app = IdleCua(config=cfg, idle_detector=FakeIdleDetector(idle_seconds=1000, locked=True))
    ok2, reason2 = locked_app.can_start()
    assert not ok2 and "screen" in reason2.lower()
    ready_app = IdleCua(config=cfg, idle_detector=FakeIdleDetector(idle_seconds=1000, locked=False))
    ok3, _ = ready_app.can_start()
    assert ok3
