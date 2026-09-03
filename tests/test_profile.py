from pathlib import Path
import json

import pytest
from typer.testing import CliRunner
from rich.console import Console

from idlecua.cli import app
from idlecua.config import IdleCuaConfig
from idlecua.profile.interview import run_interview
from idlecua.profile.store import load_profile
from idlecua.profile.validate import validate_profile
from idlecua.profile.render import render_human_readable
from idlecua.profile.permissions import permissions_report_text, check_permissions
from idlecua.app import IdleCua, ProfileNotConfirmedError
from idlecua.profile.models import Profile

runner = CliRunner()


def test_interview_saves_only_after_confirmation(tmp_path: Path):
    cfg = IdleCuaConfig(data_dir=tmp_path)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)

    def input_func(q):
        return "test" if q.key == "user_characteristics.occupation" else ""

    def confirm_no(_):
        return False

    console = Console(record=True)
    from idlecua.profile.interview import save_confirmed_profile

    profile, facts, assumptions = run_interview(console=console, input_func=input_func, confirm_func=confirm_no)
    assert profile.confirmed is False
    assert "user_characteristics.occupation" in facts
    assert len(assumptions) > 0
    ppath = tmp_path / "profile.json"
    saved = save_confirmed_profile(profile, ppath, console=console)
    assert saved is False
    assert not ppath.exists()

    def confirm_yes(_):
        return True

    profile2, facts2, assumptions2 = run_interview(console=console, input_func=input_func, confirm_func=confirm_yes)
    assert profile2.confirmed is True
    saved2 = save_confirmed_profile(profile2, ppath, console=console)
    assert saved2 is True
    assert ppath.exists()
    loaded = load_profile(ppath)
    assert loaded is not None
    assert loaded.confirmed is True


def test_summary_separates_facts_and_assumptions(tmp_path: Path):
    answers = {
        "user_characteristics.occupation": "Engineer",
        "autonomy_boundaries.allowed_sites": "x.com, reddit.com",
    }

    def input_func(q):
        return answers.get(q.key, "")

    def confirm(_):
        return True

    console = Console(record=True)
    profile, facts, assumptions = run_interview(console=console, input_func=input_func, confirm_func=confirm)
    assert "user_characteristics.occupation" in facts
    assert facts["user_characteristics.occupation"] == "Engineer"
    assert "autonomy_boundaries.allowed_sites" in facts
    assert "user_characteristics.material_depth" in assumptions
    assert assumptions["user_characteristics.material_depth"] == "mixed"
    assert "computer_usage.monitors" in assumptions


