"""Focused tests for walking skeleton — maps to issue #2 acceptance criteria."""

import json
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from idlecua import IdleCua, IdleCuaConfig
from idlecua.cli import app as cli_app
from idlecua.contracts import FakeComputerDriver, FakeModelProvider
from idlecua.models import AgentState, Plan, RiskLevel, Task
from idlecua.models.state import is_valid_transition

runner = CliRunner()


# -- CLI: --help lists commands --

def test_cli_help_lists_commands():
    result = runner.invoke(cli_app, ["--help"])
    assert result.exit_code == 0
    text = result.output.lower()
    assert "init" in text
    assert "plan" in text
    assert "run-once" in text


# -- CLI: init creates data dir and default config --

def test_cli_init_creates_data_dir_and_config():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td) / "idlecua-data"
        result = runner.invoke(cli_app, ["init", "--data-dir", str(data_dir)])
        assert result.exit_code == 0, result.output
        assert data_dir.exists()
        assert data_dir.is_dir()
        config_path = data_dir / "config.json"
        assert config_path.exists()
        raw = json.loads(config_path.read_text())
        assert raw["readonly"] is True
        assert "allowlist" in raw
        assert "x.com" in raw["allowlist"]


def test_cli_init_is_idempotent():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td) / "data"
        r1 = runner.invoke(cli_app, ["init", "--data-dir", str(data_dir)])
        assert r1.exit_code == 0
        # mutate config to detect overwrite
        cfg_path = data_dir / "config.json"
        raw = json.loads(cfg_path.read_text())
        raw["max_actions"] = 123
        cfg_path.write_text(json.dumps(raw))
        r2 = runner.invoke(cli_app, ["init", "--data-dir", str(data_dir)])
        assert r2.exit_code == 0
        raw2 = json.loads(cfg_path.read_text())
        # second init must not clobber existing config
        assert raw2["max_actions"] == 123


# -- CLI: plan / run-once --dry-run print typed bounded plan and execute nothing --

