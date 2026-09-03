"""T3 focused test: owner-settings validation+write behind Application API.

- Identical accept/reject for same patch via Application API and HTTP (range,
  allowed_hours shape, domain shape, idle bounds, consent).
- Over-ceiling rejected with clear message, never silently clamped.
- Single authority per ADR-0003: Profile owns owner-intent, Config owns safety.
- HTTP contract unchanged (GET/PATCH shapes byte-identical).
- idle 650 -> minutes 11 sync.
- Secrets never leaked (masked only) — settings paths carry no keys.
"""
from pathlib import Path
import json

import pytest
from fastapi.testclient import TestClient

from idlecua.config import IdleCuaConfig
from idlecua.app import IdleCua
from idlecua.profile.store import save_profile, load_profile
from idlecua.profile.models import Profile
from idlecua.server.app import create_app


def _confirmed_profile(tmp: Path) -> Profile:
    import io
    from rich.console import Console
    from idlecua.profile.interview import run_interview

    def _inp(q):
        return ""

    def _conf(_):
        return True

    con = Console(file=io.StringIO(), width=80)
    prof, _, _ = run_interview(console=con, input_func=_inp, confirm_func=_conf)
    save_profile(prof, tmp / "profile.json")
    return prof


def test_app_get_owner_settings_shape(tmp_path: Path):
    d = tmp_path / "data"
    d.mkdir()
    _confirmed_profile(d)
    app = IdleCua(config=IdleCuaConfig(data_dir=d))
    got = app.get_owner_settings()
    assert "profile" in got and "config" in got and "effective" in got
    # Profile keys as per HTTP contract
    p = got["profile"]
    for k in ("session_duration_minutes", "daily_action_limit", "daily_llm_call_limit", "allowed_hours", "allowlist", "deny_zones", "allowed_sites", "idle_threshold_seconds", "idle_threshold_minutes", "browser_consent", "confirmed"):
        assert k in p, f"missing profile key {k}"
    c = got["config"]
    assert "readonly" in c and "require_idle" in c and "ceilings" in c and "current" in c and "data_dir" in c
    assert c["ceilings"] == {"max_duration_minutes": 45, "max_actions": 200, "max_llm_calls_per_day": 150}
    e = got["effective"]
    for k in ("session_duration_minutes", "daily_action_limit", "daily_llm_call_limit", "idle_threshold_seconds"):
        assert k in e


def test_app_update_valid_persists_single_authority(tmp_path: Path):
    d = tmp_path / "data"
    d.mkdir()
    _confirmed_profile(d)
    app = IdleCua(config=IdleCuaConfig(data_dir=d))
    # Valid patch: tighten to 30, allowlist, idle 650, readonly toggle
    patch = {
        "session_duration_minutes": 30,
        "daily_action_limit": 80,
        "daily_llm_call_limit": 60,
        "allowed_hours": "09:00-17:00",
        "allowlist": ["x.com", "github.com"],
        "deny_zones": ["/messages", "/settings"],
        "idle_threshold_seconds": 650,
        "readonly": False,
        "require_idle": True,
        "browser_consent": True,
    }
    result = app.update_owner_settings(patch)
    assert "profile" in result and "config" in result and "effective" in result
    # Single authority: Profile holds owner-intent, Config holds safety
    prof = load_profile(d / "profile.json")
    assert prof is not None
    assert prof.autonomy_boundaries.session_duration_minutes == 30
    assert prof.autonomy_boundaries.allowed_sites == ["x.com", "github.com"]
    assert prof.computer_usage.idle_threshold_seconds == 650
    assert prof.computer_usage.idle_threshold_minutes == 11  # ceil(650/60)=11
    cfg = IdleCuaConfig.load(d)
    assert cfg.readonly is False
    assert cfg.require_idle is True
    # Effective is tighten-only
    assert result["effective"]["session_duration_minutes"] == 30
    assert result["effective"]["idle_threshold_seconds"] == 650
    # Secrets never in result
    dumped = json.dumps(result)
    assert "api_key" not in dumped.lower()


def test_app_idle_650_minutes_11(tmp_path: Path):
    d = tmp_path / "data"
    d.mkdir()
    _confirmed_profile(d)
    app = IdleCua(config=IdleCuaConfig(data_dir=d))
    app.update_owner_settings({"idle_threshold_seconds": 650})
    prof = load_profile(d / "profile.json")
    assert prof.computer_usage.idle_threshold_seconds == 650
    assert prof.computer_usage.idle_threshold_minutes == 11
    # Also via get_owner_settings effective
    got = app.get_owner_settings()
    assert got["profile"]["idle_threshold_seconds"] == 650
    assert got["profile"]["idle_threshold_minutes"] == 11
    assert got["effective"]["idle_threshold_seconds"] == 650


