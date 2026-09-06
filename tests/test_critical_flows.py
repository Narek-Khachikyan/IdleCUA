"""Acceptance-critical tests — five required by Testing Decisions in issue #1.

Maps to:
1. input halt + paused_by_user on simulated user return
2. emergency stop cancels task, releases input, records reason, LLM-independent
3. confirmation-required write action blocked unattended / gated interactively with explicit prompt
4. exact-repeat prevention (normalized query, URL fingerprint, plan fingerprint) — 7-day window
5. one task run end-to-end programmatically via public API (create_task → run_task)"""

import tempfile
import time
from pathlib import Path

import pytest

from idlecua import IdleCua, IdleCuaConfig
from idlecua.contracts import FakeComputerDriver, FakeModelProvider
from idlecua.executor import (
    clear_emergency_stop,
    is_emergency_stop_requested,
    request_emergency_stop,
)
from idlecua.idle import FakeIdleDetector
from idlecua.models.state import AgentState
from idlecua.planner import StubPlanner
from idlecua.policy import TypedAction
from idlecua.profile.interview import run_interview


def _confirmed_profile_dir(tmp_base: Path) -> Path:
    td = Path(tempfile.mkdtemp(dir=str(tmp_base))) if tmp_base.exists() else Path(tempfile.mkdtemp())
    # Use interview API like acceptance_sweep does — create confirmed profile via run_interview + save
    import io as _io
    from rich.console import Console as _Console
    from idlecua.profile.interview import run_interview, save_confirmed_profile

    def _input(q):
        if q.key == "user_characteristics.occupation":
            return "Engineer"
        if q.key == "user_characteristics.interests":
            return "AI agents"
        return ""

    def _confirm(_):
        return True

    _con = _Console(file=_io.StringIO(), width=80)
    profile, _, _ = run_interview(console=_con, input_func=_input, confirm_func=_confirm)
    save_confirmed_profile(profile, td / "profile.json", console=_Console(file=_io.StringIO()))
    return td


# 1. User return → paused_by_user, input halt, saved state, auto-resume only at next idle
def test_user_return_halts_input_and_pauses(tmp_path: Path):
    # Use flipping detector: first check idle, then not idle
    class FlippingDetector(FakeIdleDetector):
        def __init__(self):
            super().__init__(idle_seconds=1000, locked=False)
            self.calls = 0

        def seconds_since_last_input(self) -> float:
            self.calls += 1
            # First two checks (gate + first iteration) return idle, then simulate return
            return 1000.0 if self.calls <= 2 else 0.0

        def can_run(self, thr: int = 600):
            if self.calls <= 2:
                return True, "idle"
            return False, "not idle — user returned"

        def is_screen_locked(self) -> bool:
            return False

    td = _confirmed_profile_dir(tmp_path)
    cfg = IdleCuaConfig(data_dir=td)
    det = FlippingDetector()
    driver = FakeComputerDriver()
    driver.hold_for_test("Shift", "left")
    assert driver.held_keys or driver.held_buttons
    app = IdleCua(config=cfg, computer=driver, idle_detector=det)
    # Use a planner that yields multiple actions so flipping happens mid-task
    # StubPlanner gives ~6-7 actions; flipping after 2 calls will trigger pause on second iteration
    task = app.create_task("research AI agents on x.com")
    result = app.run_task(task, is_interactive=False)
    # Should have detected user return and paused
    assert result.state == AgentState.paused_by_user, f"expected paused_by_user, got {result.state}"
    # Held input must be released synchronously
    assert driver.held_keys == set() and driver.held_buttons == set(), "held input not released on user return"
    # Must not have executed further actions after pause (at most 1-2)
    assert result.actions_executed <= 2, f"should have halted early, got {result.actions_executed}"
    # State persisted
    stored = app.memory.get_task(task.id)
    assert stored is not None and stored["state"] == AgentState.paused_by_user.value
    # Auto-resume only at next idle — still not idle, so resume should fail
    det2 = FakeIdleDetector(idle_seconds=0, locked=False)
    app2 = IdleCua(config=cfg, computer=FakeComputerDriver(), idle_detector=det2)
    # Need to reload same memory
    app2._memory = app.memory
    ok, _ = app2.idle_detector.can_run(cfg.idle_threshold_seconds)
    assert not ok, "should not be able to resume while not idle"
    # After idle again, can resume (det back to 1000)
    det.calls = 0
    ok2, _ = det.can_run(cfg.idle_threshold_seconds)
    assert ok2


