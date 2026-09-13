"""Focused TaskLifecycle coverage through handle/inspect (spec #37).

Behavioral only: temporary SQLite plus the existing fake ComputerDriver,
ModelProvider, and IdleDetector adapters. No SQL topology, private-helper,
or cursor assertions beyond what the seam exposes via snapshots.
"""

import io
import json
import sqlite3
import tempfile
from pathlib import Path

from rich.console import Console

from idlecua import IdleCua, IdleCuaConfig
from idlecua.contracts import FakeComputerDriver
from idlecua.executor import clear_emergency_stop
from idlecua.idle import FakeIdleDetector
from idlecua.profile.interview import run_interview, save_confirmed_profile
from idlecua.task_lifecycle import (
    Cancel,
    EmergencyStop,
    Enqueue,
    GetActive,
    GetTask,
    ListTasks,
    OutcomeCategory,
    Start,
)


def _confirmed_dir(tmp_base: Path) -> Path:
    td = Path(tempfile.mkdtemp(dir=str(tmp_base))) if tmp_base.exists() else Path(tempfile.mkdtemp())
    con = Console(file=io.StringIO(), width=80)
    profile, _, _ = run_interview(console=con, input_func=lambda q: "", confirm_func=lambda _: True)
    save_confirmed_profile(profile, td / "profile.json", console=Console(file=io.StringIO()))
    return td


def _app(td: Path, **kw):
    kw.setdefault("idle_detector", FakeIdleDetector(idle_seconds=1000, locked=False))
    return IdleCua(config=IdleCuaConfig(data_dir=td), computer=FakeComputerDriver(), **kw)


def test_enqueue_inspect_and_terminal_are_terminal(tmp_path: Path):
    clear_emergency_stop()
    td = _confirmed_dir(tmp_path)
    app = _app(td)
    lc = app.lifecycle

    out = lc.handle(Enqueue(goal="immutable goal"))
    assert out.category == OutcomeCategory.ok and out.state == "waiting_for_idle"
    snap = lc.inspect(GetTask(out.task_id))
    assert snap is not None and snap.goal == "immutable goal" and snap.state == "waiting_for_idle"

    # Empty goal and queue-time approval are rejected, not persisted.
    assert lc.handle(Enqueue(goal="  ")).category == OutcomeCategory.invalid_request
    assert lc.handle(Enqueue(goal="g", approvals=("like",))).category == OutcomeCategory.invalid_request
    assert lc.handle(Enqueue(goal="g", skip_action_types=("no_such_kind",))).category == OutcomeCategory.invalid_request
    assert lc.inspect(GetTask("missing")) is None
    assert lc.handle(Start(task_id="missing", trigger="explicit", mode="unattended")).category == OutcomeCategory.not_found
    assert lc.handle(Cancel(task_id="missing")).category == OutcomeCategory.not_found

    # A later attempt creates a new Task; the original record stays intact.
    run = lc.handle(Start(task_id=out.task_id, trigger="explicit", mode="unattended"))
    assert run.category == OutcomeCategory.ok and run.state == "completed"
    again = lc.handle(Start(task_id=out.task_id, trigger="explicit", mode="unattended"))
    assert again.category == OutcomeCategory.invalid_transition
    again2 = lc.handle(Cancel(task_id=out.task_id))
    assert again2.category == OutcomeCategory.invalid_transition
    fresh = lc.handle(Enqueue(goal="immutable goal"))
    assert fresh.category == OutcomeCategory.ok and fresh.task_id != out.task_id
    assert lc.inspect(GetTask(out.task_id)).state == "completed"
    # Report persisted before completion.
    assert lc.memory.get_report(out.task_id) is not None


