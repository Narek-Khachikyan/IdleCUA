from pathlib import Path
import json
import asyncio

import pytest
from typer.testing import CliRunner
from rich.console import Console

from idle_cua.cli import app
from idle_cua.config import IdleCuaConfig
from idle_cua.profile.interview import run_interview
from idle_cua.profile.store import load_profile
from idle_cua.profile.validate import validate_profile
from idle_cua.profile.render import render_human_readable
from idle_cua.profile.permissions import permissions_report_text, check_permissions
from idle_cua.application import Application, ProfileNotConfirmedError
from idle_cua.profile.models import Profile

runner = CliRunner()


def test_interview_saves_only_after_confirmation(tmp_path: Path):
    cfg = IdleCuaConfig(data_dir=tmp_path)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)

    # Simulate interview with confirm=False -> should not save
    def input_func(q):
        return "test" if q.key == "user_characteristics.occupation" else ""

    def confirm_no(_):
        return False

    console = Console(record=True)
    from idle_cua.profile.interview import save_confirmed_profile

    profile, facts, assumptions = run_interview(console=console, input_func=input_func, confirm_func=confirm_no)
    assert profile.confirmed is False
    assert "user_characteristics.occupation" in facts
    assert len(assumptions) > 0
    saved = save_confirmed_profile(profile, cfg.profile_path, console=console)
    assert saved is False
    assert not cfg.profile_path.exists()

    # Now confirm=True -> should save
    def confirm_yes(_):
        return True

    profile2, facts2, assumptions2 = run_interview(console=console, input_func=input_func, confirm_func=confirm_yes)
    assert profile2.confirmed is True
    saved2 = save_confirmed_profile(profile2, cfg.profile_path, console=console)
    assert saved2 is True
    assert cfg.profile_path.exists()
    loaded = load_profile(cfg.profile_path)
    assert loaded is not None
    assert loaded.confirmed is True


def test_summary_separates_facts_and_assumptions(tmp_path: Path):
    # Provide one explicit fact, rest defaults
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
    # A defaulted field like material_depth should be in assumptions
    assert "user_characteristics.material_depth" in assumptions
    assert assumptions["user_characteristics.material_depth"] == "mixed"
    # computer_usage.monitors default "1" should be assumption when we return ""
    assert "computer_usage.monitors" in assumptions


def test_profile_show_edit_validate_cli(tmp_path: Path):
    # init
    result = runner.invoke(app, ["--data-dir", str(tmp_path), "init"])
    assert result.exit_code == 0
    assert (tmp_path / "config.json").exists()

    # interview --yes
    result = runner.invoke(app, ["--data-dir", str(tmp_path), "profile", "interview", "--yes"])
    assert result.exit_code == 0
    assert (tmp_path / "profile.json").exists()
    # show human-readable
    result = runner.invoke(app, ["--data-dir", str(tmp_path), "profile", "show"])
    assert result.exit_code == 0
    assert "IdleCUA Profile" in result.output
    assert "Confirmed: yes" in result.output
    # show --json
    result = runner.invoke(app, ["--data-dir", str(tmp_path), "profile", "show", "--json"])
    assert result.exit_code == 0
    assert '"confirmed": true' in result.output.lower()

    # validate should pass for default profile
    result = runner.invoke(app, ["--data-dir", str(tmp_path), "profile", "validate"])
    assert result.exit_code == 0
    assert "Profile is valid" in result.output

    # edit via --field with invalid allowlist should cause validate to fail
    result = runner.invoke(app, ["--data-dir", str(tmp_path), "profile", "edit", "--field", "autonomy_boundaries.allowed_sites=bad domain!!"])
    assert result.exit_code == 0
    result = runner.invoke(app, ["--data-dir", str(tmp_path), "profile", "validate"])
    assert result.exit_code == 1
    assert "allowlist" in result.output.lower() and "invalid domain" in result.output.lower()

    # fix allowlist
    result = runner.invoke(app, ["--data-dir", str(tmp_path), "profile", "edit", "--field", "autonomy_boundaries.allowed_sites=x.com, reddit.com"])
    assert result.exit_code == 0
    result = runner.invoke(app, ["--data-dir", str(tmp_path), "profile", "validate"])
    assert result.exit_code == 0

    # invalid limits
    result = runner.invoke(app, ["--data-dir", str(tmp_path), "profile", "edit", "--field", "autonomy_boundaries.session_duration_minutes=999"])
    assert result.exit_code == 0
    result = runner.invoke(app, ["--data-dir", str(tmp_path), "profile", "validate"])
    assert result.exit_code == 1
    assert "session_duration_minutes" in result.output

    # restore
    result = runner.invoke(app, ["--data-dir", str(tmp_path), "profile", "edit", "--field", "autonomy_boundaries.session_duration_minutes=30"])
    assert result.exit_code == 0
    result = runner.invoke(app, ["--data-dir", str(tmp_path), "profile", "validate"])
    assert result.exit_code == 0

    # invalid schedule
    result = runner.invoke(app, ["--data-dir", str(tmp_path), "profile", "edit", "--field", "autonomy_boundaries.allowed_hours=bad"])
    assert result.exit_code == 0
    result = runner.invoke(app, ["--data-dir", str(tmp_path), "profile", "validate"])
    assert result.exit_code == 1
    assert "allowed_hours" in result.output