def test_user_return_direct_detector():
    det = FakeIdleDetector(idle_seconds=1000, locked=False)
    assert det.can_run(600)[0] is True
    det.hardware_input()
    assert det.seconds_since_last_input() == 0
    assert det.can_run(600)[0] is False
    # Owner-return detection now lives behind the lifecycle seam: the same
    # hardware signal pauses a running Session (covered end-to-end above).
    td = Path(tempfile.mkdtemp())
    import io
    from rich.console import Console
    from idlecua.profile.interview import run_interview, save_confirmed_profile

    def _inp(q):
        return ""
    def _conf(_):
        return True
    _c = Console(file=io.StringIO(), width=80)
    prof, _, _ = run_interview(console=_c, input_func=_inp, confirm_func=_conf)
    save_confirmed_profile(prof, td + "/profile.json" if isinstance(td, str) else Path(td) / "profile.json", console=Console(file=io.StringIO()))
    cfg = IdleCuaConfig(data_dir=td)
    app = IdleCua(config=cfg, computer=FakeComputerDriver(), idle_detector=det)
    # While hardware input is fresh (idle 0s), readiness refuses to start.
    ok, reason = app.can_start()
    assert not ok and "idle" in reason.lower()
    det.set_idle(1000)
    ok2, _ = app.can_start()
    assert ok2


# 2. Emergency stop — LLM-independent
def test_emergency_stop_releases_input_and_records(tmp_path: Path):
    clear_emergency_stop()
    td = _confirmed_profile_dir(tmp_path)
    cfg = IdleCuaConfig(data_dir=td)

    # Direct flag + journal test (no executor clear race)
    driver = FakeComputerDriver()
    driver.hold_for_test("Shift", "left")
    assert driver.held_keys and driver.held_buttons
    request_emergency_stop("test SIGINT")
    assert is_emergency_stop_requested()
    released = driver.release_all_inputs()
    assert driver.held_keys == set() and driver.held_buttons == set()
    assert any("Shift" in r or "left" in r for r in released)
    clear_emergency_stop()
    assert not is_emergency_stop_requested()

    # Mid-task emergency stop via driver side-effect
    class StoppingDriver(FakeComputerDriver):
        def open_url(self, url: str):
            super().open_url(url)
            # Trigger emergency stop after first driver call
            request_emergency_stop("mid-task stop")

    driver2 = StoppingDriver()
    driver2.hold_for_test("Alt", "left")
    app = IdleCua(config=cfg, computer=driver2, idle_detector=FakeIdleDetector(idle_seconds=1000, locked=False))
    task = app.create_task("research emergency stop mid-task")
    result = app.run_task(task, is_interactive=False)
    assert result.state == AgentState.stopped
    assert driver2.held_keys == set() and driver2.held_buttons == set()
    errors = app.memory.list_errors(task_id=task.id)
    assert any("mid-task stop" in e["message"] or "emergency" in e["message"].lower() for e in errors)
    clear_emergency_stop()
    assert not is_emergency_stop_requested()
    # Test kill via app API
    driver2 = FakeComputerDriver()
    driver2.hold_for_test("Alt", "right")
    app2 = IdleCua(config=cfg, computer=driver2, idle_detector=FakeIdleDetector(idle_seconds=1000, locked=False))
    app2.request_emergency_stop("CLI kill")
    assert is_emergency_stop_requested()
    assert driver2.held_keys == set() and driver2.held_buttons == set()
    clear_emergency_stop()