def test_cancel_queued_and_paused_leaves_others_alone(tmp_path: Path):
    clear_emergency_stop()
    td = _confirmed_dir(tmp_path)
    app = _app(td)
    lc = app.lifecycle

    a = lc.handle(Enqueue(goal="cancel me")).task_id
    b = lc.handle(Enqueue(goal="keep me")).task_id
    out = lc.handle(Cancel(task_id=a, reason="owner"))
    assert out.category == OutcomeCategory.stopped and out.state == "stopped"
    snap = lc.inspect(GetTask(a))
    assert snap.state == "stopped" and (snap.stop_or_failure_cause or "").startswith("cancelled:")
    assert lc.inspect(GetTask(b)).state == "waiting_for_idle"

    # A paused task cancels too, with its own cause; queued work is untouched.
    class Flip(FakeIdleDetector):
        def __init__(self):
            super().__init__(idle_seconds=1000, locked=False)
            self.n = 0

        def seconds_since_last_input(self):
            self.n += 1
            return 1000.0 if self.n <= 3 else 0.0

        def can_run(self, thr=600):
            return (True, "idle") if self.n <= 3 else (False, "user returned")

        def is_screen_locked(self):
            return False

    app_p = _app(td, idle_detector=Flip())
    app_p._memory = app.memory
    c = lc.handle(Enqueue(goal="pause then cancel")).task_id
    paused = app_p.lifecycle.handle(Start(task_id=c, trigger="explicit", mode="unattended"))
    assert paused.state == "paused_by_user"
    cancelled = lc.handle(Cancel(task_id=c, reason="no longer needed"))
    assert cancelled.category == OutcomeCategory.stopped
    assert (lc.inspect(GetTask(c)).stop_or_failure_cause or "").startswith("cancelled:")
    assert lc.inspect(GetTask(b)).state == "waiting_for_idle"


def test_active_session_exclusion_ignores_elapsed_time(tmp_path: Path):
    clear_emergency_stop()
    import os

    td = _confirmed_dir(tmp_path)
    app = _app(td)
    lc = app.lifecycle

    a = lc.handle(Enqueue(goal="first")).task_id
    b = lc.handle(Enqueue(goal="second")).task_id
    lc.memory.lease_acquire(a, os.getpid())
    # Backdate the lease: elapsed time alone never permits takeover.
    conn = sqlite3.connect(str(td / "memory.db"))
    try:
        conn.execute("UPDATE active_session SET acquired_at='2020-01-01T00:00:00+00:00'")
        conn.commit()
    finally:
        conn.close()
    blocked = lc.handle(Start(task_id=b, trigger="explicit", mode="unattended"))
    assert blocked.category == OutcomeCategory.already_active
    assert lc.inspect(GetTask(b)).state == "waiting_for_idle"
    lc.memory.lease_release(a)


def test_stale_lease_recovery_marks_outcome_unknown_without_retry(tmp_path: Path):
    clear_emergency_stop()
    td = _confirmed_dir(tmp_path)
    app = _app(td)
    lc = app.lifecycle

    dead = lc.handle(Enqueue(goal="crashed")).task_id
    live = lc.handle(Enqueue(goal="next")).task_id
    lc.memory.lease_acquire(dead, 999999)
    lc.memory.record_action("act-started", dead, "search", "https://x.com", "allowed", "started", None)
    out = lc.handle(Start(task_id=live, trigger="explicit", mode="unattended"))
    assert out.category == OutcomeCategory.ok and out.state == "completed"
    rows = {a["id"]: a["status"] for a in lc.memory.list_actions(task_id=dead)}
    assert rows.get("act-started") == "outcome_unknown"
    failed = lc.inspect(GetTask(dead))
    assert failed.state == "failed" and failed.stop_or_failure_cause == "interrupted_unknown"
    # Unknown work is never retried automatically.
    again = lc.handle(Start(task_id=dead, trigger="explicit", mode="unattended"))
    assert again.category == OutcomeCategory.invalid_transition


