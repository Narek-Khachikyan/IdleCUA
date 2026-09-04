"""T4: diagnostics bundle behind Application API (spec #23, ticket #27).

- Doctor output and diagnostics endpoint agree on permissions, driver probe,
  profile validity, secrets-scan verdict for same machine/data dir.
- Remediation hints identical on both surfaces; secrets never leak.
- Decisions covered through Application API with fakes; route/CLI tests remain thin smoke.
- HTTP contract unchanged (GET /api/v1/diagnostics keys: permissions[{name,granted,remediation}],
  driver{ok,message}, profile{valid,confirmed,errors}, secrets_scan{ok,findings[{source,pattern}]},
  scheduler_lock{locked,info}, watch_loop{running,pid,started_at,idle_threshold}).
"""

from pathlib import Path
import json
import sys
import types

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from idlecua.config import IdleCuaConfig
from idlecua.app import IdleCua
from idlecua.profile.models import Profile
from idlecua.profile.store import save_profile
from idlecua.server.app import create_app, _scheduler_state


def _confirmed_profile(tmp: Path, thr: int = 600) -> Profile:
    p = Profile()
    p.confirmed = True
    p.autonomy_boundaries.allowed_sites = ["x.com", "reddit.com"]
    p.autonomy_boundaries.allowed_hours = "00:00-23:59"
    p.computer_usage.idle_threshold_seconds = thr
    save_profile(p, tmp / "profile.json")
    return p