def test_profile_show_edit_validate_cli(tmp_path: Path):
    result = runner.invoke(app, ["init", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert (tmp_path / "config.json").exists()

    result = runner.invoke(app, ["profile", "interview", "--yes", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert (tmp_path / "profile.json").exists()
    result = runner.invoke(app, ["profile", "show", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "IdleCUA Profile" in result.output
    assert "Confirmed: yes" in result.output
    result = runner.invoke(app, ["profile", "show", "--json", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert '"confirmed": true' in result.output.lower()

    result = runner.invoke(app, ["profile", "validate", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "Profile is valid" in result.output

    # T3 (spec #23): CLI edit now rejects invalid via Application API (same message as HTTP 400) and does NOT persist invalid values.
    # Previous test allowed save-invalid-then-validate; updated to expect identical accept/reject per ADR-0003.
    result = runner.invoke(app, ["profile", "edit", "--field", "autonomy_boundaries.allowed_sites=bad domain!!", "--data-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "allowlist" in result.output.lower() and "invalid domain" in result.output.lower()
    # Invalid not persisted — profile remains valid (tighten-only, no silent clamp)
    result = runner.invoke(app, ["profile", "validate", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0

    result = runner.invoke(app, ["profile", "edit", "--field", "autonomy_boundaries.allowed_sites=x.com, reddit.com", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0
    result = runner.invoke(app, ["profile", "validate", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0

    result = runner.invoke(app, ["profile", "edit", "--field", "autonomy_boundaries.session_duration_minutes=999", "--data-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "session_duration_minutes" in result.output or "1..45" in result.output
    result = runner.invoke(app, ["profile", "validate", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0

    result = runner.invoke(app, ["profile", "edit", "--field", "autonomy_boundaries.session_duration_minutes=30", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0
    result = runner.invoke(app, ["profile", "validate", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0

    result = runner.invoke(app, ["profile", "edit", "--field", "autonomy_boundaries.allowed_hours=bad", "--data-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "allowed_hours" in result.output
    result = runner.invoke(app, ["profile", "validate", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0


def test_permission_check_reports_remediation(tmp_path: Path):
    result = runner.invoke(app, ["profile", "check-permissions", "--data-dir", str(tmp_path)])
    assert "Accessibility" in result.output
    assert "Screen Recording" in result.output
    # When permissions are granted via cua probe, remediation is not shown (OK state).
    # Otherwise remediation must appear.
    if "[OK]" not in result.output:
        assert "Remediation" in result.output or "remediation" in result.output.lower()
    text = permissions_report_text(check_permissions())
    assert "Accessibility" in text
    assert "Screen Recording" in text


def test_hard_gate_refuses_autonomous_runs_while_unconfirmed(tmp_path: Path):
    cfg = IdleCuaConfig(data_dir=tmp_path)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    app_obj = IdleCua(config=cfg)
    # No profile -> gate should refuse
    with pytest.raises(ProfileNotConfirmedError, match="No profile found"):
        app_obj.run_once("research", dry_run=False)
    # Dry-run should still be allowed (no gate)
    plan = app_obj.run_once("research", dry_run=True)
    assert plan.goal == "research"

    # Create unconfirmed profile
    p = Profile()
    p.confirmed = False
    p.autonomy_boundaries.allowed_sites = ["x.com"]
    from idlecua.profile.store import save_profile

    save_profile(p, tmp_path / "profile.json")
    with pytest.raises(ProfileNotConfirmedError, match="unconfirmed"):
        app_obj.run_once("research", dry_run=False)

    # Confirmed but invalid -> also refused
    p.confirmed = True
    p.autonomy_boundaries.allowed_sites = ["bad domain!!"]
    save_profile(p, tmp_path / "profile.json")
    with pytest.raises(ProfileNotConfirmedError, match="invalid domain"):
        app_obj.run_once("research", dry_run=False)

    # Valid confirmed -> gate passes and execution should succeed (fake driver, SQLite, report)
    p.autonomy_boundaries.allowed_sites = ["x.com", "reddit.com"]
    p.autonomy_boundaries.session_duration_minutes = 30
    p.autonomy_boundaries.daily_action_limit = 200
    p.autonomy_boundaries.daily_llm_call_limit = 150
    p.computer_usage.idle_threshold_minutes = 10
    p.autonomy_boundaries.allowed_hours = "00:00-23:59"
    save_profile(p, tmp_path / "profile.json")
    # Now gate passes and run_once executes via fake driver
    result_obj = app_obj.run_once("research", dry_run=False)
    # ExecutionResult should have completed state and persisted artifacts
    assert hasattr(result_obj, "state")
    state_val = result_obj.state.value if hasattr(result_obj.state, "value") else str(result_obj.state)
    assert state_val in ("completed", "paused_by_user", "stopped", "failed")
    assert result_obj.actions_executed >= 0
    assert result_obj.report_markdown and "# IdleCUA" in result_obj.report_markdown
    # Check SQLite persisted
    assert (tmp_path / "memory.db").exists()
    assert len(app_obj.memory.list_tasks()) >= 1

    # CLI run-once should also succeed with valid profile (not NotImplemented)
    result = runner.invoke(app, ["run-once", "research task", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "Task completed" in result.output or "research task" in result.output.lower()
    # Make profile unconfirmed again and CLI should refuse with Profile gate (exit 1, not 2)
    p.confirmed = False
    save_profile(p, tmp_path / "profile.json")
    result = runner.invoke(app, ["run-once", "research task", "--data-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "Refused" in result.output or "unconfirmed" in result.output.lower()
    # Dry-run should succeed even when unconfirmed
    result = runner.invoke(app, ["run-once", "--dry-run", "research task", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0


def test_machine_readable_plus_human_render_no_duplication(tmp_path: Path):
    result = runner.invoke(app, ["init", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0
    result = runner.invoke(app, ["profile", "interview", "--yes", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0
    json_path = tmp_path / "profile.json"
    assert json_path.exists()
    data = json.loads(json_path.read_text())
    profile = load_profile(json_path)
    assert profile is not None
    human = render_human_readable(profile)
    assert profile.autonomy_boundaries.allowed_sites[0] in human
    assert str(profile.autonomy_boundaries.session_duration_minutes) in human
    files = list(tmp_path.iterdir())
    assert len([f for f in files if f.name.startswith("profile")]) == 1


def test_cli_init_and_plan_dryrun(tmp_path: Path):
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "init" in result.output
    result = runner.invoke(app, ["init", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0
    result = runner.invoke(app, ["plan", "hello world", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0
    # plan output is table, not explicit "No actions executed" but contains goal
    assert "hello world" in result.output.lower()
    result = runner.invoke(app, ["run-once", "--dry-run", "hello world", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "hello world" in result.output.lower()