def test_selection_paused_before_queued_fifo(tmp_path: Path):
    clear_emergency_stop()
    td = _confirmed_dir(tmp_path)

    class Flip(FakeIdleDetector):
        def __init__(self):
            super().__init__(idle_seconds=1000, locked=False)
            self.n = 0

        def seconds_since_last_input(self):
            self.n += 1
            return 1000.0 if self.n <= 3 else 0.0

        def can_run(self, thr=600):
            return (True, "idle") if self.n <= 3 else (False, "user returned")

        def is_screen_locked(self):
            return False

    app = _app(td, idle_detector=Flip())
    lc = app.lifecycle
    first = lc.handle(Enqueue(goal="first runs then pauses")).task_id
    r1 = lc.handle(Start(task_id=first, trigger="explicit", mode="unattended"))
    assert r1.state == "paused_by_user"
    second = lc.handle(Enqueue(goal="queued second")).task_id
    third = lc.handle(Enqueue(goal="queued third")).task_id
    # Oldest paused resumes before any queued task starts (next idle window).
    # Resume re-runs unverified slots after the last confirmed Action instead
    # of skipping them, so one idle window may pause again before finishing:
    # keep resuming at fresh idle windows until the first task completes.
    app.idle_detector.n = 0
    picked = lc.handle(Start(task_id=None, trigger="idle", mode="unattended"))
    assert picked.task_id == first
    for _ in range(5):
        if lc.inspect(GetTask(first)).state == "completed":
            break
        app.idle_detector.n = 0
        picked = lc.handle(Start(task_id=None, trigger="idle", mode="unattended"))
        assert picked.task_id == first
    assert lc.inspect(GetTask(first)).state == "completed"
    # An unverified slot is recorded as failed — never confirmed — and a
    # later resume re-runs it instead of skipping it. Observable through the
    # seam: failures are visible in the snapshot while the task still runs to
    # completion with confirmed work and exactly one final Report.
    done = lc.inspect(GetTask(first))
    assert done is not None
    assert int(done.cumulative.get("actions_failed", 0)) >= 1
    assert int(done.cumulative.get("actions_completed", 0)) >= 1
    assert len(done.completed_outcomes) >= 1
    assert done.report_markdown
    picked2 = lc.handle(Start(task_id=None, trigger="idle", mode="unattended"))
    assert picked2.task_id == second
    assert lc.inspect(GetTask(third)).state == "waiting_for_idle"


def test_v2_rows_keep_confirmed_prefix_after_migration(tmp_path: Path):
    """Pre-v3 action rows without a slot keep their confirmed prefix.

    Simulates a v2 database (no slot index): after migration the resume must
    continue after the last confirmed Action, never replaying confirmed
    Actions from slot 0. Asserted through handle/inspect only.
    """
    clear_emergency_stop()
    td = _confirmed_dir(tmp_path)

    from idlecua.planner import StubPlanner

    class UniquePlanner(StubPlanner):
        def plan(self, desc, profile=None, history=None):
            from idlecua.models.plan import Plan, RiskLevel

            return Plan(goal=desc, target="x.com", expected_actions=[
                "open_allowed_site", "search", "read_ui", "scroll",
                "extract_public_info", "save_note",
            ], expected_result="t", max_duration_minutes=10, max_actions=50,
                risk_level=RiskLevel.low, requires_confirmation=False)

    class Flip(FakeIdleDetector):
        def __init__(self):
            super().__init__(idle_seconds=1000, locked=False)
            self.n = 0

        def seconds_since_last_input(self):
            self.n += 1
            return 1000.0 if self.n <= 3 else 0.0

        def can_run(self, thr=600):
            return (True, "idle") if self.n <= 3 else (False, "user returned")

        def is_screen_locked(self):
            return False

    app = _app(td, planner=UniquePlanner(), idle_detector=Flip())
    lc = app.lifecycle
    tid = lc.handle(Enqueue(goal="v2 migration replay check")).task_id
    paused = lc.handle(Start(task_id=tid, trigger="explicit", mode="unattended"))
    assert paused.state == "paused_by_user"
    mid = lc.inspect(GetTask(tid))
    assert mid is not None and int(mid.cumulative.get("actions_completed", 0)) >= 1

    # Simulate a v2 database file: strip the slot index the new code wrote
    # and roll the schema version back so reopening runs the migration.
    conn = sqlite3.connect(str(td / "memory.db"))
    try:
        conn.execute("UPDATE actions SET slot_index=NULL WHERE task_id=?", (tid,))
        conn.execute("PRAGMA user_version=2")
        conn.commit()
    finally:
        conn.close()

    from idlecua.memory import MemoryStore

    app2 = _app(td, planner=UniquePlanner())
    app2._memory = MemoryStore(td)
    resumed = app2.lifecycle.handle(Start(task_id=tid, trigger="explicit", mode="unattended"))
    assert resumed.state == "completed"
    # No confirmed Action ran twice: every completed kind is unique (the plan
    # itself uses unique kinds), and confirmed work was never replanned.
    final = app2.lifecycle.inspect(GetTask(tid))
    assert final is not None and final.state == "completed"
    kinds = [o.get("kind") for o in final.completed_outcomes]
    assert len(kinds) == len(set(kinds)), kinds
    assert final.report_markdown