def test_permission_check_reports_remediation(tmp_path: Path):
    result = runner.invoke(app, ["--data-dir", str(tmp_path), "profile", "check-permissions"])
    # Should output Accessibility and Screen Recording with remediation steps
    assert "Accessibility" in result.output
    assert "Screen Recording" in result.output
    assert "Remediation" in result.output or "remediation" in result.output.lower()
    # Also test direct function
    text = permissions_report_text(check_permissions())
    assert "Accessibility" in text
    assert "Screen Recording" in text


def test_hard_gate_refuses_autonomous_runs_while_unconfirmed(tmp_path: Path):
    cfg = IdleCuaConfig(data_dir=tmp_path)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    # No profile -> gate should refuse
    app_obj = Application(config=cfg)
    with pytest.raises(ProfileNotConfirmedError, match="No profile found"):
        asyncio.run(app_obj.run_task("research", dry_run=False))
    # Dry-run should still be allowed (no gate)
    plan = asyncio.run(app_obj.run_task("research", dry_run=True))
    assert plan.task == "research"

    # Create unconfirmed profile
    p = Profile()
    p.confirmed = False
    p.autonomy_boundaries.allowed_sites = ["x.com"]
    from idle_cua.profile.store import save_profile

    save_profile(p, cfg.profile_path)
    with pytest.raises(ProfileNotConfirmedError, match="unconfirmed"):
        asyncio.run(app_obj.run_task("research", dry_run=False))

    # Confirmed but invalid -> also refused
    p.confirmed = True
    p.autonomy_boundaries.allowed_sites = ["bad domain!!"]
    save_profile(p, cfg.profile_path)
    with pytest.raises(ProfileNotConfirmedError, match="invalid domain"):
        asyncio.run(app_obj.run_task("research", dry_run=False))

    # Valid confirmed -> allowed
    p.autonomy_boundaries.allowed_sites = ["x.com", "reddit.com"]
    p.autonomy_boundaries.session_duration_minutes = 30
    p.autonomy_boundaries.daily_action_limit = 200
    p.autonomy_boundaries.daily_llm_call_limit = 150
    p.computer_usage.idle_threshold_minutes = 10
    p.autonomy_boundaries.allowed_hours = "00:00-23:59"
    save_profile(p, cfg.profile_path)
    plan = asyncio.run(app_obj.run_task("research", dry_run=False))
    assert plan.task == "research"
    assert app_obj.state_machine.state == "completed"

    # CLI run-once should also gate
    result = runner.invoke(app, ["--data-dir", str(tmp_path), "run-once", "research task"])
    # Currently profile is valid confirmed, so should succeed
    assert result.exit_code == 0
    # Make profile unconfirmed again and CLI should refuse
    p.confirmed = False
    save_profile(p, cfg.profile_path)
    result = runner.invoke(app, ["--data-dir", str(tmp_path), "run-once", "research task"])
    assert result.exit_code == 1
    assert "Refused" in result.output or "unconfirmed" in result.output.lower()
    # Dry-run should succeed even when unconfirmed
    result = runner.invoke(app, ["--data-dir", str(tmp_path), "run-once", "--dry-run", "research task"])
    assert result.exit_code == 0


def test_machine_readable_plus_human_render_no_duplication(tmp_path: Path):
    # Ensure human-readable is derived from same file, not duplicated
    result = runner.invoke(app, ["--data-dir", str(tmp_path), "init"])
    assert result.exit_code == 0
    result = runner.invoke(app, ["--data-dir", str(tmp_path), "profile", "interview", "--yes"])
    assert result.exit_code == 0
    json_path = tmp_path / "profile.json"
    assert json_path.exists()
    data = json.loads(json_path.read_text())
    # Human rendering should reflect same data
    profile = load_profile(json_path)
    assert profile is not None
    human = render_human_readable(profile)
    # Check some fields appear in both
    assert profile.autonomy_boundaries.allowed_sites[0] in human
    assert str(profile.autonomy_boundaries.session_duration_minutes) in human
    # Ensure no second file duplicates profile (only profile.json + config.json + db)
    files = list(tmp_path.iterdir())
    assert len([f for f in files if f.name.startswith("profile")]) == 1  # only profile.json


def test_cli_init_and_plan_dryrun(tmp_path: Path):
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "init" in result.output
    result = runner.invoke(app, ["--data-dir", str(tmp_path), "init"])
    assert result.exit_code == 0
    result = runner.invoke(app, ["--data-dir", str(tmp_path), "plan", "hello world"])
    assert result.exit_code == 0
    assert "Goal:" in result.output
    assert "Max duration" in result.output
    assert "No actions executed" in result.output
    result = runner.invoke(app, ["--data-dir", str(tmp_path), "run-once", "--dry-run", "hello world"])
    assert result.exit_code == 0
    assert "Dry-run" in result.output
