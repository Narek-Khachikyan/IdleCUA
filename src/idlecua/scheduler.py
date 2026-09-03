"""Idle-gated scheduler — wait-for-idle polling loop and auto-start session.

Wires IdleDetector (Quartz HID hardware timer), schedule/allowlist limits,
and the policy-gated TaskExecutor into a single autonomous flow:

  idle auto-start → plan → policy → driver → history → daily Markdown report
                         → graceful stop on user return / limits / emergency stop

Spec: HID System State so synthetic never masks return; watchdog fallback
documented in idle.py; gated before any work (idle ≥ threshold, screen unlocked,
allowed hours, limits, profile confirmed); user-return halts input synchronously
and transitions to paused_by_user; auto-resume only at next idle; limits enforced
gracefully with saved report.

This module is the thin scheduler seam — no business logic duplication; it
delegates planning, policy, execution, persistence, and reporting to the
existing seams (Planner, PolicyEngine, TaskExecutor, MemoryStore, report).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .config import IdleCuaConfig
from .idle import IdleDetector
from .memory import MemoryStore


@dataclass
class GateCheck:
    ok: bool
    reason: str
    gate: str  # which gate failed / passed


class IdleScheduler:
    """Poll the hardware idle detector and launch one bounded session when gates pass."""

    def __init__(
        self,
        config: IdleCuaConfig,
        idle_detector: IdleDetector,
        memory: MemoryStore,
    ) -> None:
        self.config = config
        self.idle = idle_detector
        self.memory = memory

    # -- gate checks (pre-run, no driver calls) --

    def check_profile_gate(self) -> GateCheck:
        from .profile.store import load_profile
        from .profile.validate import validate_profile

        ppath = self.config.data_dir / "profile.json"
        profile = load_profile(ppath)
        if profile is None:
            return GateCheck(False, f"No profile at {ppath}. Run `idle-cua profile interview` and confirm.", "profile")
        if not bool(getattr(profile, "confirmed", False)):
            return GateCheck(False, f"Profile at {ppath} is unconfirmed. Complete interview and confirm.", "profile")
        errs = validate_profile(profile)
        if errs:
            return GateCheck(False, f"Profile invalid: {'; '.join(errs)}", "profile")
        return GateCheck(True, "Profile confirmed and valid.", "profile")

    def check_schedule_gate(self, profile: Any | None = None) -> GateCheck:
        if profile is None:
            try:
                from .profile.store import load_profile

                profile = load_profile(self.config.data_dir / "profile.json")
            except Exception:
                profile = None
        if profile is None:
            return GateCheck(True, "No profile — allowed hours default 24/7", "schedule")
        allowed = getattr(getattr(profile, "autonomy_boundaries", None), "allowed_hours", "00:00-23:59") or "00:00-23:59"
        from .schedule_utils import is_within_allowed_hours
        ok, _ = is_within_allowed_hours(allowed)
        if ok:
            import datetime as dt
            now = dt.datetime.now().time()
            return GateCheck(True, f"Schedule allows {allowed} (now {now})", "schedule")
        import datetime as dt
        now = dt.datetime.now().time()
        return GateCheck(False, f"Schedule blocks execution: allowed_hours {allowed} (now {now})", "schedule")

    def check_idle_gate(self, threshold_override: int | None = None) -> GateCheck:
        # ADR-0003: effective threshold from Profile if available, else Config — single source via profile helper
        if threshold_override is not None:
            thr = int(threshold_override)
        else:
            try:
                from .profile.models import get_effective_idle_threshold_seconds
                from .profile.store import load_profile as _lp

                p = _lp(self.config.data_dir / "profile.json")
                thr = get_effective_idle_threshold_seconds(p, fallback=int(getattr(self.config, "idle_threshold_seconds", 600)))
            except Exception:
                thr = int(getattr(self.config, "idle_threshold_seconds", 600))
        try:
            locked = self.idle.is_screen_locked()
            if locked:
                return GateCheck(False, "Screen is locked — agent will not run until unlocked", "screen")
            ok, reason = self.idle.can_run(thr)
            if ok:
                return GateCheck(True, reason, "idle")
            return GateCheck(False, reason, "idle")
        except Exception as e:
            return GateCheck(False, f"idle check failed: {e}", "idle")

    def check_limits_gate(self) -> GateCheck:
        from .accounting import get_today_count

        try:
            llm_today = get_today_count(self.config.data_dir)
            cap = int(self.config.max_llm_calls_per_day)
            if llm_today >= cap:
                return GateCheck(False, f"LLM daily cap reached: {llm_today}/{cap}", "limits")
        except Exception:
            pass
        return GateCheck(True, "Limits not reached", "limits")

    def check_all_gates(self, threshold_override: int | None = None) -> list[GateCheck]:
        checks: list[GateCheck] = []
        # Profile first (hard gate)
        checks.append(self.check_profile_gate())
        # Schedule
        checks.append(self.check_schedule_gate())
        # Idle + screen
        checks.append(self.check_idle_gate(threshold_override))
        # Limits
        checks.append(self.check_limits_gate())
        return checks

    def can_start(self, threshold_override: int | None = None) -> tuple[bool, str]:
        checks = self.check_all_gates(threshold_override)
        for c in checks:
            if not c.ok:
                return False, f"{c.gate}: {c.reason}"
        return True, "all gates pass"

    # -- wait-for-idle polling --

    def wait_for_idle(
        self,
        poll_interval: float = 5.0,
        timeout: float | None = None,
        threshold_override: int | None = None,
        on_tick: Callable[[int, GateCheck], None] | None = None,
    ) -> bool:
        """Poll until idle ≥ threshold and screen unlocked; honors emergency stop.

        Returns True if gates passed, False on timeout or emergency stop.
        Calls on_tick(tick, idle_gate) each poll for CLI progress.
        """
        from .executor import is_emergency_stop_requested

        start = time.monotonic()
        tick = 0
        if threshold_override is not None:
            thr = int(threshold_override)
        else:
            try:
                from .profile.models import get_effective_idle_threshold_seconds
                from .profile.store import load_profile as _lp2

                _p2 = _lp2(self.config.data_dir / "profile.json")
                thr = get_effective_idle_threshold_seconds(_p2, fallback=int(getattr(self.config, "idle_threshold_seconds", 600)))
            except Exception:
                thr = int(getattr(self.config, "idle_threshold_seconds", 600))
        while True:
            if is_emergency_stop_requested():
                return False
            idle_gate = self.check_idle_gate(thr)
            if on_tick is not None:
                try:
                    on_tick(tick, idle_gate)
                except Exception:
                    pass
            # Also need schedule + limits + profile: but idle is the primary poll; others are checked once before waiting?
            # For wait_for_idle we only block on idle/screen; profile/schedule/limits are checked by caller before/after.
            if idle_gate.ok:
                # Re-check full gates before returning (schedule may have blocked)
                ok, _ = self.can_start(threshold_override)
                if ok:
                    return True
                # If full gates fail due to non-idle gate (e.g., schedule), keep polling — schedule may become ok
                # But profile never becomes ok by polling; caller handles that
            if timeout is not None and (time.monotonic() - start) >= timeout:
                return False
            time.sleep(max(0.1, float(poll_interval)))
            tick += 1

    # -- one autonomous session (idle → plan → policy → driver → history → daily report → stop handling) --

    def run_one_session(
        self,
        task_description: str,
        *,
        is_interactive: bool = False,
        confirm_func: Callable | None = None,
        idle_detector_override=None,
        computer_override=None,
        model_provider_override=None,
    ):
        """Execute one idle-gated session and return ExecutionResult.

        Assumes caller already verified gates (or runs regardless — executor still
        enforces policy/limits/user-return). Persists to SQLite and writes daily
        Markdown report via TaskExecutor.
        """
        # Lazy import to avoid cycles
        from .app import IdleCua
        from .contracts.computer import FakeComputerDriver
        from .idle import FakeIdleDetector

        # Build an IdleCua wired to this scheduler's config/detector/memory
        idle_det = idle_detector_override if idle_detector_override is not None else self.idle
        # IdleCua will create memory from config, but we pass our memory explicitly so
        # history is shared.
        app = IdleCua(
            config=self.config,
            computer=computer_override,
            model_provider=model_provider_override,
            idle_detector=idle_det,
            memory=self.memory,
        )
        # Ensure app's idle_detector is the same object (IdleCua may default to fake)
        app.idle_detector = idle_det
        # Hard gate: profile must be confirmed — executor also checks, but we surface early
        ok, reason = self.can_start()
        if not ok:
            # Still try to run? For now refuse and raise
            raise RuntimeError(f"Cannot start session — gate failed: {reason}")
        # Delegate to app's execution path (creates task, runs through executor)
        result = app.run_task(task_description, is_interactive=is_interactive, confirm_func=confirm_func)
        return result

    def run_loop(
        self,
        task_description: str,
        *,
        poll_interval: float = 5.0,
        idle_threshold_override: int | None = None,
        max_sessions: int | None = None,
        once: bool = False,
        timeout_per_wait: float | None = None,
        on_event: Callable[[str, Any], None] | None = None,
        is_interactive: bool = False,
        confirm_func: Callable | None = None,
    ) -> list[Any]:
        """Wait-for-idle → session → repeat loop.

        - Waits for idle (polling HID timer), then runs one session (plan → policy → driver → report).
        - Handles graceful stop on user return (executor transitions to paused_by_user), limits,
          and emergency stop (LLM-independent).
        - Auto-resume only at next idle period: after a paused_by_user, loop waits again.
        - Returns list of ExecutionResults (or empty if never started).
        """
        from .executor import is_emergency_stop_requested

        results: list[Any] = []
        sessions = 0
        if idle_threshold_override is not None:
            thr = int(idle_threshold_override)
        else:
            try:
                from .profile.models import get_effective_idle_threshold_seconds
                from .profile.store import load_profile as _lp3

                _p3 = _lp3(self.config.data_dir / "profile.json")
                thr = get_effective_idle_threshold_seconds(_p3, fallback=int(getattr(self.config, "idle_threshold_seconds", 600)))
            except Exception:
                thr = int(getattr(self.config, "idle_threshold_seconds", 600))

        # Initial gate: profile must be confirmed; otherwise don't loop silently
        pg = self.check_profile_gate()
        if not pg.ok:
            raise RuntimeError(f"Scheduler refused — {pg.reason}")

        while True:
            if is_emergency_stop_requested():
                if on_event:
                    try:
                        on_event("emergency_stop", None)
                    except Exception:
                        pass
                break
            if max_sessions is not None and sessions >= max_sessions:
                break

            # Check non-idle gates (schedule/limits) without waiting; if blocked, wait a bit then recheck
            schedule_gate = self.check_schedule_gate()
            limits_gate = self.check_limits_gate()
            if not schedule_gate.ok:
                if on_event:
                    try:
                        on_event("schedule_blocked", schedule_gate.reason)
                    except Exception:
                        pass
                time.sleep(max(1.0, float(poll_interval)))
                continue
            if not limits_gate.ok:
                if on_event:
                    try:
                        on_event("limits_reached", limits_gate.reason)
                    except Exception:
                        pass
                break

            # Wait until idle (hardware timer)
            if on_event:
                try:
                    on_event("waiting_for_idle", {"threshold": thr, "poll_interval": poll_interval})
                except Exception:
                    pass

            def _tick(tick: int, gate: GateCheck) -> None:
                if on_event:
                    try:
                        on_event("idle_tick", {"tick": tick, "gate": gate})
                    except Exception:
                        pass

            got_idle = self.wait_for_idle(
                poll_interval=poll_interval,
                timeout=timeout_per_wait,
                threshold_override=thr,
                on_tick=_tick,
            )
            if not got_idle:
                if timeout_per_wait is not None:
                    if on_event:
                        try:
                            on_event("wait_timeout", None)
                        except Exception:
                            pass
                    break
                # If emergency stop broke the wait, exit; otherwise keep polling
                if is_emergency_stop_requested():
                    break
                continue

            if on_event:
                try:
                    on_event("idle_detected", None)
                except Exception:
                    pass

            # Run one autonomous session (bounded, with report)
            try:
                result = self.run_one_session(
                    task_description,
                    is_interactive=is_interactive,
                    confirm_func=confirm_func,
                )
            except Exception as e:
                if on_event:
                    try:
                        on_event("session_error", str(e))
                    except Exception:
                        pass
                # Don't tight-loop on error; wait a bit
                time.sleep(max(1.0, float(poll_interval)))
                continue

            results.append(result)
            sessions += 1
            if on_event:
                try:
                    on_event("session_completed", result)
                except Exception:
                    pass

            # If executor paused due to user return, wait for next idle before next session
            try:
                from .models.state import AgentState

                if getattr(result.state, "value", str(result.state)) == AgentState.paused_by_user.value:
                    if on_event:
                        try:
                            on_event("paused_by_user", result)
                        except Exception:
                            pass
                    # Continue loop — will wait_for_idle again (auto-resume only at next idle)
            except Exception:
                pass

            if once:
                break

            # Brief pause before next poll to avoid hammering
            time.sleep(max(0.5, float(poll_interval)))

        return results