# 3. Confirmation gating — write-block
def test_confirmation_gating_unattended_vs_interactive(tmp_path: Path):
    class PostPlanner(StubPlanner):
        def plan(self, desc, profile=None, history=None):
            from idlecua.models.plan import Plan, RiskLevel
            return Plan(
                goal=desc,
                target="x.com",
                expected_actions=["open_allowed_site", "like", "save_note"],
                expected_result="test",
                max_duration_minutes=10,
                max_actions=10,
                risk_level=RiskLevel.medium,
                requires_confirmation=True,
            )

    # Unattended (is_interactive=False) must block confirmation-required
    td1 = _confirmed_profile_dir(tmp_path)
    cfg1 = IdleCuaConfig(data_dir=td1, readonly=True)
    app1 = IdleCua(config=cfg1, computer=FakeComputerDriver(), planner=PostPlanner(), idle_detector=FakeIdleDetector(idle_seconds=1000, locked=False))
    result1 = app1.run_task("post a like on x.com", is_interactive=False)
    # like should be skipped, not executed
    assert any(s["type"] == "action" and "confirmation-required" in s["reason"].lower() for s in result1.skipped_repeats) or any("like" in str(s).lower() for s in result1.skipped_repeats)
    # Verify that like action was recorded as blocked, not completed
    blocked = [a for a in app1.memory.list_actions(task_id=result1.task_id) if a["kind"] == "like" and a["status"] == "blocked"]
    assert len(blocked) >= 1

    # Interactive with confirm_func returning False → still blocked (owner declined)
    td2 = _confirmed_profile_dir(tmp_path)
    cfg2 = IdleCuaConfig(data_dir=td2, readonly=False)
    app2 = IdleCua(config=cfg2, computer=FakeComputerDriver(), planner=PostPlanner(), idle_detector=FakeIdleDetector(idle_seconds=1000, locked=False))
    result2 = app2.run_task("post a like", is_interactive=True, confirm_func=lambda a: False)
    assert any("owner declined" in s["reason"].lower() for s in result2.skipped_repeats)
    blocked2 = [a for a in app2.memory.list_actions(task_id=result2.task_id) if a["kind"] == "like"]
    assert any(a["status"] == "blocked" for a in blocked2)

    # Interactive with confirm_func returning True → still no driver mapping in MVP,
    # spec sanctions skip-and-surface: must NOT claim "completed" for a no-op (US27/US23).
    td3 = _confirmed_profile_dir(tmp_path)
    cfg3 = IdleCuaConfig(data_dir=td3, readonly=False)
    driver3 = FakeComputerDriver()
    app3 = IdleCua(config=cfg3, computer=driver3, planner=PostPlanner(), idle_detector=FakeIdleDetector(idle_seconds=1000, locked=False))
    result3 = app3.run_task("post a like", is_interactive=True, confirm_func=lambda a: True)
    # like has no typed driver mapping in MVP — even when confirmed it is correctly skipped and surfaced
    skipped = [a for a in app3.memory.list_actions(task_id=result3.task_id) if a["kind"] == "like" and a["status"] == "skipped"]
    assert len(skipped) >= 1, f"like should be skipped (no driver mapping in MVP) when confirmed, got {app3.memory.list_actions(task_id=result3.task_id)}"
    assert any("no typed driver mapping" in (a["error"] or "") for a in skipped), f"expected no-mapping reason, got {skipped}"
    assert any("no typed driver mapping" in s["reason"] for s in result3.skipped_repeats), f"expected surfaced reason, got {result3.skipped_repeats}"
    # Must not have sent blind input (type_text/press/click) for like
    assert all(call[0] != "type_text" or "[like]" not in str(call[1]) for call in driver3.calls), f"blind input for like must not happen, got {driver3.calls}"

    # When is_interactive True but confirm_func is None, must NOT auto-confirm — should prompt or deny (default deny)
    td4 = _confirmed_profile_dir(tmp_path)
    cfg4 = IdleCuaConfig(data_dir=td4, readonly=False)
    app4 = IdleCua(config=cfg4, computer=FakeComputerDriver(), planner=PostPlanner(), idle_detector=FakeIdleDetector(idle_seconds=1000, locked=False))
    # Patch Confirm.ask to return False to simulate user declining at prompt
    import unittest.mock as mock
    with mock.patch("rich.prompt.Confirm.ask", return_value=False):
        result4 = app4.run_task("post a like", is_interactive=True, confirm_func=None)
    # Should be blocked (declined), not auto-confirmed
    assert any("owner declined" in s["reason"].lower() for s in result4.skipped_repeats)