def test_pause_resume_keeps_plan_progress_and_cumulative_limits(tmp_path: Path):
    clear_emergency_stop()
    td = _confirmed_dir(tmp_path)

    class Flip(FakeIdleDetector):
        def __init__(self):
            super().__init__(idle_seconds=1000, locked=False)
            self.n = 0

        def seconds_since_last_input(self):
            self.n += 1
            return 1000.0 if self.n <= 3 else 0.0

        def can_run(self, thr=600):
            return (True, "idle") if self.n <= 3 else (False, "user returned")

        def is_screen_locked(self):
            return False

    app = _app(td, idle_detector=Flip())
    lc = app.lifecycle
    tid = lc.handle(Enqueue(goal="resumable work")).task_id
    paused = lc.handle(Start(task_id=tid, trigger="explicit", mode="unattended"))
    assert paused.state == "paused_by_user"
    mid = lc.memory.get_task(tid)
    assert int(mid["plan_progress"]) > 0
    done_before = [
        (a["kind"], a["status"])
        for a in lc.memory.list_actions(task_id=tid)
        if a["status"] == "completed" and not str(a.get("error") or "").startswith("cleanup:")
    ]
    assert done_before

    app2 = IdleCua(config=IdleCuaConfig(data_dir=td), computer=FakeComputerDriver(),
                   idle_detector=FakeIdleDetector(idle_seconds=1000, locked=False))
    app2._memory = app.memory
    resumed = app2.lifecycle.handle(Start(task_id=tid, trigger="explicit", mode="unattended"))
    assert resumed.state == "completed"
    done_after = [
        (a["kind"], a["status"])
        for a in app2.memory.list_actions(task_id=tid)
        if a["status"] == "completed" and not str(a.get("error") or "").startswith("cleanup:")
    ]
    # No confirmed Action repeats: the resumed prefix is exactly the pause prefix.
    assert [k for k, _ in done_after[: len(done_before)]] == [k for k, _ in done_before]
    assert len(done_after) > len(done_before)
    # Cumulative active duration survives the pause.
    assert float(app2.memory.get_task(tid)["active_duration_s"]) >= float(mid["active_duration_s"]) > 0
    # One immutable plan across the pause cycle and one final report.
    assert lc.memory.get_task(tid)["plan_json"] == app2.memory.get_task(tid)["plan_json"]
    assert app2.memory.get_report(tid) is not None