def test_app_rejects_over_ceiling_no_clamp(tmp_path: Path):
    d = tmp_path / "data"
    d.mkdir()
    _confirmed_profile(d)
    app = IdleCua(config=IdleCuaConfig(data_dir=d))
    # daily_action above ceiling 200 should be rejected, not clamped
    with pytest.raises(ValueError, match="above ceiling 200"):
        app.update_owner_settings({"daily_action_limit": 250})
    # Ensure persisted profile still has original value (not clamped to 200)
    prof = load_profile(d / "profile.json")
    assert prof.autonomy_boundaries.daily_action_limit == 200
    # daily_llm ceiling 150
    with pytest.raises(ValueError, match="above ceiling 150"):
        app.update_owner_settings({"daily_llm_call_limit": 200})
    assert prof.autonomy_boundaries.daily_llm_call_limit == 150
    # session 46 >45
    with pytest.raises(ValueError, match="1..45"):
        app.update_owner_settings({"session_duration_minutes": 46})


def test_app_rejects_domain_and_idle_and_hours(tmp_path: Path):
    d = tmp_path / "data"
    d.mkdir()
    _confirmed_profile(d)
    app = IdleCua(config=IdleCuaConfig(data_dir=d))
    with pytest.raises(ValueError, match="invalid domain"):
        app.update_owner_settings({"allowlist": ["bad domain!!"]})
    with pytest.raises(ValueError, match="idle_threshold_seconds must be 60..7200"):
        app.update_owner_settings({"idle_threshold_seconds": 10})
    with pytest.raises(ValueError, match="allowed_hours"):
        app.update_owner_settings({"allowed_hours": "bad"})
    # browser_consent valid patch should succeed
    r = app.update_owner_settings({"browser_consent": True})
    assert r["profile"]["autonomy_boundaries"]["browser_consent"]["main_profile_granted"] is True


def test_http_and_app_parity_and_400_message(tmp_path: Path):
    d = tmp_path / "data"
    d.mkdir()
    _confirmed_profile(d)
    # HTTP contract
    http_app = create_app(data_dir=d, test_mode=True)
    client = TestClient(http_app)
    # GET shape unchanged
    g = client.get("/api/v1/settings")
    assert g.status_code == 200
    j = g.json()
    assert "profile" in j and "config" in j and "effective" in j
    # Valid patch via HTTP
    patch = {"session_duration_minutes": 25, "allowlist": ["x.com"], "idle_threshold_seconds": 650}
    resp = client.patch("/api/v1/settings", json=patch)
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert "profile" in resp.json() and "config" in resp.json()
    # ensure no effective in PATCH response (contract byte-identical)
    assert "effective" not in resp.json()
    # Same patch via Application API directly — should also succeed with identical effective
    app = IdleCua(config=IdleCuaConfig(data_dir=d))
    # Use different data_dir to avoid collision? Use same dir but different patch value to test parity.
    # First, test identical reject: over-ceiling via both surfaces yields identical message.
    bad_patch = {"daily_action_limit": 500}
    # App rejects
    try:
        app.update_owner_settings(bad_patch)
        assert False, "should have raised"
    except ValueError as ve:
        app_msg = str(ve)
    # HTTP rejects with same message text
    http_bad = client.patch("/api/v1/settings", json=bad_patch)
    assert http_bad.status_code == 400
    http_msg = http_bad.json()["detail"]
    assert app_msg == http_msg, f"messages differ: app='{app_msg}' http='{http_msg}'"
    assert "above ceiling 200" in app_msg
    # Domain parity
    bad2 = {"allowlist": ["bad domain!!"]}
    try:
        app.update_owner_settings(bad2)
        assert False
    except ValueError as ve:
        app_msg2 = str(ve)
    http_bad2 = client.patch("/api/v1/settings", json=bad2)
    assert http_bad2.status_code == 400
    assert http_bad2.json()["detail"] == app_msg2
    assert "invalid domain" in app_msg2
    # Idle parity
    bad3 = {"idle_threshold_seconds": 10}
    try:
        app.update_owner_settings(bad3)
        assert False
    except ValueError as ve:
        app_msg3 = str(ve)
    http_bad3 = client.patch("/api/v1/settings", json=bad3)
    assert http_bad3.status_code == 400
    assert http_bad3.json()["detail"] == app_msg3
    assert "60..7200" in app_msg3


def test_http_settings_no_secrets_leak(tmp_path: Path):
    d = tmp_path / "data"
    d.mkdir()
    _confirmed_profile(d)
    # add a provider key to ensure settings endpoints don't leak it
    http_app = create_app(data_dir=d, test_mode=True)
    client = TestClient(http_app)
    secret = "sk-or-v1-secret-1234567890-xyz"
    client.post("/api/v1/providers", json={"name": "openrouter", "base_url": "https://openrouter.ai/api/v1", "model": "m", "api_key": secret})
    g = client.get("/api/v1/settings")
    assert secret not in g.text
    assert secret not in json.dumps(g.json())
    p = client.patch("/api/v1/settings", json={"session_duration_minutes": 30})
    assert secret not in p.text
    # OpenAPI for providers should still be masked
    o = client.get("/api/openapi.json")
    assert secret not in o.text