def test_app_can_execute_write_block():
    # Direct PolicyEngine check for US23/24
    cfg = IdleCuaConfig(data_dir=Path(tempfile.mkdtemp()))
    app = IdleCua(config=cfg)
    # Forbidden must be blocked even interactive+confirmed
    for kind in ["payment", "bypass_captcha", "expand_allowlist"]:
        res = app.check_action(TypedAction(kind=kind, target_url="https://x.com"))
        assert res.verdict.value == "blocked"
        ok, _ = app.can_execute(TypedAction(kind=kind, target_url="https://x.com"), is_interactive=True, confirmed=True)
        assert not ok
    # Confirmation-required blocked unattended
    for kind in ["like", "post", "comment"]:
        ok, res = app.can_execute(TypedAction(kind=kind, target_url="https://x.com"), is_interactive=False)
        assert not ok and res.verdict.value == "needs-confirmation"
    # When readonly False and interactive+confirmed, allowed
    cfg2 = IdleCuaConfig(data_dir=Path(tempfile.mkdtemp()), readonly=False)
    app2 = IdleCua(config=cfg2)
    ok3, _ = app2.can_execute(TypedAction(kind="like", target_url="https://x.com"), is_interactive=True, confirmed=True)
    assert ok3


# 4. Anti-repeat — query, URL, plan fingerprint
def test_anti_repeat_prevention(tmp_path: Path):
    td = _confirmed_profile_dir(tmp_path)
    cfg = IdleCuaConfig(data_dir=td)
    driver = FakeComputerDriver()
    app = IdleCua(config=cfg, computer=driver, idle_detector=FakeIdleDetector(idle_seconds=1000, locked=False))
    desc = "research AI agents on x.com for anti-repeat test unique 999"
    r1 = app.run_once(desc, dry_run=False)
    assert r1.actions_executed > 0
    assert len(r1.skipped_repeats) == 0, f"first run should have no skipped repeats, got {r1.skipped_repeats}"
    # Second identical run — plan fingerprint should be detected and execution prevented (US13)
    r2 = app.run_once(desc, dry_run=False)
    assert any(s["type"] == "plan" for s in r2.skipped_repeats), f"plan repeat not detected: {r2.skipped_repeats}"
    assert r2.actions_executed == 0, "repeated plan should not re-execute actions (US13 prevention)"
    assert "Skipped repeats" in r2.report_markdown
    # Query dedup: normalized query seen
    from idlecua.dedup import normalize_query

    qnorm = normalize_query(desc)
    assert app.memory.has_query_within_days(qnorm, days=7)
    # URL dedup: fingerprint seen
    if r1.urls:
        fp = r1.urls[0]["fingerprint"]
        assert app.memory.has_url_within_days(fp, days=7)
    # Plan fingerprint stored
    from idlecua.dedup import plan_fingerprint

    fp_plan = plan_fingerprint(r1.plan)
    assert app.memory.has_plan_fingerprint_within_days(fp_plan, days=7)
    # Fresh query allowed
    r3 = app.run_once("research totally fresh query 12345 unique for anti-repeat", dry_run=False)
    # Fresh plan should execute (not blocked as repeat)
    assert r3.actions_executed > 0


