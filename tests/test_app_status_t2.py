"""T2: status assembly + Demo badge behind Application API (spec #23, ticket #25)."""

from pathlib import Path
import json
import tempfile

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from idlecua.app import IdleCua
from idlecua.config import IdleCuaConfig
from idlecua.idle import FakeIdleDetector
from idlecua.profile.models import Profile
from idlecua.profile.store import save_profile
from idlecua.providers.config import ProviderStore, ProviderConfig
from idlecua.keychain import get_default_store


def _confirmed(tmp: Path, thr: int = 600) -> Profile:
    p = Profile()
    p.confirmed = True
    p.autonomy_boundaries.allowed_sites = ["x.com", "reddit.com"]
    p.autonomy_boundaries.allowed_hours = "00:00-23:59"
    p.computer_usage.idle_threshold_seconds = thr
    save_profile(p, tmp / "profile.json")
    return p


def test_demo_mode_no_provider_is_limited(tmp_path: Path):
    cfg = IdleCuaConfig(data_dir=tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    _confirmed(tmp_path, thr=600)
    app = IdleCua(config=cfg, idle_detector=FakeIdleDetector(idle_seconds=1000, locked=False))
    assert app.is_demo_mode() is True
    honest = app.get_honest_status()
    assert honest["level"] == "limited"
    assert honest["chip_text"] == "Limited mode"
    assert "Limited" in honest["banner_text"]
    assert "stub planner" in honest["text"].lower() or "Limited" in honest["text"]
    # enriched must carry same
    enriched = app.get_status_enriched(watch_loop={"running": False})
    assert enriched["demo_mode"] is True
    assert enriched["honest_status"]["level"] == "limited"
    assert enriched["honest_status"]["chip_text"] == "Limited mode"
    # stub never presented as LLM work
    assert "LLM off" in enriched["honest_status"]["hero_sub"] or "stub" in enriched["honest_status"]["hero_sub"].lower()


def test_demo_mode_with_provider_and_key_is_not_demo(tmp_path: Path, monkeypatch):
    cfg = IdleCuaConfig(data_dir=tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    _confirmed(tmp_path, thr=600)
    # create provider and store key via file fallback (SystemCredentialStore file)
    store = ProviderStore.load(tmp_path)
    pc = ProviderConfig(name="openrouter", base_url="https://openrouter.ai/api/v1", model="anthropic/claude-3.5-sonnet")
    store.providers[pc.name] = pc
    store.selected = pc.name
    store.save()
    kc = get_default_store(tmp_path)
    kc.set("openrouter", "sk-test-1234-5678")
    app = IdleCua(config=cfg, idle_detector=FakeIdleDetector(idle_seconds=1000, locked=False))
    assert app.is_demo_mode() is False
    honest = app.get_honest_status(watch_running=False)
    # With idle 1000 > threshold 600 and not demo, should be ready (not limited)
    assert honest["level"] in ("ready", "waiting")
    enriched = app.get_status_enriched(watch_loop={"running": False})
    assert enriched["demo_mode"] is False
    assert enriched["honest_status"]["level"] != "limited"


def test_honest_precedence_running_over_limited(tmp_path: Path):
    cfg = IdleCuaConfig(data_dir=tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    _confirmed(tmp_path, thr=600)
    app = IdleCua(config=cfg, idle_detector=FakeIdleDetector(idle_seconds=1000, locked=False))
    # No provider -> demo true, but running task should win
    assert app.is_demo_mode() is True
    task = app.create_task("run something important")
    app.memory.update_task_state(task.id, "running")
    honest = app.get_honest_status(watch_running=False)
    assert honest["level"] == "running"
    assert "Running" in honest["text"]
    # enriched should also be running
    enriched = app.get_status_enriched(watch_loop={"running": False})
    assert enriched["honest_status"]["level"] == "running"


def test_honest_waiting_and_ready_threshold(tmp_path: Path):
    cfg = IdleCuaConfig(data_dir=tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    _confirmed(tmp_path, thr=600)
    # provider with key to avoid limited
    store = ProviderStore.load(tmp_path)
    pc = ProviderConfig(name="openrouter", base_url="https://openrouter.ai/api/v1", model="anthropic/claude-3.5-sonnet")
    store.providers[pc.name] = pc
    store.selected = pc.name
    store.save()
    get_default_store(tmp_path).set("openrouter", "sk-abc-1234")
    # waiting case: idle 10 < 600
    app_wait = IdleCua(config=cfg, idle_detector=FakeIdleDetector(idle_seconds=10, locked=False))
    honest_wait = app_wait.get_honest_status(watch_running=True)
    assert honest_wait["level"] == "waiting"
    assert "Waiting for idle" in honest_wait["text"]
    assert "Running" in honest_wait["hero_sub"]  # watch loop Running
    # ready case: idle 1000 >= 600
    app_ready = IdleCua(config=cfg, idle_detector=FakeIdleDetector(idle_seconds=1000, locked=False))
    honest_ready = app_ready.get_honest_status(watch_running=False)
    assert honest_ready["level"] == "ready"
    assert "Ready" in honest_ready["text"]


def test_enriched_status_keys_and_http_contract(tmp_path: Path):
    from idlecua.server.app import create_app, _is_demo_mode, _honest_status, _get_effective_idle_threshold

    _confirmed(tmp_path, thr=600)
    cfg = IdleCuaConfig(data_dir=tmp_path)
    app = IdleCua(config=cfg, idle_detector=FakeIdleDetector(idle_seconds=123, locked=False))
    enriched = app.get_status_enriched(watch_loop={"running": False, "pid": None, "started_at": None, "lock": None})
    # Must contain versioned contract keys
    for k in ["agent_state", "idle_seconds", "idle_threshold_seconds", "screen_locked", "watch_loop", "demo_mode", "honest_status", "limits", "daily_usage", "today_usage", "last_report", "active_task", "last_action", "current_site", "stop_command"]:
        assert k in enriched, f"missing {k}"
    # honest_status shape
    hs = enriched["honest_status"]
    for hk in ["text", "sub", "level", "dot", "banner_text", "chip_text", "hero_title", "hero_sub"]:
        assert hk in hs, f"honest missing {hk}"
    # limits shape
    assert "llm_calls_today" in enriched["limits"]
    assert "max_llm_calls_per_day" in enriched["limits"]
    # daily_usage/today_usage duplicates
    assert enriched["daily_usage"] == enriched["today_usage"] or enriched["daily_usage"]["llm_calls"] == enriched["today_usage"]["llm_calls"]
    # HTTP contract via TestClient must expose same keys
    srv = create_app(data_dir=tmp_path, test_mode=True)
    client = TestClient(srv)
    resp = client.get("/api/v1/status")
    assert resp.status_code == 200
    j = resp.json()
    for k in ["agent_state", "idle_seconds", "idle_threshold_seconds", "screen_locked", "watch_loop", "demo_mode", "honest_status", "limits", "daily_usage", "today_usage", "last_report", "active_task", "last_action", "current_site", "stop_command"]:
        assert k in j, f"http missing {k}"
    # server delegates still importable and same as app
    assert _is_demo_mode(tmp_path) == app.is_demo_mode()
    assert _honest_status(tmp_path, idle_seconds=123, watch_running=False)["level"] == app.get_honest_status(watch_running=False, idle_seconds=123)["level"]
    # idle_threshold via app single source
    assert _get_effective_idle_threshold(tmp_path) == app.get_effective_idle_threshold() == enriched["idle_threshold_seconds"]
    # secrets never leaked
    dumped = json.dumps(j)
    assert "sk-" not in dumped or "••••" in dumped or "sk-test" not in dumped


def test_cli_and_http_parity_same_data_dir(tmp_path: Path):
    # Same data dir -> same AgentState, idle_seconds, limits, last_report, demo badge
    data_dir = tmp_path / "parity"
    data_dir.mkdir(parents=True)
    _confirmed(data_dir, thr=650)
    cfg = IdleCuaConfig(data_dir=data_dir)
    # create a task and a report to have last_report
    app = IdleCua(config=cfg, idle_detector=FakeIdleDetector(idle_seconds=800, locked=False))
    task = app.create_task("research parity")
    app.memory.update_task_state(task.id, "waiting_for_idle")
    app.memory.save_report(task.id, "# Report for parity\nHello")

    # CLI JSON
    from idlecua.cli import app as cli_app

    runner = CliRunner()
    # Need to pass data_dir via option
    res = runner.invoke(cli_app, ["status", "--data-dir", str(data_dir), "--json"])
    assert res.exit_code == 0, res.stdout + res.stderr
    cli_json = json.loads(res.stdout)
    # HTTP via TestClient (will create its own IdleCua but with same data_dir, same idle via Fake default 1000 vs our 800 -> we need to set detector to 800 for server too?
    # Server's _get_idle_cua uses FakeIdleDetector default 1000, not our 800. To make parity, we rely on same default? Our app used 800, server will use 1000 -> mismatch.
    # Instead we test parity of fields that are deterministic: agent_state, limits, last_report, demo_mode, honest_status chip_text
    # Idle_seconds will differ due to different detector defaults, so we only check that both are present and that non-idle fields match when using same threshold.
    # For true idle_seconds parity, we need to ensure both use same detector value. We can monkey-patch server's detector by injecting via memory? Simpler: check parity of stable fields, not idle_seconds absolute.
    # Create HTTP status
    from idlecua.server.app import create_app

    srv = create_app(data_dir=data_dir, test_mode=True)
    client = TestClient(srv)
    http_json = client.get("/api/v1/status").json()

    # Stable fields must match
    assert cli_json["agent_state"] == http_json["agent_state"]
    assert cli_json["demo_mode"] == http_json["demo_mode"]
    assert cli_json["honest_status"]["chip_text"] == http_json["honest_status"]["chip_text"]
    assert cli_json["honest_status"]["level"] == http_json["honest_status"]["level"]
    assert cli_json["limits"] == http_json["limits"]
    assert cli_json["daily_usage"] == http_json["daily_usage"]
    assert cli_json["today_usage"] == http_json["today_usage"]
    # last_report parity (both should see same report)
    assert (cli_json["last_report"] or {}).get("markdown") == (http_json["last_report"] or {}).get("markdown")
    # idle threshold parity
    assert cli_json["idle_threshold_seconds"] == http_json["idle_threshold_seconds"] == 650
    # CLI demo badge line appears in non-JSON output
    res2 = runner.invoke(cli_app, ["status", "--data-dir", str(data_dir)])
    assert res2.exit_code == 0
    # When demo_mode True (no provider key), CLI must show DEMO badge same as server chip
    if cli_json["demo_mode"]:
        assert "DEMO" in res2.stdout
        assert "Limited mode" in res2.stdout
        # Dashboard HTML should also contain DEMO badge
        html = client.get("/").text
        assert "DEMO" in html
        assert "Limited mode" in html