def test_cli_plan_prints_bounded_typed_plan():
    result = runner.invoke(cli_app, ["plan", "research recent AI papers on agents", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(_extract_json(result.output))
    assert data["goal"] == "research recent AI papers on agents"
    assert "target" in data
    assert "expected_actions" in data
    assert isinstance(data["expected_actions"], list)
    assert len(data["expected_actions"]) > 0
    assert 1 <= data["max_duration_minutes"] <= 45
    assert 1 <= data["max_actions"] <= 200
    assert data["risk_level"] in ("low", "medium", "high")


def test_cli_run_once_dry_run_prints_same_plan_as_plan():
    task = "research recent AI papers on agents"
    r_plan = runner.invoke(cli_app, ["plan", task, "--json"])
    r_run = runner.invoke(cli_app, ["run-once", "--dry-run", task, "--json"])
    assert r_plan.exit_code == 0
    assert r_run.exit_code == 0
    d_plan = json.loads(_extract_json(r_plan.output))
    d_run = json.loads(_extract_json(r_run.output))
    assert d_plan == d_run


def test_cli_run_once_without_dry_run_is_rejected():
    # Use isolated data dir so test is not affected by ~/.idlecua confirmed profile on dev machine
    with tempfile.TemporaryDirectory() as td:
        result = runner.invoke(cli_app, ["run-once", "do something", "--data-dir", td])
        assert result.exit_code != 0
        # Walking skeleton rejects non-dry-run; with profile gate it may also refuse due to missing/unconfirmed profile.
        low = result.output.lower()
        assert "dry-run" in low or "profile" in low or "refused" in low


def _extract_json(output: str) -> str:
    """CliRunner + rich may emit ANSI; find the JSON object."""
    start = output.find("{")
    end = output.rfind("}")
    assert start != -1 and end != -1, f"no JSON in output: {output!r}"
    return output[start : end + 1]


# -- Public Python API: same plan without CLI --

def test_public_api_dry_run_matches_cli_plan():
    task_desc = "research recent AI papers on agents"
    # via CLI
    cli_result = runner.invoke(cli_app, ["plan", task_desc, "--json"])
    cli_data = json.loads(_extract_json(cli_result.output))
    # via public API
    driver = FakeComputerDriver()
    app = IdleCua(config=IdleCuaConfig(data_dir=Path(tempfile.mkdtemp())), computer=driver)
    plan = app.dry_run(task_desc)
    assert plan.goal == cli_data["goal"]
    assert plan.target == cli_data["target"]
    assert plan.expected_actions == cli_data["expected_actions"]
    assert plan.max_duration_minutes == cli_data["max_duration_minutes"]
    assert plan.max_actions == cli_data["max_actions"]


def test_public_api_create_task_and_dry_run():
    app = IdleCua(config=IdleCuaConfig(data_dir=Path(tempfile.mkdtemp())), computer=FakeComputerDriver())
    task = app.create_task("find github projects about agents")
    assert isinstance(task, Task)
    assert task.description == "find github projects about agents"
    plan = app.dry_run(task.description)
    assert isinstance(plan, Plan)
    assert plan.goal == task.description


# -- Focused test: dry-run makes zero ComputerDriver calls --

def test_dry_run_makes_zero_computer_driver_calls():
    driver = FakeComputerDriver()
    model = FakeModelProvider()
    app = IdleCua(
        config=IdleCuaConfig(data_dir=Path(tempfile.mkdtemp())),
        computer=driver,
        model_provider=model,
    )
    task = app.create_task("research X on reddit")
    assert len(driver.calls) == 0
    plan = app.dry_run(task.description)
    assert isinstance(plan, Plan)
    assert len(driver.calls) == 0, f"dry-run must not call ComputerDriver, got {driver.calls}"
    assert len(model.calls) == 0, f"dry-run must not call ModelProvider, got {model.calls}"
    # also via run_once --dry-run path
    plan2 = app.run_once(task.description, dry_run=True)
    assert len(driver.calls) == 0
    assert len(model.calls) == 0
    assert plan == plan2


def test_dry_run_is_deterministic():
    app = IdleCua(config=IdleCuaConfig(data_dir=Path(tempfile.mkdtemp())), computer=FakeComputerDriver())
    p1 = app.dry_run("hello world")
    p2 = app.dry_run("hello world")
    assert p1 == p2
    p3 = app.dry_run("different task")
    # different input should usually yield different plan (at least goal differs)
    assert p3.goal != p1.goal


# -- Plan is bounded (caps from spec) --

def test_plan_is_bounded():
    app = IdleCua(config=IdleCuaConfig(data_dir=Path(tempfile.mkdtemp())), computer=FakeComputerDriver())
    for desc in ["x", "a" * 200, "research github", "post a comment on x.com"]:
        plan = app.dry_run(desc)
        assert 1 <= plan.max_duration_minutes <= 45
        assert 1 <= plan.max_actions <= 200
        assert plan.risk_level in (RiskLevel.low, RiskLevel.medium, RiskLevel.high)
        assert len(plan.expected_actions) > 0


# -- State transitions validated; illegal rejected --

def test_state_transitions_valid_path():
    t = Task(description="demo")
    assert t.state == AgentState.disabled
    t.transition_to(AgentState.waiting_for_idle)
    t.transition_to(AgentState.planning)
    t.transition_to(AgentState.running)
    t.transition_to(AgentState.completed)
    # completed -> waiting_for_idle is allowed (next cycle)
    t.transition_to(AgentState.waiting_for_idle)


def test_state_illegal_transitions_rejected():
    # disabled -> running is illegal
    t = Task(description="demo")
    with pytest.raises(ValueError, match="Illegal transition"):
        t.transition_to(AgentState.running)
    with pytest.raises(ValueError, match="Illegal transition"):
        t.transition_to(AgentState.completed)

    # waiting_for_idle -> running illegal (must go through planning)
    t2 = Task(description="demo2")
    t2.transition_to(AgentState.waiting_for_idle)
    with pytest.raises(ValueError, match="Illegal transition"):
        t2.transition_to(AgentState.running)

    # running -> disabled illegal (must stop or complete/fail/pause first)
    t3 = Task(description="demo3")
    t3.transition_to(AgentState.waiting_for_idle)
    t3.transition_to(AgentState.planning)
    t3.transition_to(AgentState.running)
    with pytest.raises(ValueError, match="Illegal transition"):
        t3.transition_to(AgentState.disabled)

    # completed -> running illegal
    t4 = Task(description="demo4")
    t4.transition_to(AgentState.waiting_for_idle)
    t4.transition_to(AgentState.planning)
    t4.transition_to(AgentState.running)
    t4.transition_to(AgentState.completed)
    with pytest.raises(ValueError, match="Illegal transition"):
        t4.transition_to(AgentState.running)


def test_is_valid_transition_helper():
    assert is_valid_transition(AgentState.disabled, AgentState.waiting_for_idle)
    assert not is_valid_transition(AgentState.disabled, AgentState.running)
    assert is_valid_transition(AgentState.running, AgentState.paused_by_user)
    assert not is_valid_transition(AgentState.completed, AgentState.running)


def test_task_string_state_coercion():
    t = Task(description="demo", state="disabled")
    assert t.state == AgentState.disabled
    t.transition_to("waiting_for_idle")
    assert t.state == AgentState.waiting_for_idle
    assert t.can_transition_to("planning")
    assert not t.can_transition_to("running")


# -- Config and contracts present --

def test_config_round_trip():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "cfg"
        cfg = IdleCuaConfig(data_dir=p, max_actions=50, max_duration_minutes=20)
        cfg.save()
        loaded = IdleCuaConfig.load(p)
        assert loaded.max_actions == 50
        assert loaded.max_duration_minutes == 20


def test_fake_drivers_exist_and_record():
    driver = FakeComputerDriver()
    driver.click(10, 20)
    driver.type_text("hi")
    assert len(driver.calls) == 2
    driver.reset()
    assert len(driver.calls) == 0

    model = FakeModelProvider(response="hello")
    assert model.complete("prompt") == "hello"
    assert model.calls == ["prompt"]