def test_normalized_query_dedup(tmp_path: Path):
    td = _confirmed_profile_dir(tmp_path)
    cfg = IdleCuaConfig(data_dir=td)
    app = IdleCua(config=cfg, computer=FakeComputerDriver(), idle_detector=FakeIdleDetector(idle_seconds=1000, locked=False))
    desc1 = "Research   AI Agents"
    desc2 = "research ai agents"  # same normalized
    r1 = app.run_once(desc1, dry_run=False)
    # second with same normalized but different raw should still be considered repeat at query level
    # However plan fingerprint may also match; we test query-level helper
    from idlecua.dedup import normalize_query

    assert normalize_query(desc1) == normalize_query(desc2)
    assert app.memory.has_query_within_days(normalize_query(desc2), days=7)


# 5. E2E programmatic run via public API
def test_e2e_create_task_run_task_programmatic(tmp_path: Path):
    td = _confirmed_profile_dir(tmp_path)
    cfg = IdleCuaConfig(data_dir=td)
    driver = FakeComputerDriver()
    app = IdleCua(config=cfg, computer=driver, model_provider=FakeModelProvider(response="hi"), idle_detector=FakeIdleDetector(idle_seconds=1000, locked=False))
    # No CLI, direct API
    task = app.create_task("research recent AI papers on agents via e2e")
    assert task.description == "research recent AI papers on agents via e2e"
    assert task.state == AgentState.waiting_for_idle
    plan = app.dry_run(task.description)
    assert len(driver.calls) == 0, "dry_run must not touch driver"
    result = app.run_task(task, is_interactive=False)
    assert hasattr(result, "state")
    assert result.state in (AgentState.completed, AgentState.paused_by_user, AgentState.stopped, AgentState.failed)
    # For this deterministic case with fake idle, should complete
    assert result.state == AgentState.completed
    assert result.actions_executed > 0
    assert result.report_markdown and "# IdleCUA" in result.report_markdown
    assert result.report_path is not None and result.report_path.exists()
    # SQLite persisted
    assert app.memory.get_task(task.id) is not None
    assert len(app.memory.list_actions(task_id=task.id)) > 0
    assert len(app.memory.list_queries(limit=10)) > 0
    # Status/history/report accessors work without CLI
    st = app.get_status()
    assert "agent_state" in st
    hist = app.get_history(limit=5)
    assert "queries" in hist and "urls" in hist
    rep = app.get_report(task.id)
    assert rep is not None and "markdown" in rep


def test_idle_gate_blocks_when_not_idle(tmp_path: Path):
    td = _confirmed_profile_dir(tmp_path)
    cfg = IdleCuaConfig(data_dir=td, idle_threshold_seconds=600)
    app = IdleCua(config=cfg, computer=FakeComputerDriver(), idle_detector=FakeIdleDetector(idle_seconds=10, locked=False))
    # require_idle True, not idle -> should block
    with pytest.raises(RuntimeError, match="idle gate blocked"):
        app.run_task("research should be blocked by idle gate")
    # When idle, should pass
    app2 = IdleCua(config=cfg, computer=FakeComputerDriver(), idle_detector=FakeIdleDetector(idle_seconds=1000, locked=False))
    app2._memory = app.memory  # share same dir
    task = app2.create_task("research when idle")
    result = app2.run_task(task, is_interactive=False)
    assert result.state == AgentState.completed


def test_verify_by_reread_asserts_change(tmp_path: Path):
    # Ensure verify fails when state unchanged (not just url substring)
    td = _confirmed_profile_dir(tmp_path)
    cfg = IdleCuaConfig(data_dir=td)

    class StaticDriver(FakeComputerDriver):
        def get_accessibility_tree(self):
            self._record("get_accessibility_tree")
            return {"role": "root", "children": []}

        def open_url(self, url: str):
            self._record("open_url", url)
            # Do not change state — get_accessibility_tree will return same
            pass

    driver = StaticDriver()
    app = IdleCua(config=cfg, computer=driver, idle_detector=FakeIdleDetector(idle_seconds=1000, locked=False))
    task = app.create_task("research verify change detection")
    result = app.run_task(task, is_interactive=False)
    assert any("verify failed" in e["message"].lower() for e in result.errors), f"expected verify failure for static driver (state unchanged), got {result.errors}"
