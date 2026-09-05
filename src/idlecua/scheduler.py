"""Idle-gated scheduler — thin Watch-loop adapter (ADR-0004, ADR-0006).

Owns only polling, timing, Watch-loop control, and the process lock.
It never selects Tasks, reads lifecycle storage, evaluates lifecycle gates,
or constructs the Application API: on each idle window it submits one
idle-triggered Start through a caller-supplied `start_fn` (which owns
selection, gating, and execution behind the TaskLifecycle seam).

Emergency stop is observed here and exits the loop without selecting more work.
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
    """Poll the hardware idle detector and fire idle-triggered Starts."""

    def __init__(
        self,
        config: IdleCuaConfig,
        idle_detector: IdleDetector,
        memory: MemoryStore,
    ) -> None:
        self.config = config
        self.idle = idle_detector
        self.memory = memory

    # -- idle primitive (polling only; lifecycle owns the gate decision) --

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

    # -- wait-for-idle polling --

    def wait_for_idle(
        self,
        poll_interval: float = 5.0,
        timeout: float | None = None,
        threshold_override: int | None = None,
        on_tick: Callable[[int, GateCheck], None] | None = None,
    ) -> bool:
        """Poll until idle ≥ threshold and screen unlocked; honors emergency stop.

        Returns True if the idle window opened, False on timeout or emergency stop.
        Full lifecycle gates are evaluated by the TaskLifecycle Start, not here.
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
            if idle_gate.ok:
                return True
            if timeout is not None and (time.monotonic() - start) >= timeout:
                return False
            time.sleep(max(0.1, float(poll_interval)))
            tick += 1

    # -- Watch loop: poll, then submit one idle-triggered Start per window --

    def run_loop(
        self,
        *,
        start_fn: Callable[[], Any],
        poll_interval: float = 5.0,
        idle_threshold_override: int | None = None,
        max_sessions: int | None = None,
        once: bool = False,
        timeout_per_wait: float | None = None,
        on_event: Callable[[str, Any], None] | None = None,
    ) -> list[Any]:
        """Wait-for-idle → idle-triggered Start → repeat loop.

        `start_fn` sends one `Start(trigger="idle", mode="unattended")` to the
        lifecycle owner and returns its structured outcome. This loop never
        selects Tasks, reads lifecycle storage, or evaluates lifecycle gates.
        Returns the outcomes of fired Starts.
        """
        from .executor import is_emergency_stop_requested

        if start_fn is None:
            raise ValueError("start_fn is required: the Watch loop only submits Starts")
        results: list[Any] = []
        sessions = 0
        if idle_threshold_override is not None:
            thr: int | None = int(idle_threshold_override)
        else:
            try:
                from .profile.models import get_effective_idle_threshold_seconds
                from .profile.store import load_profile as _lp3

                _p3 = _lp3(self.config.data_dir / "profile.json")
                thr = get_effective_idle_threshold_seconds(_p3, fallback=int(getattr(self.config, "idle_threshold_seconds", 600)))
            except Exception:
                thr = int(getattr(self.config, "idle_threshold_seconds", 600))

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
                # Emergency stop broke the wait — exit without selecting work.
                if is_emergency_stop_requested():
                    break
                continue

            if on_event:
                try:
                    on_event("idle_detected", None)
                except Exception:
                    pass

            # One idle-triggered Start; the lifecycle owner selects and claims work.
            try:
                from .task_lifecycle import OutcomeCategory as _Cat

                outcome = start_fn()
                cat = getattr(outcome, "category", None)
                if cat in (_Cat.ok, _Cat.stopped):
                    results.append(outcome)
                    sessions += 1
                    if on_event:
                        try:
                            on_event("session_completed", outcome)
                        except Exception:
                            pass
                    if getattr(outcome, "state", None) == "paused_by_user":
                        if on_event:
                            try:
                                on_event("paused_by_user", outcome)
                            except Exception:
                                pass
                    if once:
                        break
                    time.sleep(max(0.5, float(poll_interval)))
                    continue
                # No Session started — never counted; back off and keep polling.
                fg = getattr(outcome, "failed_gate", None)
                msg = str(getattr(outcome, "message", "") or "")
                if on_event:
                    try:
                        if cat == _Cat.not_ready and fg == "limits":
                            on_event("limits_reached", msg)
                        elif cat == _Cat.not_ready and fg == "schedule":
                            on_event("schedule_blocked", msg)
                        else:
                            on_event("session_error", msg)
                    except Exception:
                        pass
                if cat == _Cat.not_ready and fg == "limits":
                    break
                time.sleep(max(1.0, float(poll_interval)))
                continue
            except Exception as e:
                if on_event:
                    try:
                        on_event("session_error", str(e))
                    except Exception:
                        pass
                # Don't tight-loop on error; wait a bit
                time.sleep(max(1.0, float(poll_interval)))
                continue

        return results