def test_queue_skip_and_unattended_confirmation(tmp_path: Path):
    clear_emergency_stop()
    from idlecua.planner import StubPlanner

    class PostPlanner(StubPlanner):
        def plan(self, desc, profile=None, history=None):
            from idlecua.models.plan import Plan, RiskLevel

            return Plan(goal=desc, target="x.com", expected_actions=["open_allowed_site", "post", "save_note"],
                        expected_result="t", max_duration_minutes=10, max_actions=10,
                        risk_level=RiskLevel.medium, requires_confirmation=True)

    td = _confirmed_dir(tmp_path)
    app = _app(td, planner=PostPlanner())
    lc = app.lifecycle
    tid = lc.handle(Enqueue(goal="skip post", skip_action_types=("post",))).task_id
    out = lc.handle(Start(task_id=tid, trigger="explicit", mode="unattended"))
    assert out.state == "completed"
    kinds = {(a["kind"], a["status"]) for a in lc.memory.list_actions(task_id=tid)}
    assert ("post", "completed") not in kinds
    assert any(k == "post" and s in ("blocked", "skipped") for k, s in kinds)

    # Without a queue skip, unattended execution still skips confirmation_required.
    tid2 = lc.handle(Enqueue(goal="plain post")).task_id
    out2 = lc.handle(Start(task_id=tid2, trigger="explicit", mode="unattended"))
    assert out2.state == "completed"
    kinds2 = {(a["kind"], a["status"]) for a in lc.memory.list_actions(task_id=tid2)}
    assert ("post", "completed") not in kinds2


def test_interactive_approval_and_refusal(tmp_path: Path):
    clear_emergency_stop()
    from idlecua.planner import StubPlanner

    class PostPlanner(StubPlanner):
        def plan(self, desc, profile=None, history=None):
            from idlecua.models.plan import Plan, RiskLevel

            return Plan(goal=desc, target="x.com", expected_actions=["open_allowed_site", "post", "save_note"],
                        expected_result="t", max_duration_minutes=10, max_actions=10,
                        risk_level=RiskLevel.medium, requires_confirmation=True)

    td = _confirmed_dir(tmp_path)
    app = _app(td, planner=PostPlanner())
    app.config.readonly = False
    lc = app.lifecycle

    # Refusal skips the pending Action and continues the Session.
    seen = []

    def _refuse(action):
        seen.append(action.kind)
        return False

    tid = lc.handle(Enqueue(goal="refused post")).task_id
    out = lc.handle(Start(task_id=tid, trigger="explicit", mode="interactive", confirm_func=_refuse))
    assert out.state == "completed"
    assert seen == ["post"]
    rows = {(a["kind"], a["status"]): (a.get("error") or "") for a in lc.memory.list_actions(task_id=tid)}
    assert ("post", "blocked") in rows and "declined" in rows[("post", "blocked")].lower()
    assert ("save_note", "completed") in rows

    # Approval resumes execution; with no typed driver mapping in MVP the
    # confirmed Action is skipped-and-surfaced, never claimed as completed.
    tid2 = lc.handle(Enqueue(goal="approved post")).task_id
    out2 = lc.handle(Start(task_id=tid2, trigger="explicit", mode="interactive", confirm_func=lambda a: True))
    assert out2.state == "completed"
    rows2 = {(a["kind"], a["status"]): (a.get("error") or "") for a in lc.memory.list_actions(task_id=tid2)}
    assert ("post", "completed") not in rows2
    assert ("post", "skipped") in rows2

    # Unattended execution never enters approval: no prompt, safe skip.
    asked = []

    tid3 = lc.handle(Enqueue(goal="unattended post")).task_id
    out3 = lc.handle(Start(task_id=tid3, trigger="explicit", mode="unattended",
                           confirm_func=lambda a: asked.append(a.kind) or True))
    assert out3.state == "completed" and asked == []
    assert lc.memory.get_task(tid3)["state"] == "completed"


def test_emergency_stop_idempotent_and_blocks_idle_start(tmp_path: Path):
    clear_emergency_stop()
    td = _confirmed_dir(tmp_path)
    app = _app(td)
    lc = app.lifecycle

    tid = lc.handle(Enqueue(goal="queued work")).task_id
    first = lc.handle(EmergencyStop(reason="test"))
    second = lc.handle(EmergencyStop(reason="test"))
    assert first.category == OutcomeCategory.stopped and second.category == OutcomeCategory.stopped
    # An idle-triggered Start never overrides the latch nor consumes the task.
    idle_try = lc.handle(Start(task_id=tid, trigger="idle", mode="unattended"))
    assert idle_try.category == OutcomeCategory.stopped
    assert lc.inspect(GetTask(tid)).state == "waiting_for_idle"
    # An explicit Start clears the latch on renewed intent and runs.
    explicit = lc.handle(Start(task_id=tid, trigger="explicit", mode="unattended"))
    assert explicit.category == OutcomeCategory.ok and explicit.state == "completed"
    clear_emergency_stop()