def test_app_get_diagnostics_shape_and_contract(tmp_path: Path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir(parents=True)
    _confirmed_profile(d, thr=650)
    # Mock secrets scan to avoid repo finding making test flaky
    from idlecua.secrets_scan import ScanResult

    fake_scan = ScanResult(ok=True, findings=[], scanned_files=2, scanned_db_tables=0, skipped=[])
    monkeypatch.setattr("idlecua.secrets_scan.scan_project", lambda project_root=None, data_dir=None: fake_scan)
    import idlecua.secrets_scan as ss_mod

    monkeypatch.setattr(ss_mod, "scan_project", lambda project_root=None, data_dir=None: fake_scan)

    app = IdleCua(config=IdleCuaConfig(data_dir=d))
    bundle = app.get_diagnostics()
    # Required contract keys must exist
    for k in ("permissions", "driver", "profile", "secrets_scan", "scheduler_lock", "watch_loop"):
        assert k in bundle, f"missing {k}"
    # permissions shape
    assert isinstance(bundle["permissions"], list)
    for p in bundle["permissions"]:
        assert "name" in p and "granted" in p and "remediation" in p
    # driver shape
    assert "ok" in bundle["driver"] and "message" in bundle["driver"]
    assert isinstance(bundle["driver"]["ok"], bool)
    # profile shape
    assert "valid" in bundle["profile"] and "confirmed" in bundle["profile"] and "errors" in bundle["profile"]
    assert isinstance(bundle["profile"]["errors"], list)
    # secrets_scan shape (masked, no snippet)
    assert "ok" in bundle["secrets_scan"] and "findings" in bundle["secrets_scan"]
    for f in bundle["secrets_scan"]["findings"]:
        assert "source" in f and "pattern" in f
        assert "snippet" not in f  # never leak snippet
    # no raw secret in bundle (masked)
    assert "sk-SECRET" not in json.dumps(bundle)
    # scheduler_lock shape
    assert "locked" in bundle["scheduler_lock"] and "info" in bundle["scheduler_lock"]
    # watch_loop shape
    wl = bundle["watch_loop"]
    for k in ("running", "pid", "started_at", "idle_threshold"):
        assert k in wl
    # Effective context present (guidance)
    assert "effective_idle_threshold" in bundle
    assert bundle["effective_idle_threshold"] == 650
    assert "effective_limits" in bundle


def test_app_diagnostics_uses_fakes_not_live(tmp_path: Path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir(parents=True)
    _confirmed_profile(d)

    # Fake permissions
    from idlecua.profile.permissions import PermissionStatus

    fake_perms = [
        PermissionStatus(name="Accessibility", granted=False, remediation="Grant Accessibility: test-remediation-A"),
        PermissionStatus(name="Screen Recording", granted=True, remediation="Grant Screen Recording: test-remediation-B"),
    ]

    monkeypatch.setattr("idlecua.profile.permissions.check_permissions", lambda: fake_perms)

    # Fake driver
    fake_driver = types.ModuleType("cua_driver")
    fake_driver.__version__ = "9.9.9-fake"

    class _FakeStatus:
        accessibility = True
        screen_recording = False

    fake_driver.current_mac_os_permission_status = lambda: _FakeStatus()
    monkeypatch.setitem(sys.modules, "cua_driver", fake_driver)

    # Fake secrets scan with findings (ensure masking)
    from idlecua.secrets_scan import ScanResult, SecretFinding

    fake_scan = ScanResult(ok=False, findings=[SecretFinding(source="fake.txt", pattern="openai_api_key", snippet="sk-SECRET-LEAK")], scanned_files=5, scanned_db_tables=1, skipped=[])

    def _fake_scan_project(project_root=None, data_dir=None):
        return fake_scan

    monkeypatch.setattr("idlecua.secrets_scan.scan_project", _fake_scan_project)
    monkeypatch.setattr("idlecua.app.scan_project", _fake_scan_project, raising=False)

    app = IdleCua(config=IdleCuaConfig(data_dir=d))
    import idlecua.secrets_scan as ss_mod

    monkeypatch.setattr(ss_mod, "scan_project", _fake_scan_project)

    bundle = app.get_diagnostics()
    # Permissions via fake
    assert any(p["name"] == "Accessibility" and p["granted"] is False for p in bundle["permissions"])
    assert any("test-remediation-A" in p["remediation"] for p in bundle["permissions"])
    # Driver via fake
    assert bundle["driver"]["ok"] is True
    assert "9.9.9-fake" in bundle["driver"]["message"]
    assert "accessibility=True" in bundle["driver"]["message"]
    assert "screen_recording=False" in bundle["driver"]["message"]
    # Secrets via fake, masked
    assert bundle["secrets_scan"]["ok"] is False
    assert len(bundle["secrets_scan"]["findings"]) == 1
    assert bundle["secrets_scan"]["findings"][0]["source"] == "fake.txt"
    assert bundle["secrets_scan"]["findings"][0]["pattern"] == "openai_api_key"
    # Ensure snippet not leaked (finding should not contain raw secret)
    dumped = json.dumps(bundle["secrets_scan"])
    assert "sk-SECRET-LEAK" not in dumped
    assert "snippet" not in dumped.lower()

    monkeypatch.delitem(sys.modules, "cua_driver", raising=False)


def test_profile_validity_reflects_t3_settings_authority(tmp_path: Path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir(parents=True)
    _confirmed_profile(d)
    from idlecua.secrets_scan import ScanResult

    fake_scan = ScanResult(ok=True, findings=[], scanned_files=0, scanned_db_tables=0, skipped=[])
    monkeypatch.setattr("idlecua.secrets_scan.scan_project", lambda project_root=None, data_dir=None: fake_scan)
    import idlecua.secrets_scan as ss_mod

    monkeypatch.setattr(ss_mod, "scan_project", lambda project_root=None, data_dir=None: fake_scan)

    app = IdleCua(config=IdleCuaConfig(data_dir=d))
    b1 = app.get_diagnostics()
    assert b1["profile"]["valid"] is True
    assert b1["profile"]["confirmed"] is True
    assert b1["profile"]["errors"] == []

    # Make profile invalid via allowlist domain (T3 validation)
    p = Profile.model_validate(json.loads((d / "profile.json").read_text(encoding="utf-8")))
    p.autonomy_boundaries.allowed_sites = ["bad domain!!"]
    save_profile(p, d / "profile.json")
    b2 = IdleCua(config=IdleCuaConfig(data_dir=d)).get_diagnostics()
    assert b2["profile"]["valid"] is False
    assert any("invalid domain" in e.lower() for e in b2["profile"]["errors"])

    # Unconfirmed also invalid
    p2 = Profile()
    p2.confirmed = False
    p2.autonomy_boundaries.allowed_sites = ["x.com"]
    save_profile(p2, d / "profile.json")
    b3 = IdleCua(config=IdleCuaConfig(data_dir=d)).get_diagnostics()
    assert b3["profile"]["valid"] is False
    assert b3["profile"]["confirmed"] is False
    assert any("unconfirmed" in e.lower() for e in b3["profile"]["errors"])

    # Missing profile
    (d / "profile.json").unlink()
    b4 = IdleCua(config=IdleCuaConfig(data_dir=d)).get_diagnostics()
    assert b4["profile"]["valid"] is False
    assert b4["profile"]["confirmed"] is False
    assert any("No profile" in e for e in b4["profile"]["errors"])


def test_http_and_cli_parity_same_bundle(tmp_path: Path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir(parents=True)
    _confirmed_profile(d, thr=700)

    # Use fakes for stable parity
    from idlecua.profile.permissions import PermissionStatus

    fake_perms = [
        PermissionStatus(name="Accessibility", granted=False, remediation="Grant Accessibility: parity-remediation"),
        PermissionStatus(name="Screen Recording", granted=False, remediation="Grant Screen Recording: parity-remediation-B"),
    ]
    monkeypatch.setattr("idlecua.profile.permissions.check_permissions", lambda: fake_perms)

    fake_driver = types.ModuleType("cua_driver")
    fake_driver.__version__ = "0.0.1-parity"

    class _S:
        accessibility = True
        screen_recording = True

    fake_driver.current_mac_os_permission_status = lambda: _S()
    monkeypatch.setitem(sys.modules, "cua_driver", fake_driver)

    from idlecua.secrets_scan import ScanResult

    fake_scan = ScanResult(ok=True, findings=[], scanned_files=2, scanned_db_tables=0, skipped=[])
    monkeypatch.setattr("idlecua.secrets_scan.scan_project", lambda project_root=None, data_dir=None: fake_scan)
    import idlecua.secrets_scan as ss_mod

    monkeypatch.setattr(ss_mod, "scan_project", lambda project_root=None, data_dir=None: fake_scan)

    app = IdleCua(config=IdleCuaConfig(data_dir=d))
    direct = app.get_diagnostics()

    srv = create_app(data_dir=d, test_mode=True)
    client = TestClient(srv)
    resp = client.get("/api/v1/diagnostics")
    assert resp.status_code == 200, resp.text
    http = resp.json()

    for k in ("permissions", "driver", "profile", "secrets_scan", "scheduler_lock", "watch_loop"):
        assert k in http

    assert http["permissions"] == direct["permissions"]
    # HTTP exposes only the versioned driver keys {ok, message}; the bundle
    # carries extra structured fields for CLI renderers (no message parsing).
    assert http["driver"] == {"ok": direct["driver"]["ok"], "message": direct["driver"]["message"]}
    assert direct["driver"]["version"] == "0.0.1-parity"
    assert direct["driver"]["accessibility"] is True
    assert direct["driver"]["screen_recording"] is True
    assert http["profile"] == direct["profile"]
    assert http["secrets_scan"]["ok"] == direct["secrets_scan"]["ok"]
    assert http["secrets_scan"]["findings"] == direct["secrets_scan"]["findings"]
    assert http["scheduler_lock"]["locked"] == direct["scheduler_lock"]["locked"]
    assert http["watch_loop"]["idle_threshold"] == 700
    assert direct["watch_loop"]["idle_threshold"] == 700

    from idlecua.cli import app as cli_app

    runner = CliRunner()
    res = runner.invoke(cli_app, ["doctor", "--data-dir", str(d)])
    assert res.exit_code == 0, res.stdout + res.stderr
    # CLI output should contain same remediation hints as http permissions (for not-granted)
    for perm in http["permissions"]:
        # CLI prints remediation only for not-granted (MISSING/UNKNOWN); our fake perms are both not granted (False), so both should appear
        assert perm["remediation"] in res.stdout, f"remediation missing for {perm['name']}"
        assert perm["name"] in res.stdout
    assert http["driver"]["message"] in res.stdout or "cua-driver" in res.stdout
    if http["profile"]["valid"]:
        assert "Profile validation: OK" in res.stdout
    else:
        for err in http["profile"]["errors"]:
            assert err in res.stdout or "Profile validation errors" in res.stdout
    if http["secrets_scan"]["ok"]:
        assert "Secrets scan PASSED" in res.stdout
    else:
        assert "Secrets scan FAILED" in res.stdout

    ui = client.get("/diagnostics")
    assert ui.status_code == 200
    for perm in http["permissions"]:
        assert perm["name"] in ui.text

    monkeypatch.delitem(sys.modules, "cua_driver", raising=False)


def test_remediation_hints_identical_and_secrets_never_leak(tmp_path: Path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir(parents=True)
    _confirmed_profile(d)
    # Mock perms to have deterministic remediation
    from idlecua.profile.permissions import PermissionStatus

    fake_perms = [
        PermissionStatus(name="Accessibility", granted=False, remediation="Grant Accessibility: test-remediation-identical"),
        PermissionStatus(name="Screen Recording", granted=False, remediation="Grant Screen Recording: test-remediation-identical-B"),
    ]
    monkeypatch.setattr("idlecua.profile.permissions.check_permissions", lambda: fake_perms)

    from idlecua.secrets_scan import ScanResult

    fake_scan_ok = ScanResult(ok=True, findings=[], scanned_files=2, scanned_db_tables=0, skipped=[])
    monkeypatch.setattr("idlecua.secrets_scan.scan_project", lambda project_root=None, data_dir=None: fake_scan_ok)
    import idlecua.secrets_scan as ss_mod

    monkeypatch.setattr(ss_mod, "scan_project", lambda project_root=None, data_dir=None: fake_scan_ok)

    app = IdleCua(config=IdleCuaConfig(data_dir=d))
    bundle = app.get_diagnostics()
    perms = bundle["permissions"]
    for p in perms:
        assert p["remediation"]
        assert "Grant" in p["remediation"] or "test-remediation" in p["remediation"]

    srv = create_app(data_dir=d, test_mode=True)
    client = TestClient(srv)
    http = client.get("/api/v1/diagnostics").json()
    from idlecua.cli import app as cli_app

    runner = CliRunner()
    cli_res = runner.invoke(cli_app, ["doctor", "--data-dir", str(d)])
    assert cli_res.exit_code == 0
    for p in http["permissions"]:
        assert p["remediation"] in cli_res.stdout

    # Secrets never leak
    from idlecua.secrets_scan import SecretFinding

    fake_scan2 = ScanResult(ok=False, findings=[SecretFinding(source="db:memory:1", pattern="openai_api_key", snippet="sk-FAKESECRET1234567890")], scanned_files=1, scanned_db_tables=1, skipped=[])
    monkeypatch.setattr("idlecua.secrets_scan.scan_project", lambda project_root=None, data_dir=None: fake_scan2)
    monkeypatch.setattr(ss_mod, "scan_project", lambda project_root=None, data_dir=None: fake_scan2)

    app2 = IdleCua(config=IdleCuaConfig(data_dir=d))
    b2 = app2.get_diagnostics()
    dumped = json.dumps(b2)
    assert "sk-FAKESECRET" not in dumped
    assert "snippet" not in dumped.lower()
    http2 = client.get("/api/v1/diagnostics").json()
    assert "sk-FAKESECRET" not in json.dumps(http2)
    assert all("snippet" not in json.dumps(f).lower() for f in http2["secrets_scan"]["findings"])
    cli2 = runner.invoke(cli_app, ["doctor", "--data-dir", str(d)])
    assert "sk-FAKESECRET" not in cli2.stdout
    cli_vs = runner.invoke(cli_app, ["verify-secrets", "--data-dir", str(d), "--json"])
    assert cli_vs.exit_code == 1
    payload = json.loads(cli_vs.stdout)
    assert payload["ok"] is False
    assert "sk-FAKESECRET" not in json.dumps(payload)


def test_http_contract_unchanged_and_thin_smoke(tmp_path: Path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir(parents=True)
    _confirmed_profile(d)
    from idlecua.secrets_scan import ScanResult

    fake_scan = ScanResult(ok=True, findings=[], scanned_files=1, scanned_db_tables=0, skipped=[])
    monkeypatch.setattr("idlecua.secrets_scan.scan_project", lambda project_root=None, data_dir=None: fake_scan)
    import idlecua.secrets_scan as ss_mod

    monkeypatch.setattr(ss_mod, "scan_project", lambda project_root=None, data_dir=None: fake_scan)

    srv = create_app(data_dir=d, test_mode=True)
    client = TestClient(srv)
    resp = client.get("/api/v1/diagnostics")
    assert resp.status_code == 200
    j = resp.json()
    assert set(["permissions", "driver", "profile", "secrets_scan", "scheduler_lock", "watch_loop"]).issubset(set(j.keys()))
    assert isinstance(j["permissions"], list)
    for entry in j["permissions"]:
        assert "name" in entry and "granted" in entry and "remediation" in entry
    assert isinstance(j["driver"], dict) and "ok" in j["driver"] and "message" in j["driver"]
    assert isinstance(j["profile"], dict) and "valid" in j["profile"] and "confirmed" in j["profile"] and "errors" in j["profile"]
    assert isinstance(j["secrets_scan"], dict) and "ok" in j["secrets_scan"] and "findings" in j["secrets_scan"]
    for f in j["secrets_scan"]["findings"]:
        assert "source" in f and "pattern" in f
        assert "snippet" not in f
    assert isinstance(j["scheduler_lock"], dict) and "locked" in j["scheduler_lock"] and "info" in j["scheduler_lock"]
    assert isinstance(j["watch_loop"], dict) and "running" in j["watch_loop"] and "pid" in j["watch_loop"] and "started_at" in j["watch_loop"] and "idle_threshold" in j["watch_loop"]
    # No raw secret leak
    assert "sk-" not in json.dumps(j) or "••••" in json.dumps(j)
    assert "snippet" not in json.dumps(j).lower()
    openapi = client.get("/api/openapi.json")
    assert openapi.status_code == 200
    # OpenAPI should be masked
    assert "sk-" not in openapi.text or "••••" in openapi.text


def test_verify_secrets_thin_and_masked(tmp_path: Path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir(parents=True)
    _confirmed_profile(d)
    from idlecua.secrets_scan import ScanResult

    fake_ok = ScanResult(ok=True, findings=[], scanned_files=2, scanned_db_tables=0, skipped=[])
    monkeypatch.setattr("idlecua.secrets_scan.scan_project", lambda project_root=None, data_dir=None: fake_ok)
    import idlecua.secrets_scan as ss_mod

    monkeypatch.setattr(ss_mod, "scan_project", lambda project_root=None, data_dir=None: fake_ok)

    from idlecua.cli import app as cli_app

    runner = CliRunner()
    res = runner.invoke(cli_app, ["verify-secrets", "--data-dir", str(d)])
    assert res.exit_code == 0, res.stdout + res.stderr
    assert "Secrets scan" in res.stdout

    from idlecua.secrets_scan import SecretFinding

    fake_scan = ScanResult(ok=False, findings=[SecretFinding(source="fake.txt", pattern="generic_api_key_field", snippet="sk-fake-leak")], scanned_files=2, scanned_db_tables=0, skipped=[])
    monkeypatch.setattr("idlecua.secrets_scan.scan_project", lambda project_root=None, data_dir=None: fake_scan)
    monkeypatch.setattr(ss_mod, "scan_project", lambda project_root=None, data_dir=None: fake_scan)
    res2 = runner.invoke(cli_app, ["verify-secrets", "--data-dir", str(d), "--json"])
    assert res2.exit_code == 1, res2.stdout
    payload = json.loads(res2.stdout)
    assert payload["ok"] is False
    assert payload["findings"][0]["pattern"] == "generic_api_key_field"
    assert "sk-fake-leak" not in json.dumps(payload)


def test_scheduler_lock_and_watch_loop_in_bundle(tmp_path: Path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir(parents=True)
    _confirmed_profile(d, thr=550)
    from idlecua.secrets_scan import ScanResult

    fake_scan = ScanResult(ok=True, findings=[], scanned_files=0, scanned_db_tables=0, skipped=[])
    monkeypatch.setattr("idlecua.secrets_scan.scan_project", lambda project_root=None, data_dir=None: fake_scan)
    import idlecua.secrets_scan as ss_mod

    monkeypatch.setattr(ss_mod, "scan_project", lambda project_root=None, data_dir=None: fake_scan)

    from idlecua.server.lock import acquire_lock, release_lock

    app = IdleCua(config=IdleCuaConfig(data_dir=d))
    b1 = app.get_diagnostics()
    assert b1["scheduler_lock"]["locked"] is False
    assert b1["scheduler_lock"]["info"] is None
    assert b1["watch_loop"]["idle_threshold"] == 550
    assert b1["watch_loop"]["running"] is False

    info = acquire_lock(d)
    try:
        b2 = IdleCua(config=IdleCuaConfig(data_dir=d)).get_diagnostics()
        assert b2["scheduler_lock"]["locked"] is True
        assert b2["scheduler_lock"]["info"] is not None
        assert b2["scheduler_lock"]["info"]["pid"] == info["pid"]
        assert b2["watch_loop"]["pid"] == info["pid"]
        custom_watch = {"running": True, "pid": 12345, "started_at": "2026-09-03T00:00:00", "idle_threshold": 999}
        b3 = IdleCua(config=IdleCuaConfig(data_dir=d)).get_diagnostics(watch_loop=custom_watch)
        assert b3["watch_loop"]["running"] is True
        assert b3["watch_loop"]["pid"] == 12345
        assert b3["watch_loop"]["idle_threshold"] == 999
    finally:
        release_lock(d)

    b4 = IdleCua(config=IdleCuaConfig(data_dir=d)).get_diagnostics()
    assert b4["scheduler_lock"]["locked"] is False


def test_diagnostics_ui_renders_driver_probe_and_secrets_scan(tmp_path: Path, monkeypatch):
    """Issue #36: /diagnostics renders the driver probe and secrets-scan verdict."""
    d = tmp_path / "data"
    d.mkdir(parents=True)
    _confirmed_profile(d)

    fake_driver = types.ModuleType("cua_driver")
    fake_driver.__version__ = "9.9.9-ui"

    class _S:
        accessibility = True
        screen_recording = False

    fake_driver.current_mac_os_permission_status = lambda: _S()
    monkeypatch.setitem(sys.modules, "cua_driver", fake_driver)

    from idlecua.secrets_scan import ScanResult, SecretFinding

    fake_scan = ScanResult(
        ok=False,
        findings=[SecretFinding(source="ui-fake.txt", pattern="openai_api_key", snippet="sk-UILEAK-123")],
        scanned_files=1,
        scanned_db_tables=0,
        skipped=[],
    )
    monkeypatch.setattr("idlecua.secrets_scan.scan_project", lambda project_root=None, data_dir=None: fake_scan)
    import idlecua.secrets_scan as ss_mod

    monkeypatch.setattr(ss_mod, "scan_project", lambda project_root=None, data_dir=None: fake_scan)

    srv = create_app(data_dir=d, test_mode=True)
    client = TestClient(srv)
    html = client.get("/diagnostics").text
    assert "Driver probe" in html
    assert "9.9.9-ui" in html
    assert "Secrets scan FAILED" in html
    assert "ui-fake.txt" in html
    assert "openai_api_key" in html
    assert "sk-UILEAK-123" not in html

    fake_ok = ScanResult(ok=True, findings=[], scanned_files=1, scanned_db_tables=0, skipped=[])
    monkeypatch.setattr("idlecua.secrets_scan.scan_project", lambda project_root=None, data_dir=None: fake_ok)
    monkeypatch.setattr(ss_mod, "scan_project", lambda project_root=None, data_dir=None: fake_ok)
    html_ok = client.get("/diagnostics").text
    assert "Secrets scan PASSED" in html_ok

    monkeypatch.delitem(sys.modules, "cua_driver", raising=False)
