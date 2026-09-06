"""One internal deep module owning every Task state transition.

Interface: `handle(command)` for mutation, `inspect(query)` for observation.
`IdleCua` remains the public Application API; CLI, HTTP API, Local UI, and
the Watch loop are adapters and never mutate lifecycle storage directly.

Closed command set: Enqueue, Start, Cancel, EmergencyStop.
Closed queries: GetTask, ListTasks, GetActive.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable


class OutcomeCategory(str, Enum):
    ok = "ok"
    not_found = "not_found"
    not_ready = "not_ready"
    already_active = "already_active"
    invalid_request = "invalid_request"
    invalid_transition = "invalid_transition"
    stopped = "stopped"
    execution_failed = "execution_failed"


@dataclass(frozen=True)
class LifecycleOutcome:
    category: OutcomeCategory
    task_id: str | None = None
    state: str | None = None
    message: str = ""
    retryable: bool = False
    failed_gate: str | None = None


@dataclass(frozen=True)
class Enqueue:
    goal: str
    skip_action_types: tuple[str, ...] = ()
    approvals: tuple[str, ...] = ()


@dataclass(frozen=True)
class Start:
    task_id: str | None = None
    trigger: str = "explicit"  # explicit | idle
    mode: str = "unattended"  # unattended | interactive
    confirm_func: Callable | None = field(default=None, compare=False)


@dataclass(frozen=True)
class Cancel:
    task_id: str
    reason: str = "cancelled"


@dataclass(frozen=True)
class EmergencyStop:
    reason: str = "emergency stop"


@dataclass(frozen=True)
class GetTask:
    task_id: str


@dataclass(frozen=True)
class ListTasks:
    states: tuple[str, ...] = ()
    limit: int = 100


@dataclass(frozen=True)
class GetActive:
    pass


@dataclass(frozen=True)
class TaskSnapshot:
    task_id: str
    goal: str
    state: str
    plan: dict | None = None
    completed_outcomes: tuple[dict, ...] = ()
    cumulative: dict = field(default_factory=dict)
    skipped_types: tuple[str, ...] = ()
    stop_or_failure_cause: str | None = None
    last_outcome: str | None = None
    report_markdown: str | None = None


TERMINAL_STATES = frozenset({"completed", "failed", "stopped"})
STARTABLE_STATES = frozenset({"waiting_for_idle", "paused_by_user", "paused_for_approval"})
CANCELLABLE_STATES = frozenset({"waiting_for_idle", "paused_by_user", "paused_for_approval"})


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


class TaskLifecycle:
    """Internal owner of Task/Session lifecycle. See module docstring."""

    def __init__(
        self,
        config,
        memory,
        *,
        driver=None,
        model_provider=None,
        planner=None,
        policy=None,
        idle_detector=None,
    ) -> None:
        self.config = config
        self.memory = memory
        self._driver = driver
        self._model_provider = model_provider
        self._planner = planner
        self._policy = policy
        self._idle_detector = idle_detector

    # -- lazy deps (set by IdleCua after construction) --

    @property
    def driver(self):
        return self._driver

    @driver.setter
    def driver(self, v) -> None:
        self._driver = v

    @property
    def model_provider(self):
        return self._model_provider

    @model_provider.setter
    def model_provider(self, v) -> None:
        self._model_provider = v

    @property
    def planner(self):
        if self._planner is None:
            from .planner import StubPlanner

            self._planner = StubPlanner()
        return self._planner

    @planner.setter
    def planner(self, v) -> None:
        self._planner = v

    @property
    def policy(self):
        if self._policy is None:
            from .policy import PolicyEngine

            self._policy = PolicyEngine(self.config)
        return self._policy

    @policy.setter
    def policy(self, v) -> None:
        self._policy = v

    @property
    def idle_detector(self):
        if self._idle_detector is None:
            from .idle import FakeIdleDetector

            self._idle_detector = FakeIdleDetector(idle_seconds=1000, locked=False)
        return self._idle_detector

    @idle_detector.setter
    def idle_detector(self, v) -> None:
        self._idle_detector = v

    # -- public seam --

    def handle(self, command) -> LifecycleOutcome:
        try:
            if isinstance(command, Enqueue):
                return self._handle_enqueue(command)
            if isinstance(command, Start):
                return self._handle_start(command)
            if isinstance(command, Cancel):
                return self._handle_cancel(command)
            if isinstance(command, EmergencyStop):
                return self._handle_emergency_stop(command)
        except LifecycleOutcomeError as e:
            return e.outcome
        raise TypeError(f"unknown command: {type(command).__name__}")

    def inspect(self, query):
        if isinstance(query, GetTask):
            return self._inspect_one(query.task_id)
        if isinstance(query, ListTasks):
            return self._inspect_list(query)
        if isinstance(query, GetActive):
            return self._inspect_active()
        raise TypeError(f"unknown query: {type(query).__name__}")

    # -- inspection --

    def _snapshot_from_row(self, row: dict) -> TaskSnapshot:
        plan = None
        try:
            if row.get("plan_json"):
                plan = json.loads(row["plan_json"])
        except Exception:
            plan = None
        completed: list[dict] = []
        try:
            for a in self.memory.list_actions(task_id=row["id"]):
                if a.get("status") == "completed":
                    completed.append(
                        {"kind": a.get("kind"), "target_url": a.get("target_url"), "status": "completed"}
                    )
        except Exception:
            completed = []
        try:
            skipped = tuple(json.loads(row.get("skipped_types") or "[]"))
        except Exception:
            skipped = ()
        cause = row.get("stop_cause") or row.get("failure_cause")
        cumulative = {
            "active_duration_s": float(row.get("active_duration_s") or 0),
            "plan_progress": int(row.get("plan_progress") or 0),
            "actions_completed": len(completed),
        }
        try:
            rep = self.memory.get_report(row["id"])
            md = rep.get("markdown") if rep else None
        except Exception:
            md = None
        return TaskSnapshot(
            task_id=row["id"],
            goal=row.get("description", ""),
            state=row.get("state", "unknown"),
            plan=plan,
            completed_outcomes=tuple(completed),
            cumulative=cumulative,
            skipped_types=skipped,
            stop_or_failure_cause=cause,
            last_outcome=row.get("last_outcome"),
            report_markdown=md,
        )

    def _inspect_one(self, task_id: str) -> TaskSnapshot | None:
        try:
            row = self.memory.get_task(task_id)
        except Exception:
            return None
        if not row:
            return None
        return self._snapshot_from_row(row)

    def _inspect_list(self, query: ListTasks) -> list[TaskSnapshot]:
        try:
            if query.states:
                rows = self.memory.list_tasks_fifo(list(query.states), limit=query.limit)
                # FIFO is oldest-first; keep that order for selection parity.
                ordered = rows
            else:
                rows = self.memory.list_tasks(limit=query.limit)
                ordered = rows
        except Exception:
            return []
        out = []
        for r in ordered:
            try:
                out.append(self._snapshot_from_row(r))
            except Exception:
                continue
        return out

    def _inspect_active(self) -> TaskSnapshot | None:
        try:
            lease = self.memory.lease_get()
        except Exception:
            lease = None
        if lease and lease.get("task_id"):
            snap = self._inspect_one(str(lease["task_id"]))
            if snap is not None:
                return snap
        # Fallback: newest running/planning row (no lease detail exposed).
        try:
            for r in self.memory.list_tasks(limit=20):
                if r.get("state") in ("running", "planning"):
                    return self._snapshot_from_row(r)
        except Exception:
            pass
        return None

    # -- commands --

    def _handle_enqueue(self, cmd: Enqueue) -> LifecycleOutcome:
        goal = (cmd.goal or "").strip()
        if not goal:
            return LifecycleOutcome(OutcomeCategory.invalid_request, None, None, "goal must be non-empty", False)
        if cmd.approvals:
            return LifecycleOutcome(
                OutcomeCategory.invalid_request, None, None,
                "queue-time approval rejected in v1: unattended execution cannot impersonate interactive confirmation",
                False,
            )
        skips = sorted(set(s.strip() for s in (cmd.skip_action_types or ()) if s and s.strip()))
        if skips:
            from .policy import classify_action
            from .policy import ActionClass

            for s in skips:
                try:
                    ac = classify_action(s)
                except Exception:
                    ac = ActionClass.unknown
                if ac == ActionClass.unknown:
                    return LifecycleOutcome(
                        OutcomeCategory.invalid_request, None, None,
                        f"unknown skip action type: '{s}'", False,
                    )
        task_id = uuid.uuid4().hex
        try:
            self.memory.upsert_task(task_id, goal, "waiting_for_idle", None)
            self.memory.update_task_checkpoint(task_id, skipped_types=skips, last_outcome="enqueued")
        except Exception:
            return LifecycleOutcome(OutcomeCategory.execution_failed, None, None, "enqueue persistence failed", True)
        return LifecycleOutcome(OutcomeCategory.ok, task_id, "waiting_for_idle", "enqueued", False)

    def _handle_cancel(self, cmd: Cancel) -> LifecycleOutcome:
        try:
            row = self.memory.get_task(cmd.task_id)
        except Exception:
            return LifecycleOutcome(OutcomeCategory.execution_failed, cmd.task_id, None, "cancel read failed", True)
        if not row:
            return LifecycleOutcome(OutcomeCategory.not_found, cmd.task_id, None, "task not found", False)
        state = row.get("state", "")
        if state in TERMINAL_STATES:
            return LifecycleOutcome(OutcomeCategory.invalid_transition, cmd.task_id, state, f"terminal task cannot be cancelled (state={state})", False)
        if state not in CANCELLABLE_STATES:
            return LifecycleOutcome(
                OutcomeCategory.invalid_transition, cmd.task_id, state,
                "only queued or paused tasks can be cancelled", False,
            )
        cause = f"cancelled:{cmd.reason}" if cmd.reason else "cancelled"
        try:
            self.memory.update_task_state(cmd.task_id, "stopped")
            self.memory.update_task_checkpoint(cmd.task_id, stop_cause=cause, last_outcome="cancelled")
            self.memory.record_error(str(uuid.uuid4()), cmd.task_id, cause)
            # A paused task holds no lease; a queued task never held one. Be safe anyway.
            try:
                lease = self.memory.lease_get()
                if lease and lease.get("task_id") == cmd.task_id:
                    self.memory.lease_release(cmd.task_id)
            except Exception:
                pass
        except Exception:
            return LifecycleOutcome(OutcomeCategory.execution_failed, cmd.task_id, state, "cancel persistence failed", True)
        return LifecycleOutcome(OutcomeCategory.stopped, cmd.task_id, "stopped", cause, False)

    def _handle_emergency_stop(self, cmd: EmergencyStop) -> LifecycleOutcome:
        from . import executor as _ex

        reason = cmd.reason or "emergency stop"
        # Idempotent latch first — never waits for LLM.
        try:
            _ex.request_emergency_stop(reason)
        except Exception:
            pass
        try:
            if self._driver is not None:
                if hasattr(self._driver, "release_all_inputs"):
                    try:
                        self._driver.release_all_inputs()
                    except Exception:
                        pass
                if hasattr(self._driver, "terminate_agent_processes"):
                    try:
                        self._driver.terminate_agent_processes()  # type: ignore
                    except Exception:
                        pass
        except Exception:
            pass
        cause = f"emergency_stop:{reason}"
        # Stop the active task when present; distinct cause from Cancellation.
        active_id: str | None = None
        try:
            lease = self.memory.lease_get()
            if lease and lease.get("task_id"):
                active_id = str(lease["task_id"])
        except Exception:
            active_id = None
        if active_id is None:
            try:
                for r in self.memory.list_tasks(limit=20):
                    if r.get("state") in ("running", "planning", "paused_for_approval"):
                        active_id = r["id"]
                        break
            except Exception:
                active_id = None
        if active_id is not None:
            try:
                row = self.memory.get_task(active_id)
                if row and row.get("state") not in TERMINAL_STATES:
                    self.memory.update_task_state(active_id, "stopped")
                    self.memory.update_task_checkpoint(active_id, stop_cause=cause, last_outcome="emergency_stop")
                    self.memory.record_error(str(uuid.uuid4()), active_id, cause)
            except Exception:
                pass
            try:
                self.memory.lease_release(active_id)
            except Exception:
                pass
            try:
                self.memory.kv_set("last_stop_reason", reason)
            except Exception:
                pass
            return LifecycleOutcome(OutcomeCategory.stopped, active_id, "stopped", cause, False)
        try:
            self.memory.kv_set("last_stop_reason", reason)
        except Exception:
            pass
        try:
            self.memory.lease_release()
        except Exception:
            pass
        return LifecycleOutcome(OutcomeCategory.stopped, None, None, cause, False)

    # -- Start --

    def _load_profile(self):
        try:
            from .profile.store import load_profile

            return load_profile(Path(self.config.data_dir) / "profile.json")
        except Exception:
            return None

    def _effective_threshold(self, profile) -> int:
        try:
            from .profile.models import get_effective_idle_threshold_seconds

            return int(get_effective_idle_threshold_seconds(profile, fallback=int(getattr(self.config, "idle_threshold_seconds", 600))))
        except Exception:
            return int(getattr(self.config, "idle_threshold_seconds", 600))

    def _readiness(self, threshold_override: int | None = None) -> dict:
        """One decision path for explicit and idle-triggered Start.

        Owned here so every caller (CLI, HTTP API, Watch loop) shares one
        verdict. Scheduler only polls the HID primitive; it never decides.
        A threshold override only changes the value used inside this path.
        Structured gate reasons are safe static text: raw dependency
        errors stay in diagnostic history, never in transport messages.
        """
        from .profile.store import load_profile as _lp
        from .profile.validate import validate_profile

        profile = self._load_profile()
        try:
            threshold = self._effective_threshold(profile)
        except Exception:
            threshold = int(getattr(self.config, "idle_threshold_seconds", 600))
        if threshold_override is not None:
            try:
                threshold = int(threshold_override)
            except Exception:
                pass
        ppath = Path(self.config.data_dir) / "profile.json"
        try:
            _p = _lp(ppath)
            if _p is None:
                profile_gate = {"ok": False, "reason": f"No profile found at {ppath}. Run `idle-cua profile interview` and confirm.", "gate": "profile"}
            elif not bool(getattr(_p, "confirmed", False)):
                profile_gate = {"ok": False, "reason": f"Profile at {ppath} is unconfirmed. Complete `idle-cua profile interview` and confirm, or `idle-cua profile show` to inspect. Autonomous runs are blocked until the profile is confirmed.", "gate": "profile"}
            else:
                _errs = validate_profile(_p)
                if _errs:
                    profile_gate = {"ok": False, "reason": f"Profile at {ppath} is confirmed but invalid: {'; '.join(_errs)}. Run `idle-cua profile validate`.", "gate": "profile"}
                else:
                    profile_gate = {"ok": True, "reason": "Profile is confirmed and valid.", "gate": "profile"}
        except Exception:
            profile_gate = {"ok": False, "reason": "profile check failed", "gate": "profile"}
        try:
            if profile is None:
                schedule_gate = {"ok": True, "reason": "No profile — allowed hours default 24/7", "gate": "schedule"}
            else:
                allowed = getattr(getattr(profile, "autonomy_boundaries", None), "allowed_hours", "00:00-23:59") or "00:00-23:59"
                from .schedule_utils import is_within_allowed_hours
                import datetime as _dt

                ok_s, _ = is_within_allowed_hours(allowed)
                now = _dt.datetime.now().time()
                if ok_s:
                    schedule_gate = {"ok": True, "reason": f"Schedule allows {allowed} (now {now})", "gate": "schedule"}
                else:
                    schedule_gate = {"ok": False, "reason": f"Schedule blocks execution: allowed_hours {allowed} (now {now})", "gate": "schedule"}
        except Exception:
            schedule_gate = {"ok": True, "reason": "schedule check skipped", "gate": "schedule"}
        try:
            if bool(self.idle_detector.is_screen_locked()):
                idle_gate = {"ok": False, "reason": "Screen is locked — agent will not run until unlocked", "gate": "screen"}
            else:
                ok_i, reason_i = self.idle_detector.can_run(int(threshold))
                idle_gate = {"ok": bool(ok_i), "reason": str(reason_i), "gate": "idle"}
        except Exception:
            idle_gate = {"ok": False, "reason": "idle check failed", "gate": "idle"}
        if not bool(getattr(self.config, "require_idle", True)):
            idle_gate = {"ok": True, "reason": "idle gate disabled (require_idle=False)", "gate": "idle"}
        try:
            from .accounting import get_today_count

            llm_today = get_today_count(self.config.data_dir)
            cap = int(getattr(self.config, "max_llm_calls_per_day", 150))
            if int(llm_today) >= cap:
                limits_gate = {"ok": False, "reason": f"LLM daily cap reached: {llm_today}/{cap}", "gate": "limits"}
            else:
                limits_gate = {"ok": True, "reason": "Limits not reached", "gate": "limits"}
        except Exception:
            limits_gate = {"ok": True, "reason": "Limits not reached", "gate": "limits"}
        for g in (profile_gate, schedule_gate, idle_gate, limits_gate):
            if not g["ok"]:
                gate = g["gate"]
                reason = g["reason"]
                if gate == "idle":
                    combined = f"idle gate blocked: {reason}"
                elif gate == "screen":
                    combined = f"screen locked — {reason}"
                else:
                    combined = f"{gate}: {reason}" if not reason.startswith(f"{gate}:") else reason
                return {
                    "threshold": threshold,
                    "profile": profile_gate,
                    "schedule": schedule_gate,
                    "idle": idle_gate,
                    "limits": limits_gate,
                    "can_start": False,
                    "reason": combined,
                    "failed_gate": gate,
                }
        return {
            "threshold": threshold,
            "profile": profile_gate,
            "schedule": schedule_gate,
            "idle": idle_gate,
            "limits": limits_gate,
            "can_start": True,
            "reason": "all gates pass",
            "failed_gate": None,
        }

    def _recover_stale_lease(self) -> None:
        """New-process recovery: dead lease -> outcome_unknown + terminal fail.

        Preserves confirmed findings/history; never retries the uncertain Action.
        Releases held input. Live leases block regardless of elapsed time.
        """
        try:
            lease = self.memory.lease_get()
        except Exception:
            return
        if not lease or not lease.get("task_id"):
            return
        try:
            pid = int(lease.get("pid", -1))
        except Exception:
            pid = -1
        if _is_pid_alive(pid):
            return
        task_id = str(lease["task_id"])
        # Mark in-flight started-but-unconfirmed Actions as outcome_unknown.
        try:
            actions = self.memory.list_actions(task_id=task_id)
        except Exception:
            actions = []
        marked = False
        for a in actions:
            if a.get("status") == "started":
                try:
                    self.memory.update_action_status(a["id"], "outcome_unknown", "interrupted after dispatch before confirmation")
                    marked = True
                except Exception:
                    continue
        try:
            if self._driver is not None and hasattr(self._driver, "release_all_inputs"):
                self._driver.release_all_inputs()
        except Exception:
            pass
        try:
            row = self.memory.get_task(task_id)
            if row and row.get("state") not in TERMINAL_STATES:
                self.memory.update_task_state(task_id, "failed")
                self.memory.update_task_checkpoint(
                    task_id, failure_cause="interrupted_unknown", last_outcome="outcome_unknown"
                )
                msg = "interrupted_unknown: action dispatched before confirmation; never retried automatically"
                if not marked:
                    msg = "interrupted_unknown: stale lease with dead PID; confirmed progress preserved"
                self.memory.record_error(str(uuid.uuid4()), task_id, msg)
        except Exception:
            pass
        try:
            self.memory.lease_release(task_id)
        except Exception:
            pass

    def _select_next(self) -> dict | None:
        """Oldest paused before oldest queued; FIFO within each group."""
        try:
            paused = self.memory.list_tasks_fifo(["paused_by_user"], limit=1)
            if paused:
                return paused[0]
            queued = self.memory.list_tasks_fifo(["waiting_for_idle"], limit=1)
            if queued:
                return queued[0]
        except Exception:
            return None
        return None

    def _handle_start(self, cmd: Start) -> LifecycleOutcome:
        from . import executor as _ex

        if cmd.trigger not in ("explicit", "idle"):
            return LifecycleOutcome(OutcomeCategory.invalid_request, cmd.task_id, None, "trigger must be explicit|idle", False)
        if cmd.mode not in ("unattended", "interactive"):
            return LifecycleOutcome(OutcomeCategory.invalid_request, cmd.task_id, None, "mode must be unattended|interactive", False)
        if cmd.mode == "interactive" and cmd.trigger == "idle":
            # Idle-triggered Starts are always unattended; interactive needs a live owner.
            return LifecycleOutcome(OutcomeCategory.invalid_request, cmd.task_id, None, "idle-triggered start cannot be interactive", False)

        # Crash recovery runs before any new claim.
        try:
            self._recover_stale_lease()
        except Exception:
            pass

        # Resolve target.
        try:
            row = None
            if cmd.task_id is not None:
                row = self.memory.get_task(cmd.task_id)
                if not row:
                    return LifecycleOutcome(OutcomeCategory.not_found, cmd.task_id, None, "task not found", False)
            else:
                row = self._select_next()
                if not row:
                    return LifecycleOutcome(OutcomeCategory.not_ready, None, None, "no queued or paused task", False)
        except LifecycleOutcomeError:
            raise
        except Exception:
            return LifecycleOutcome(OutcomeCategory.execution_failed, cmd.task_id, None, "start read failed", True)

        task_id = row["id"]
        state = row.get("state", "")
        if state in TERMINAL_STATES:
            return LifecycleOutcome(OutcomeCategory.invalid_transition, task_id, state, f"terminal task cannot start (state={state}); create a new task", False)
        if state in ("running", "planning"):
            return LifecycleOutcome(OutcomeCategory.already_active, task_id, state, "session already active", False)
        if state == "paused_for_approval" and cmd.mode == "unattended":
            return LifecycleOutcome(OutcomeCategory.invalid_transition, task_id, state, "approval-paused task requires interactive start", False)
        if state not in STARTABLE_STATES:
            return LifecycleOutcome(OutcomeCategory.invalid_transition, task_id, state, f"task cannot start from state={state}", False)

        # Active-Session concurrency: one live lease blocks regardless of elapsed time.
        try:
            lease = self.memory.lease_get()
            if lease and str(lease.get("task_id")) != task_id:
                try:
                    other_pid = int(lease.get("pid", -1))
                except Exception:
                    other_pid = -1
                if _is_pid_alive(other_pid):
                    return LifecycleOutcome(OutcomeCategory.already_active, task_id, state, "another session is active", True)
                # Dead lease for another task: recover it, then continue.
                try:
                    self._recover_stale_lease()
                except Exception:
                    pass
        except LifecycleOutcomeError:
            raise
        except Exception:
            pass

        # An idle-triggered Start never overrides a latched Emergency stop:
        # the task is left untouched until an explicit start clears the latch.
        if cmd.trigger == "idle" and _ex.is_emergency_stop_requested():
            return LifecycleOutcome(
                OutcomeCategory.stopped, task_id, state,
                "emergency stop latched; explicit start required", False,
            )

        # One readiness path for explicit and idle triggers.
        readiness = self._readiness()
        if not readiness["can_start"]:
            return LifecycleOutcome(
                OutcomeCategory.not_ready, task_id, state,
                str(readiness["reason"]), True, failed_gate=str(readiness.get("failed_gate")),
            )

        # A fresh explicit run clears a previous in-process stop latch.
        # Idle-triggered Starts never clear it: after an Emergency stop the
        # Watch loop must stay down until an explicit scheduler start.
        if cmd.trigger == "explicit":
            try:
                _ex.clear_emergency_stop()
            except Exception:
                pass
        try:
            self.memory.lease_acquire(task_id, os.getpid())
        except Exception:
            return LifecycleOutcome(OutcomeCategory.execution_failed, task_id, state, "lease acquire failed", True)

        try:
            return self._run_session(task_id, row, cmd, readiness)
        finally:
            try:
                self.memory.lease_release(task_id)
            except Exception:
                pass

    # -- session execution --

    def _run_session(self, task_id: str, row: dict, cmd: Start, readiness: dict) -> LifecycleOutcome:
        import time as _time

        from .action_runner import dispatch_one, snapshot_for_verify, verify_significant
        from .dedup import normalize_query, normalize_url, plan_fingerprint, url_fingerprint
        from .models.plan import Plan
        from .models.plan import RiskLevel as _RL
        from .planner import LlmCallCapExceeded, LlmPlanner, PlanRejectedError, StubPlanner
        from .policy import ActionClass, PolicyEngine, PolicyVerdict, TypedAction, classify_action
        from .report import generate_markdown_report, save_report_to_file
        from . import executor as _ex

        profile = self._load_profile()
        history = None
        try:
            history = self.memory.get_history(limit=50)
        except Exception:
            history = None

        run_start = _time.monotonic()
        approval_wait_s = 0.0
        try:
            cumulative_base = float(row.get("active_duration_s") or 0)
        except Exception:
            cumulative_base = 0.0
        try:
            stored_skips = list(json.loads(row.get("skipped_types") or "[]"))
        except Exception:
            stored_skips = []
        skip_set = set(stored_skips)
        # Resuming keeps the persisted skip set; a fresh Start uses it as enqueued.
        is_resume = row.get("state") == "paused_by_user" or row.get("state") == "paused_for_approval"
        try:
            progress = int(row.get("plan_progress") or 0)
        except Exception:
            progress = 0

        plan = None
        plan_error: str | None = None
        cap_note: str | None = None
        if is_resume:
            raw = row.get("plan_json")
            if not raw:
                self._fail_task(task_id, "failed", "interrupted_unknown", "resume without persisted plan", {})
                self._write_report_best_effort(task_id, row.get("description", ""), None, [], [], [], [{"message": "resume without persisted plan"}], [], profile)
                return LifecycleOutcome(OutcomeCategory.execution_failed, task_id, "failed", "resume without persisted plan", False)
            try:
                pj = json.loads(raw)
                plan = Plan(
                    goal=pj["goal"],
                    target=pj["target"],
                    expected_actions=list(pj.get("expected_actions", [])),
                    expected_result=pj.get("expected_result", ""),
                    max_duration_minutes=int(pj.get("max_duration_minutes", 45)),
                    max_actions=int(pj.get("max_actions", 50)),
                    risk_level=_RL(pj.get("risk_level", "low")),
                    requires_confirmation=bool(pj.get("requires_confirmation", False)),
                )
            except Exception:
                self._fail_task(task_id, "failed", "interrupted_unknown", "resume plan unreadable", {})
                self._write_report_best_effort(task_id, row.get("description", ""), None, [], [], [], [{"message": "resume plan unreadable"}], [], profile)
                return LifecycleOutcome(OutcomeCategory.execution_failed, task_id, "failed", "resume plan unreadable", False)
            # Resume passes the same gates (already checked) and continues after last confirmed Action.
            try:
                self.memory.update_task_state(task_id, "running")
            except Exception:
                return LifecycleOutcome(OutcomeCategory.execution_failed, task_id, row.get("state"), "resume persistence failed", True)
        else:
            try:
                self.memory.update_task_state(task_id, "planning")
            except Exception:
                return LifecycleOutcome(OutcomeCategory.execution_failed, task_id, row.get("state"), "state persistence failed", True)
            # Plan creation (LLM path with graceful cap fallback, mirroring executor).
            try:
                planner = self.planner
                if isinstance(planner, LlmPlanner):
                    try:
                        plan = planner.plan(row.get("description", ""), profile=profile, history=history)
                    except LlmCallCapExceeded as ce:
                        cap_note = str(ce)
                        fb = getattr(planner, "fallback", None) or StubPlanner()
                        plan = fb.plan(row.get("description", ""), profile=profile, history=history)
                    except PlanRejectedError as pe:
                        plan_error = f"plan rejected (LLM output not convertible to typed actions, never executed as free text): {pe}"
                        plan = None
                    except Exception:
                        plan_error = "planner error: planning failed"
                        plan = None
                else:
                    try:
                        plan = planner.plan(row.get("description", ""), profile=profile, history=history)  # type: ignore[call-arg]
                    except TypeError:
                        plan = planner.plan(row.get("description", ""))
            except Exception:
                plan_error = plan_error or "planning failed"
                plan = None
            if plan is None:
                detail = plan_error or "planning failed"
                # Safe structured outcome: never leak raw provider output in the
                # transport message; full detail stays in failure_cause/report.
                safe = "plan rejected: LLM output not convertible to typed actions" if detail.startswith("plan rejected") else "planning failed"
                self._fail_task(task_id, "failed", "planning_failed", detail, {})
                self._write_report_best_effort(task_id, row.get("description", ""), None, [], [], [], [{"message": detail}], [], profile)
                return LifecycleOutcome(OutcomeCategory.execution_failed, task_id, "failed", safe, False)
            # Tighten caps from profile when stricter.
            try:
                if profile is not None:
                    pm = getattr(getattr(profile, "autonomy_boundaries", None), "session_duration_minutes", None)
                    if pm and int(pm) < int(plan.max_duration_minutes):
                        plan = Plan(
                            goal=plan.goal, target=plan.target, expected_actions=list(plan.expected_actions),
                            expected_result=plan.expected_result,
                            max_duration_minutes=min(int(plan.max_duration_minutes), int(pm)),
                            max_actions=min(int(plan.max_actions), int(getattr(profile.autonomy_boundaries, "daily_action_limit", 200))),
                            risk_level=plan.risk_level, requires_confirmation=plan.requires_confirmation,
                        )
            except Exception:
                pass
            # Anti-repeat: identical plan within 7 days never re-executes.
            # Report-before-completion (US33): the Report is persisted before
            # the Task becomes completed; a report failure fails the Task.
            try:
                fp = plan_fingerprint(plan)
                if self.memory.has_plan_fingerprint_within_days(fp, days=7):
                    md = self._write_report_best_effort(task_id, row.get("description", ""), plan, [], [], [], [{"message": f"skipped repeat plan {fp}"}], [{"type": "plan", "value": fp, "reason": "plan identical to recent session within 7 days — skipped repeat"}], profile)
                    if md is None:
                        # _cumulative_s is defined below; here only the stored
                        # base has elapsed (planning just finished).
                        self._fail_task(task_id, "failed", "report_failed", "report persistence failed", {"active_duration_s": float(cumulative_base), "plan_progress": progress})
                        return LifecycleOutcome(OutcomeCategory.execution_failed, task_id, "failed", "report persistence failed; confirmed progress retained", False)
                    try:
                        self.memory.update_task_state(task_id, "completed")
                        self.memory.update_task_checkpoint(task_id, last_outcome="skipped_repeat_plan")
                        self.memory.record_error(str(uuid.uuid4()), task_id, f"skipped repeat plan {fp}")
                    except Exception:
                        return LifecycleOutcome(OutcomeCategory.execution_failed, task_id, "running", "completion persistence failed", True)
                    return LifecycleOutcome(OutcomeCategory.ok, task_id, "completed", f"skipped repeat plan {fp}", False)
            except Exception:
                pass
            # Persist the immutable Plan before the first Action.
            pj = {
                "goal": plan.goal, "target": plan.target, "expected_actions": list(plan.expected_actions),
                "expected_result": plan.expected_result, "max_duration_minutes": plan.max_duration_minutes,
                "max_actions": plan.max_actions, "risk_level": plan.risk_level.value,
                "requires_confirmation": plan.requires_confirmation,
            }
            try:
                stored = self.memory.set_task_plan_if_absent(task_id, json.dumps(pj))
                if not stored:
                    # Another process won the race: reload the persisted plan (immutability).
                    fresh = self.memory.get_task(task_id)
                    if fresh and fresh.get("plan_json"):
                        pj2 = json.loads(fresh["plan_json"])
                        plan = Plan(
                            goal=pj2["goal"], target=pj2["target"], expected_actions=list(pj2.get("expected_actions", [])),
                            expected_result=pj2.get("expected_result", ""), max_duration_minutes=int(pj2.get("max_duration_minutes", 45)),
                            max_actions=int(pj2.get("max_actions", 50)), risk_level=_RL(pj2.get("risk_level", "low")),
                            requires_confirmation=bool(pj2.get("requires_confirmation", False)),
                        )
            except Exception:
                return LifecycleOutcome(OutcomeCategory.execution_failed, task_id, "planning", "plan persistence failed", True)
            try:
                self.memory.update_task_state(task_id, "running")
            except Exception:
                return LifecycleOutcome(OutcomeCategory.execution_failed, task_id, "planning", "state persistence failed", True)

        assert plan is not None
        expected = list(plan.expected_actions)
        if progress < 0:
            progress = 0
        if progress > len(expected):
            progress = len(expected)

        # Pre-snapshot dedup sets (avoid self-blocking intra-session).
        try:
            existing_url_fps = {r["fingerprint"] for r in self.memory.list_urls(limit=1000)}
            existing_query_norms = {r["normalized"] for r in self.memory.list_queries(limit=1000)}
        except Exception:
            existing_url_fps, existing_query_norms = set(), set()

        queries: list[dict] = []
        urls: list[dict] = []
        findings: list[dict] = []
        errors: list[dict] = []
        skipped: list[dict] = []
        # Reload confirmed history for resume so the Report keeps prior findings.
        if is_resume:
            try:
                for q in self.memory.list_queries(limit=1000):
                    if q.get("task_id") == task_id:
                        queries.append(q)
                for u in self.memory.list_urls(limit=1000):
                    if u.get("task_id") == task_id:
                        urls.append(u)
                findings = self.memory.list_findings(task_id=task_id)
                errors = self.memory.list_errors(task_id=task_id)
            except Exception:
                pass
        if cap_note:
            errors.append({"message": f"LLM cap reached at planning — {cap_note} (graceful fallback to stub, state saved)"})
            try:
                self.memory.record_error(str(uuid.uuid4()), task_id, f"LLM cap reached at planning — {cap_note}")
            except Exception:
                pass
            skipped.append({"type": "llm_cap", "value": str(getattr(self.config, "max_llm_calls_per_day", 150)), "reason": cap_note})

        from .accounting import get_today_count

        def _active_s() -> float:
            return max(0.0, _time.monotonic() - run_start - approval_wait_s)

        def _cumulative_s() -> float:
            return float(cumulative_base) + _active_s()

        def _checkpoint(progress_v: int, outcome: str) -> bool:
            try:
                self.memory.update_task_checkpoint(task_id, plan_progress=progress_v, active_duration_s=_cumulative_s(), last_outcome=outcome)
                return True
            except Exception:
                return False

        # Schedule gate re-check uses profile; allowed-hours block fails fast.
        try:
            from .schedule_utils import is_within_allowed_hours

            allowed = getattr(getattr(profile, "autonomy_boundaries", None), "allowed_hours", "00:00-23:59") or "00:00-23:59"
            ok_h, _ = is_within_allowed_hours(allowed)
            if not ok_h:
                msg = f"schedule blocks execution: allowed_hours {allowed}"
                self._fail_task(task_id, "failed", "schedule_blocked", msg, {"active_duration_s": _cumulative_s(), "plan_progress": progress})
                self._write_report_best_effort(task_id, row.get("description", ""), plan, queries, urls, findings, errors + [{"message": msg}], skipped, profile)
                return LifecycleOutcome(OutcomeCategory.execution_failed, task_id, "failed", msg, False)
        except Exception:
            pass

        i = progress
        paused_for_user = False
        stopped_terminal: str | None = None
        stop_detail: str | None = None
        while i < len(expected):
            kind = expected[i]
            # Emergency latch first — LLM-independent.
            if _ex.is_emergency_stop_requested():
                stopped_terminal = "stopped"
                stop_detail = _ex.get_emergency_stop_reason() or "emergency stop"
                try:
                    if self._driver is not None and hasattr(self._driver, "release_all_inputs"):
                        self._driver.release_all_inputs()
                    if self._driver is not None and hasattr(self._driver, "terminate_agent_processes"):
                        self._driver.terminate_agent_processes()  # type: ignore
                except Exception:
                    pass
                try:
                    self.memory.record_error(str(uuid.uuid4()), task_id, f"stopped: {stop_detail}")
                except Exception:
                    pass
                errors.append({"message": f"stopped: {stop_detail}"})
                break
            # Owner return -> pause the same Session, release input, checkpoint.
            if self._owner_returned():
                try:
                    if self._driver is not None and hasattr(self._driver, "release_all_inputs"):
                        self._driver.release_all_inputs()
                except Exception:
                    pass
                try:
                    self.memory.update_task_state(task_id, "paused_by_user")
                    self.memory.update_task_checkpoint(task_id, plan_progress=i, active_duration_s=_cumulative_s(), last_outcome="paused_by_user")
                    self.memory.record_error(str(uuid.uuid4()), task_id, "paused_by_user: hardware input detected")
                except Exception:
                    return LifecycleOutcome(OutcomeCategory.execution_failed, task_id, "running", "pause persistence failed", True)
                errors.append({"message": "paused_by_user: hardware input detected"})
                paused_for_user = True
                break
            # Cumulative limits (duration counts planning+execution only).
            lim = self._limits_hit(plan, profile, _cumulative_s(), task_id)
            if lim:
                errors.append({"message": lim})
                try:
                    self.memory.record_error(str(uuid.uuid4()), task_id, lim)
                except Exception:
                    pass
                stopped_terminal = "completed"
                stop_detail = lim
                break
            # Queue-time skip suppresses every matching Action type.
            if kind in skip_set:
                skipped.append({"type": "queue_skip", "value": kind, "reason": f"queue-time skip suppresses every '{kind}' action"})
                try:
                    aid = uuid.uuid4().hex
                    self.memory.record_action(aid, task_id, kind, None, "skipped", "skipped", f"queue-time skip: {kind}")
                except Exception:
                    pass
                i += 1
                if not _checkpoint(i, f"skipped:{kind}"):
                    return self._fail_outcome(task_id, plan, profile, queries, urls, findings, errors, skipped, "critical persistence failed before next action")
                continue
            # Build the typed action.
            local_kinds = {"save_note", "create_note", "save_link", "close_own_tab", "close_own_app"}
            if kind in local_kinds:
                action = TypedAction(kind=kind, description=row.get("description", ""))
            else:
                action = TypedAction(kind=kind, target_url=f"https://{plan.target}", description=row.get("description", ""))
            # Query/URL anti-repeat against pre-session snapshot only.
            if kind == "search":
                qn = normalize_query(row.get("description", ""))
                if qn in existing_query_norms:
                    skipped.append({"type": "query", "value": row.get("description", ""), "reason": f"normalized query '{qn}' seen within 7 days"})
                    try:
                        self.memory.record_error(str(uuid.uuid4()), task_id, f"skipped repeat query: {row.get('description','')}")
                    except Exception:
                        pass
                    errors.append({"message": f"skipped repeat query: {row.get('description','')}"})
                    i += 1
                    if not _checkpoint(i, "skipped_repeat_query"):
                        return self._fail_outcome(task_id, plan, profile, queries, urls, findings, errors, skipped, "critical persistence failed before next action")
                    continue
            if kind in ("open_allowed_site", "open_link") and action.target_url:
                fpu = url_fingerprint(action.target_url)
                if fpu in existing_url_fps:
                    skipped.append({"type": "url", "value": action.target_url, "reason": f"url fingerprint {fpu} seen within 7 days"})
                    try:
                        self.memory.record_error(str(uuid.uuid4()), task_id, f"skipped repeat url: {action.target_url}")
                    except Exception:
                        pass
                    errors.append({"message": f"skipped repeat url: {action.target_url}"})
                    i += 1
                    if not _checkpoint(i, "skipped_repeat_url"):
                        return self._fail_outcome(task_id, plan, profile, queries, urls, findings, errors, skipped, "critical persistence failed before next action")
                    continue
            # Policy gate.
            try:
                result = self.policy.evaluate(action)
            except Exception:
                result = None
            verdict = result.verdict if result is not None else PolicyVerdict.blocked
            if verdict == PolicyVerdict.needs_confirmation:
                if cmd.mode != "interactive":
                    skipped.append({"type": "action", "value": kind, "reason": f"confirmation-required '{kind}' skipped in unattended mode"})
                    try:
                        self.memory.record_action(uuid.uuid4().hex, task_id, kind, action.target_url, verdict.value, "blocked", f"needs_confirmation: {result.reason if result else 'unattended'}")
                        self.memory.record_error(str(uuid.uuid4()), task_id, f"skipped confirmation-required action '{kind}' (unattended)")
                    except Exception:
                        pass
                    errors.append({"message": f"skipped confirmation-required: {kind}"})
                    i += 1
                    if not _checkpoint(i, f"skipped_confirmation:{kind}"):
                        return self._fail_outcome(task_id, plan, profile, queries, urls, findings, errors, skipped, "critical persistence failed before next action")
                    continue
                # Interactive: pause active-duration accounting while the owner decides.
                try:
                    self.memory.update_task_state(task_id, "paused_for_approval")
                except Exception:
                    pass
                appr_start = _time.monotonic()
                confirmed = False
                try:
                    if cmd.confirm_func is not None:
                        confirmed = bool(cmd.confirm_func(action))
                    else:
                        confirmed = False
                except Exception:
                    confirmed = False
                approval_wait_s += max(0.0, _time.monotonic() - appr_start)
                try:
                    self.memory.update_task_state(task_id, "running")
                except Exception:
                    pass
                if not confirmed:
                    try:
                        self.memory.record_action(uuid.uuid4().hex, task_id, kind, action.target_url, verdict.value, "blocked", "owner declined confirmation")
                    except Exception:
                        pass
                    skipped.append({"type": "action", "value": kind, "reason": "owner declined confirmation"})
                    i += 1
                    if not _checkpoint(i, "declined_confirmation"):
                        return self._fail_outcome(task_id, plan, profile, queries, urls, findings, errors, skipped, "critical persistence failed before next action")
                    continue
                # Confirmed but MVP has no typed driver mapping -> skip-and-surface, never claim completed.
                try:
                    from .policy import SUPPORTED_DRIVER_KINDS as _SDK  # type: ignore
                except Exception:
                    _SDK = {"open_allowed_site", "open_link", "search", "read_ui", "scroll", "extract_public_info", "save_note", "close_own_tab", "open_app"}
                if classify_action(kind) == ActionClass.confirmation_required and kind not in _SDK and kind not in ("open_allowed_site", "open_link", "search", "read_ui", "scroll", "extract_public_info", "save_note", "close_own_tab", "open_app"):
                    try:
                        self.memory.record_action(uuid.uuid4().hex, task_id, kind, action.target_url, verdict.value, "skipped", f"no typed driver mapping for '{kind}' in MVP")
                        self.memory.record_error(str(uuid.uuid4()), task_id, f"skipped confirmation-required '{kind}': no driver mapping in MVP")
                    except Exception:
                        pass
                    skipped.append({"type": "action", "value": kind, "reason": f"no typed driver mapping for '{kind}' in MVP (confirmed but not executed — surfaced in report)"})
                    errors.append({"message": f"skipped confirmation-required '{kind}': no driver mapping"})
                    i += 1
                    if not _checkpoint(i, "skipped_no_mapping"):
                        return self._fail_outcome(task_id, plan, profile, queries, urls, findings, errors, skipped, "critical persistence failed before next action")
                    continue
            if verdict == PolicyVerdict.blocked:
                try:
                    self.memory.record_action(uuid.uuid4().hex, task_id, kind, action.target_url, verdict.value, "blocked", result.reason if result else "blocked")
                    self.memory.record_error(str(uuid.uuid4()), task_id, f"blocked {kind}: {result.reason if result else 'blocked'}")
                except Exception:
                    pass
                errors.append({"message": f"blocked {kind}: {result.reason if result else 'blocked'}"})
                i += 1
                if not _checkpoint(i, f"blocked:{kind}"):
                    return self._fail_outcome(task_id, plan, profile, queries, urls, findings, errors, skipped, "critical persistence failed before next action")
                continue
            # Two-phase checkpoint: started before dispatch.
            action_id = uuid.uuid4().hex
            try:
                self.memory.record_action(action_id, task_id, kind, action.target_url, verdict.value, "started", None)
            except Exception:
                return self._fail_outcome(task_id, plan, profile, queries, urls, findings, errors, skipped, "critical persistence failed before dispatch")
            # Dispatch one prepared Action.
            before = snapshot_for_verify(self._driver, kind)
            try:
                _k, fn, url_rec = dispatch_one(kind, self._driver, row.get("description", ""), action.target_url)
            except Exception as e:
                try:
                    self.memory.update_action_status(action_id, "failed", str(e))
                    self.memory.record_error(str(uuid.uuid4()), task_id, f"action {kind} failed: {e}")
                except Exception:
                    pass
                errors.append({"message": f"action {kind} failed: {e}"})
                i += 1
                if not _checkpoint(i, f"failed:{kind}"):
                    return self._fail_outcome(task_id, plan, profile, queries, urls, findings, errors, skipped, "critical persistence failed before next action")
                continue
            # No-mapping honesty for confirmation-required kinds even when policy allowed.
            try:
                from .executor import SUPPORTED_DRIVER_KINDS as _SUP

                if classify_action(kind) == ActionClass.confirmation_required and kind not in _SUP:
                    try:
                        self.memory.update_action_status(action_id, "skipped", f"no typed driver mapping for '{kind}' in MVP")
                        self.memory.record_error(str(uuid.uuid4()), task_id, f"skipped confirmation-required '{kind}': no driver mapping in MVP")
                    except Exception:
                        pass
                    skipped.append({"type": "action", "value": kind, "reason": f"no typed driver mapping for '{kind}' in MVP (confirmed but not executed — surfaced in report)"})
                    errors.append({"message": f"skipped confirmation-required '{kind}': no driver mapping"})
                    i += 1
                    if not _checkpoint(i, "skipped_no_mapping"):
                        return self._fail_outcome(task_id, plan, profile, queries, urls, findings, errors, skipped, "critical persistence failed before next action")
                    continue
            except Exception:
                pass
            dispatch_error: str | None = None
            try:
                fn()
            except BaseException as e:  # crash between dispatch and confirmation
                dispatch_error = str(e) or type(e).__name__
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    try:
                        self.memory.update_action_status(action_id, "outcome_unknown", "interrupted after dispatch before confirmation")
                        self.memory.update_task_state(task_id, "failed")
                        self.memory.update_task_checkpoint(task_id, plan_progress=i, active_duration_s=_cumulative_s(), failure_cause="interrupted_unknown", last_outcome="outcome_unknown")
                        self.memory.record_error(str(uuid.uuid4()), task_id, "interrupted_unknown: action dispatched before confirmation; never retried automatically")
                    except Exception:
                        pass
                    self._write_report_best_effort(task_id, row.get("description", ""), plan, queries, urls, findings, errors + [{"message": "interrupted_unknown"}], skipped, profile)
                    raise
                try:
                    self.memory.update_action_status(action_id, "failed", dispatch_error)
                    self.memory.record_error(str(uuid.uuid4()), task_id, f"action {kind} failed: {dispatch_error}")
                except Exception:
                    pass
                errors.append({"message": f"action {kind} failed: {dispatch_error}"})
                i += 1
                if not _checkpoint(i, f"failed:{kind}"):
                    return self._fail_outcome(task_id, plan, profile, queries, urls, findings, errors, skipped, "critical persistence failed before next action")
                continue
            # A cooperative stop that arrives after a successful dispatch is
            # handled at the top of the next iteration as `stopped`.
            # `outcome_unknown` is reserved for true interruption between
            # dispatch and confirmation (crash path above, stale-lease
            # recovery): it is never inferred from a live latch here.
            # Completion + progress advance persisted before the next Action.
            try:
                self.memory.update_action_status(action_id, "completed", None)
            except Exception:
                return self._fail_outcome(task_id, plan, profile, queries, urls, findings, errors, skipped, "critical persistence failed before next action")
            # Record queries/urls/findings.
            try:
                if kind == "search":
                    qn = normalize_query(row.get("description", ""))
                    qid = uuid.uuid4().hex
                    self.memory.record_query(qid, task_id, row.get("description", ""), qn)
                    queries.append({"id": qid, "query": row.get("description", ""), "normalized": qn})
                    if action.target_url:
                        fpu = url_fingerprint(action.target_url)
                        nu = normalize_url(action.target_url)
                        uid = uuid.uuid4().hex
                        self.memory.record_url(uid, task_id, action.target_url, nu, fpu)
                        urls.append({"id": uid, "url": action.target_url, "normalized": nu, "fingerprint": fpu})
                elif kind in ("open_allowed_site", "open_link"):
                    if action.target_url:
                        fpu = url_fingerprint(action.target_url)
                        nu = normalize_url(action.target_url)
                        uid = uuid.uuid4().hex
                        self.memory.record_url(uid, task_id, action.target_url, nu, fpu)
                        urls.append({"id": uid, "url": action.target_url, "normalized": nu, "fingerprint": fpu})
                        if kind == "open_link":
                            fid = uuid.uuid4().hex
                            title = f"Finding from {plan.target}: {row.get('description','')[:40]}"
                            summary = f"Public info extracted from {action.target_url} for goal '{plan.goal}'"
                            rel = f"Relevant to profile interests/projects for '{plan.goal}'"
                            self.memory.record_finding(fid, task_id, title, action.target_url, summary, rel)
                            findings.append({"id": fid, "title": title, "url": action.target_url, "summary": summary, "relevance": rel})
                elif kind in ("extract_public_info", "read_ui"):
                    if action.target_url:
                        fid = uuid.uuid4().hex
                        self.memory.record_finding(fid, task_id, f"Extracted: {row.get('description','')[:40]}", action.target_url, f"Extracted public info from {action.target_url}", f"Relevant to {plan.goal}")
                        findings.append({"id": fid, "title": f"Extracted: {row.get('description','')[:40]}", "url": action.target_url, "summary": f"Extracted public info from {action.target_url}", "relevance": f"Relevant to {plan.goal}"})
                elif kind == "save_note":
                    fid = uuid.uuid4().hex
                    self.memory.record_finding(fid, task_id, f"Saved note: {row.get('description','')[:40]}", f"https://{plan.target}", f"Local note saved: {row.get('description','')}", f"Relevant to {plan.goal}")
                    findings.append({"id": fid, "title": f"Saved note: {row.get('description','')[:40]}", "url": f"https://{plan.target}", "summary": f"Local note saved: {row.get('description','')}", "relevance": f"Relevant to {plan.goal}"})
            except Exception:
                pass
            verr = verify_significant(self._driver, kind, action.target_url or plan.target, before)
            if verr:
                try:
                    self.memory.record_error(str(uuid.uuid4()), task_id, f"verify failed for {kind}: {verr}")
                except Exception:
                    pass
                errors.append({"message": f"verify failed for {kind}: {verr}"})
            i += 1
            if not _checkpoint(i, f"completed:{kind}"):
                return self._fail_outcome(task_id, plan, profile, queries, urls, findings, errors, skipped, "critical persistence failed before next action")

        # Terminal paths.
        try:
            n_completed_end = sum(1 for a in self.memory.list_actions(task_id=task_id) if a.get("status") == "completed")
        except Exception:
            n_completed_end = 0
        if paused_for_user:
            self._close_agent_tabs_best_effort(task_id, plan, n_completed_end)
            return LifecycleOutcome(OutcomeCategory.ok, task_id, "paused_by_user", "paused by user; resume continues after last confirmed action", True)
        if stopped_terminal == "stopped":
            cause = f"emergency_stop:{stop_detail}" if stop_detail else "stopped"
            # If the stop arrived from the in-process latch, keep its distinct cause.
            try:
                self.memory.update_task_state(task_id, "stopped")
                self.memory.update_task_checkpoint(task_id, plan_progress=i, active_duration_s=_cumulative_s(), stop_cause=cause, last_outcome="stopped")
            except Exception:
                pass
            self._write_report_best_effort(task_id, row.get("description", ""), plan, queries, urls, findings, errors, skipped, profile)
            self._close_agent_tabs_best_effort(task_id, plan, n_completed_end)
            return LifecycleOutcome(OutcomeCategory.stopped, task_id, "stopped", cause, False)
        if stopped_terminal == "completed" and stop_detail and "limit reached" in stop_detail:
            md = self._write_report_best_effort(task_id, row.get("description", ""), plan, queries, urls, findings, errors, skipped, profile)
            if md is None:
                self._fail_task(task_id, "failed", "report_failed", "report persistence failed", {"active_duration_s": _cumulative_s(), "plan_progress": i})
                return LifecycleOutcome(OutcomeCategory.execution_failed, task_id, "failed", "report persistence failed; confirmed progress retained", False)
            try:
                self.memory.update_task_state(task_id, "completed")
                self.memory.update_task_checkpoint(task_id, plan_progress=i, active_duration_s=_cumulative_s(), last_outcome="completed_limit")
                self.memory.record_plan_fingerprint(plan_fingerprint(plan), task_id)
            except Exception:
                pass
            self._close_agent_tabs_best_effort(task_id, plan, n_completed_end)
            return LifecycleOutcome(OutcomeCategory.ok, task_id, "completed", stop_detail, False)

        # Normal finish: exactly one final Report persisted before completed.
        if not findings and urls:
            for u in urls[:2]:
                fid = uuid.uuid4().hex
                try:
                    self.memory.record_finding(fid, task_id, f"Visited {u.get('url','')}", u.get("url", ""), f"Visited {u.get('url','')} for goal '{plan.goal}'", f"Relevant to profile for '{plan.goal}'")
                except Exception:
                    pass
                findings.append({"id": fid, "title": f"Visited {u.get('url','')}", "url": u.get("url", ""), "summary": f"Visited {u.get('url','')} for goal '{plan.goal}'", "relevance": f"Relevant to profile for '{plan.goal}'"})
        md = self._write_report_best_effort(task_id, row.get("description", ""), plan, queries, urls, findings, errors, skipped, profile)
        if md is None:
            self._fail_task(task_id, "failed", "report_failed", "report persistence failed", {"active_duration_s": _cumulative_s(), "plan_progress": i})
            return LifecycleOutcome(OutcomeCategory.execution_failed, task_id, "failed", "report persistence failed; confirmed progress retained", False)
        # Success/failure by confirmed work (parity with executor graceful semantics).
        try:
            persisted_actions = self.memory.list_actions(task_id=task_id)
            n_completed = sum(1 for a in persisted_actions if a.get("status") == "completed")
        except Exception:
            n_completed = 0
        if errors and n_completed == 0 and not findings:
            self._fail_task(task_id, "failed", "execution_failed", "; ".join(e.get("message", "") for e in errors[:3]) or "execution failed", {"active_duration_s": _cumulative_s(), "plan_progress": i})
            self._close_agent_tabs_best_effort(task_id, plan, n_completed)
            return LifecycleOutcome(OutcomeCategory.execution_failed, task_id, "failed", "execution failed; confirmed progress retained", False)
        try:
            self.memory.update_task_state(task_id, "completed")
            self.memory.update_task_checkpoint(task_id, plan_progress=i, active_duration_s=_cumulative_s(), last_outcome="completed")
            self.memory.record_plan_fingerprint(plan_fingerprint(plan), task_id)
        except Exception:
            return LifecycleOutcome(OutcomeCategory.execution_failed, task_id, "running", "completion persistence failed", True)
        self._close_agent_tabs_best_effort(task_id, plan, n_completed)
        return LifecycleOutcome(OutcomeCategory.ok, task_id, "completed", "completed", False)

    def _close_agent_tabs_best_effort(self, task_id: str, plan, actions_executed: int) -> None:
        """Tab discipline parity: close only agent-owned tabs at task end."""
        try:
            if self._driver is None or not hasattr(self._driver, "close_all_agent_tabs"):
                return
            should_close = "close_own_tab" in (list(getattr(plan, "expected_actions", []) or [])) or actions_executed > 0
            if not should_close:
                return
            closed = self._driver.close_all_agent_tabs()  # type: ignore
            if not closed:
                return
            for ct in list(closed):
                try:
                    self.memory.record_action(uuid.uuid4().hex, task_id, "close_own_tab", ct, "allowed", "completed", "cleanup: agent tab closed at task end")
                except Exception:
                    pass
            try:
                self.memory.record_error(str(uuid.uuid4()), task_id, f"closed {len(list(closed))} agent tab(s) at task end: {list(closed)}")
            except Exception:
                pass
        except Exception:
            pass

    def _owner_returned(self) -> bool:
        try:
            det = self.idle_detector
            secs = float(det.seconds_since_last_input())
            from .profile.models import get_effective_idle_threshold_seconds
            from .profile.store import load_profile as _lp

            try:
                _p = _lp(Path(self.config.data_dir) / "profile.json")
                thr = get_effective_idle_threshold_seconds(_p, fallback=int(getattr(self.config, "idle_threshold_seconds", 600)))
            except Exception:
                thr = int(getattr(self.config, "idle_threshold_seconds", 600))
            from .idle import FakeIdleDetector

            if isinstance(det, FakeIdleDetector):
                ok, _ = det.can_run(int(thr))
                if ok:
                    return False
                if det.is_screen_locked():
                    return True
                return secs < float(thr)
            return secs < float(thr)
        except Exception:
            return False

    def _limits_hit(self, plan, profile, cumulative_s: float, task_id: str) -> str | None:
        try:
            max_min = int(getattr(self.config, "max_duration_minutes", 45))
            if profile is not None:
                try:
                    pm = getattr(getattr(profile, "autonomy_boundaries", None), "session_duration_minutes", None)
                    if pm:
                        max_min = min(max_min, int(pm))
                except Exception:
                    pass
            if plan is not None:
                try:
                    max_min = min(max_min, int(plan.max_duration_minutes))
                except Exception:
                    pass
            if cumulative_s / 60.0 >= float(max_min):
                return f"limit reached: session duration {cumulative_s/60.0:.1f}min >= cap {max_min}min"
        except Exception:
            pass
        try:
            n = len([a for a in self.memory.list_actions(task_id=task_id) if a.get("status") == "completed"])
            cap = int(getattr(self.config, "max_actions", 200))
            if n >= cap:
                return f"limit reached: actions {n} >= cap {cap}"
        except Exception:
            pass
        try:
            from .accounting import get_today_count

            llm = int(get_today_count(self.config.data_dir))
            cap_llm = int(getattr(self.config, "max_llm_calls_per_day", 150))
            if llm >= cap_llm:
                return f"limit reached: LLM calls {llm} >= daily cap {cap_llm}"
        except Exception:
            pass
        return None

    def _fail_task(self, task_id: str, state: str, cause_kind: str, message: str, extra: dict) -> None:
        try:
            self.memory.update_task_state(task_id, state)
            kw: dict[str, Any] = {}
            if cause_kind in ("cancelled", "emergency_stop", "legacy_not_queued") or cause_kind.startswith("cancel") or cause_kind.startswith("emergency") or cause_kind.startswith("legacy"):
                kw["stop_cause"] = message
            else:
                kw["failure_cause"] = message
            if "active_duration_s" in extra:
                kw["active_duration_s"] = float(extra["active_duration_s"])
            if "plan_progress" in extra:
                kw["plan_progress"] = int(extra["plan_progress"])
            kw["last_outcome"] = cause_kind
            self.memory.update_task_checkpoint(task_id, **kw)  # type: ignore[arg-type]
            self.memory.record_error(str(uuid.uuid4()), task_id, message)
        except Exception:
            pass

    def _fail_outcome(self, task_id, plan, profile, queries, urls, findings, errors, skipped, message: str) -> LifecycleOutcome:
        self._fail_task(task_id, "failed", "persistence_failed", message, {})
        self._write_report_best_effort(task_id, "", plan, queries, urls, findings, errors + [{"message": message}], skipped, profile)
        return LifecycleOutcome(OutcomeCategory.execution_failed, task_id, "failed", message, False)

    def _write_report_best_effort(self, task_id, goal, plan, queries, urls, findings, errors, skipped, profile) -> str | None:
        from .report import generate_markdown_report, save_report_to_file

        try:
            if plan is None:
                from .planner import StubPlanner

                try:
                    plan = StubPlanner().plan(goal or "task")
                except Exception:
                    from .models.plan import Plan as _P
                    from .models.plan import RiskLevel as _R

                    plan = _P(goal=goal or "task", target="google.com", expected_actions=["search"], expected_result="failed", max_duration_minutes=10, max_actions=10, risk_level=_R.low, requires_confirmation=False)
            try:
                persisted_actions = self.memory.list_actions(task_id=task_id)
            except Exception:
                persisted_actions = []
            try:
                row = self.memory.get_task(task_id)
                state_v = (row.get("state", "failed") if row else "failed")
            except Exception:
                state_v = "failed"
            try:
                from .accounting import get_today_count as _cnt

                llm_c = int(_cnt(self.config.data_dir))
            except Exception:
                llm_c = 0
            md = generate_markdown_report(
                task={"id": task_id, "description": goal, "state": state_v},
                plan=plan, queries=queries, urls=urls, findings=findings, errors=errors,
                actions=persisted_actions, skipped_repeats=skipped, profile=profile,
                limits={"actions_used": len([a for a in persisted_actions if a.get('status') == 'completed']), "max_actions": int(getattr(self.config, "max_actions", 200)), "duration_minutes": 0, "max_duration_minutes": int(getattr(self.config, "max_duration_minutes", 45)), "llm_calls_today": llm_c, "max_llm_calls_per_day": int(getattr(self.config, "max_llm_calls_per_day", 150))},
            )
            try:
                save_report_to_file(md, self.config.data_dir, task_id)
            except Exception:
                return None
            try:
                self.memory.save_report(task_id, md)
            except Exception:
                return None
            return md
        except Exception:
            return None


class LifecycleOutcomeError(Exception):
    def __init__(self, outcome: LifecycleOutcome) -> None:
        super().__init__(outcome.message)
        self.outcome = outcome