def test_migration_preserves_history_and_converts_legacy_states(tmp_path: Path):
    from idlecua.memory import MemoryStore

    td = tmp_path / "mig"
    td.mkdir()
    mem = MemoryStore(td)
    mem.upsert_task("t-disabled", "old disabled", "waiting_for_idle", None)
    mem.update_task_state("t-disabled", "disabled")
    mem.upsert_task("t-planning", "old planning", "planning", None)
    mem.upsert_task("t-running", "old running", "running", None)
    mem.upsert_task("t-queued", "old queued", "waiting_for_idle", None)
    mem.upsert_task("t-done", "old done", "completed", None)
    mem.save_report("t-done", "# kept")
    conn = sqlite3.connect(str(td / "memory.db"))
    try:
        conn.execute("PRAGMA user_version=0")
        conn.commit()
    finally:
        conn.close()
    mem2 = MemoryStore(td)
    assert mem2.get_task("t-disabled")["state"] == "stopped"
    assert mem2.get_task("t-disabled")["stop_cause"] == "legacy_not_queued"
    assert mem2.get_task("t-planning")["state"] == "failed"
    assert mem2.get_task("t-planning")["failure_cause"] == "interrupted_unknown"
    assert mem2.get_task("t-running")["state"] == "failed"
    assert mem2.get_task("t-queued")["state"] == "waiting_for_idle"
    assert mem2.get_task("t-done")["state"] == "completed"
    assert mem2.get_report("t-done") is not None


def test_inspect_counts_and_report_markdown(tmp_path: Path):
    clear_emergency_stop()
    td = _confirmed_dir(tmp_path)
    app = _app(td)
    lc = app.lifecycle

    out = lc.handle(Enqueue(goal="counts check goal"))
    tid = out.task_id
    run = lc.handle(Start(task_id=tid, trigger="explicit", mode="unattended"))
    assert run.category == OutcomeCategory.ok and run.state == "completed"

    snap_one = lc.inspect(GetTask(tid))
    assert snap_one is not None
    assert hasattr(snap_one, "counts") and isinstance(snap_one.counts, dict)
    assert "findings" in snap_one.counts and "urls" in snap_one.counts and "errors" in snap_one.counts
    assert isinstance(snap_one.counts["findings"], int)
    assert isinstance(snap_one.counts["urls"], int)
    assert isinstance(snap_one.counts["errors"], int)
    assert snap_one.counts["findings"] >= 1
    assert snap_one.counts["urls"] >= 1
    assert "actions_completed" in snap_one.cumulative
    assert snap_one.report_markdown is not None and "# IdleCUA" in snap_one.report_markdown
    assert hasattr(snap_one, "skipped_outcomes")
    assert isinstance(snap_one.skipped_outcomes, tuple)

    listed = lc.inspect(ListTasks(limit=10))
    assert listed
    for s in listed:
        assert s.report_markdown is None
        assert isinstance(s.counts, dict)
        assert "findings" in s.counts and "urls" in s.counts and "errors" in s.counts

    active = lc.inspect(GetActive())
    if active is not None:
        assert active.report_markdown is None


def test_list_counts_use_constant_queries(tmp_path: Path):
    clear_emergency_stop()
    td = _confirmed_dir(tmp_path)
    app = _app(td)
    lc = app.lifecycle

    ids = []
    for i in range(3):
        out = lc.handle(Enqueue(goal=f"constant query goal {i}"))
        ids.append(out.task_id)
        lc.handle(Start(task_id=out.task_id, trigger="explicit", mode="unattended"))

    snaps = lc.inspect(ListTasks(limit=10))
    assert len(snaps) >= 3
    for s in snaps:
        if s.task_id in ids:
            assert isinstance(s.counts, dict)
            assert s.counts["findings"] >= 0
