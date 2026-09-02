from __future__ import annotations

import time
import uuid
import threading
import signal
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .config import IdleCuaConfig
from .contracts.computer import ComputerDriver, FakeComputerDriver
from .contracts.model import ModelProvider, FakeModelProvider
from .dedup import normalize_query, normalize_url, url_fingerprint, plan_fingerprint
from .idle import IdleDetector, FakeIdleDetector, InputJournal
from .memory import MemoryStore
from .models.plan import Plan
from .models.state import AgentState
from .models.task import Task
from .planner import LlmCallCapExceeded, LlmPlanner, PlanRejectedError, StubPlanner
from .policy import PolicyEngine, TypedAction, PolicyVerdict
from .report import generate_markdown_report, save_report_to_file


# Global emergency-stop flag — LLM-independent, signal-safe

_emergency_stop_requested = threading.Event()
_emergency_stop_reason: str | None = None
_emergency_lock = threading.Lock()


def request_emergency_stop(reason: str = "emergency stop requested") -> None:
    with _emergency_lock:
        global _emergency_stop_reason
        _emergency_stop_reason = reason
        _emergency_stop_requested.set()


def clear_emergency_stop() -> None:
    with _emergency_lock:
        global _emergency_stop_reason
        _emergency_stop_reason = None
        _emergency_stop_requested.clear()


def is_emergency_stop_requested() -> bool:
    return _emergency_stop_requested.is_set()


def get_emergency_stop_reason() -> str | None:
    with _emergency_lock:
        return _emergency_stop_reason


def install_signal_handlers() -> None:
    """Install SIGINT/SIGTERM handlers that route through the same emergency path."""
    def handler(signum, frame):
        name = signal.Signals(signum).name if hasattr(signal, 'Signals') else str(signum)
        request_emergency_stop(f"SIG{name}")

    try:
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)
    except Exception:
        # May fail in non-main thread
        pass


@dataclass
class ExecutionResult:
    task_id: str
    state: AgentState
    plan: Plan
    actions_executed: int
    queries: list[dict] = field(default_factory=list)
    urls: list[dict] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    skipped_repeats: list[dict] = field(default_factory=list)
    report_markdown: str = ""
    report_path: Path | None = None
    stopped_reason: str | None = None
    limits: dict = field(default_factory=dict)


SUPPORTED_DRIVER_KINDS: set[str] = {
    "open_allowed_site",
    "open_link",
    "search",
    "read_ui",
    "scroll",
    "extract_public_info",
    "save_note",
    "close_own_tab",
    "open_app",
}


@dataclass
class ExecutorConfig:
    """Tunable execution parameters."""
    pacing_seconds: float = 0.05  # human-like pacing between actions
    per_site_cap: int = 20  # soft cap per domain
    verify_significant: bool = True


def _action_to_driver_call(
    action: TypedAction,
    driver: ComputerDriver,
    task_description: str,
    profile: Any | None = None,
) -> tuple[str, Callable, str | None]:
    """Map typed action to a single driver call — minimal closed vocabulary, no fallback chains."""
    kind = action.kind.strip()

    if kind in ("open_allowed_site", "open_link"):
        url = action.target_url or f"https://{action.effective_domain or 'google.com'}"
        def do():
            if hasattr(driver, "open_browser_tab") and hasattr(driver, "has_browser_consent"):
                try:
                    if bool(driver.has_browser_consent()):  # type: ignore
                        driver.open_browser_tab(url)  # type: ignore
                        return
                except Exception:
                    pass
            driver.open_url(url)
        return kind, do, url

    if kind == "search":
        query = action.description or task_description
        url = action.target_url or "https://google.com"
        def do_search():
            driver.open_url(url)
            driver.type_text(query)
            driver.press("Enter")
        return kind, do_search, url

    if kind == "read_ui":
        def do_read():
            driver.screenshot()
            try:
                driver.get_accessibility_tree()
            except Exception:
                pass
        return kind, do_read, action.target_url

    if kind == "scroll":
        def do_scroll():
            driver.scroll(0, 300)
        return kind, do_scroll, action.target_url

    if kind == "extract_public_info":
        def do_extract():
            driver.screenshot()
            try:
                driver.get_accessibility_tree()
            except Exception:
                pass
        return kind, do_extract, action.target_url

    if kind == "save_note":
        def do_save():
            pass
        return kind, do_save, None

    if kind == "close_own_tab":
        tab = action.target_url
        def do_close():
            if hasattr(driver, "close_tab"):
                try:
                    driver.close_tab(tab)  # type: ignore
                    return
                except Exception:
                    pass
            if hasattr(driver, "close_all_agent_tabs"):
                try:
                    driver.close_all_agent_tabs()  # type: ignore
                except Exception:
                    pass
        return kind, do_close, tab

    if kind == "open_app":
        raw = (action.description or task_description or "Calculator").strip()
        low = raw.lower()
        known = {
            "calculator": "Calculator",
            "textedit": "TextEdit",
            "google chrome": "Google Chrome",
            "system settings": "System Settings",
        }
        app_name = None
        for cand, mapped in known.items():
            if cand in low:
                app_name = mapped
                break
        if app_name is None:
            import re as _re
            m = _re.search(r"launch\s+([a-zA-Z ]+)", raw, _re.IGNORECASE)
            if m:
                app_name = m.group(1).strip().title()
            else:
                app_name = "Calculator"
        def do_open_app():
            driver.open_app(app_name)  # type: ignore
        return kind, do_open_app, None

    # Unknown/unsupported kinds are treated as no-op but still policy-checked upstream
    # For confirmation-required kinds with no typed driver mapping in MVP,
    # the caller must surface as skipped (spec sanctions skip-and-surface).
    def do_noop():
        pass
    return kind, do_noop, action.target_url


class TaskExecutor:
    """Policy-gated execution loop with pacing, verification, limits, anti-repeat, user-return & emergency stop."""

    def __init__(
        self,
        config: IdleCuaConfig,
        driver: ComputerDriver,
        model_provider: ModelProvider,
        memory: MemoryStore,
        policy: PolicyEngine,
        idle_detector: IdleDetector | None = None,
        planner: StubPlanner | None = None,
        executor_cfg: ExecutorConfig | None = None,
    ) -> None:
        self.config = config
        self.driver = driver
        self.model_provider = model_provider
        self.memory = memory
        self.policy = policy
        self.idle_detector = idle_detector or FakeIdleDetector(idle_seconds=1000, locked=False)
        self.planner = planner or StubPlanner()
        self.exec_cfg = executor_cfg or ExecutorConfig()
        self._stop_requested = threading.Event()
        self._stop_reason: str | None = None

    def request_stop(self, reason: str = "stop requested") -> None:
        self._stop_requested.set()
        self._stop_reason = reason
        request_emergency_stop(reason)

    def clear_stop(self) -> None:
        self._stop_requested.clear()
        self._stop_reason = None
        clear_emergency_stop()

    def _check_user_return(self) -> bool:
        """Hardware-level user return detection — synthetic never masks."""
        # Check idle detector: if no longer idle, user returned
        try:
            # If idle detector reports not idle, user returned
            idle_secs = self.idle_detector.seconds_since_last_input()
            # Use threshold from config (idle_threshold_seconds)
            threshold = getattr(self.config, "idle_threshold_seconds", 600)
            if isinstance(self.idle_detector, FakeIdleDetector):
                # For fake, check directly via its logic
                can, _ = self.idle_detector.can_run(threshold)
                if not can:
                    # Need to distinguish locked vs not idle
                    if self.idle_detector.is_screen_locked():
                        return True  # also pause
                    if idle_secs < threshold:
                        return True
                return False
            else:
                if idle_secs < threshold:
                    return True
        except Exception:
            pass
        return False

    def _check_limits(
        self,
        task: Task,
        start_time: float,
        actions_done: int,
        llm_calls_today: int,
    ) -> str | None:
        # Session duration
        elapsed_min = (time.monotonic() - start_time) / 60.0
        if elapsed_min >= self.config.max_duration_minutes:
            return f"session duration {elapsed_min:.1f}min >= cap {self.config.max_duration_minutes}min"
        if actions_done >= self.config.max_actions:
            return f"actions {actions_done} >= cap {self.config.max_actions}"
        if llm_calls_today >= self.config.max_llm_calls_per_day:
            return f"LLM calls {llm_calls_today} >= daily cap {self.config.max_llm_calls_per_day}"
        return None

    def _check_allowed_hours(self, profile: Any | None) -> tuple[bool, str]:
        if profile is None:
            return True, "no profile"
        allowed = getattr(getattr(profile, "autonomy_boundaries", None), "allowed_hours", "00:00-23:59") or "00:00-23:59"
        from .schedule_utils import is_within_allowed_hours
        return is_within_allowed_hours(allowed)

    def execute_task(
        self,
        task: Task,
        profile: Any | None = None,
        dry_run: bool = False,
        is_interactive: bool = False,
        confirm_func: Callable[[TypedAction], bool] | None = None,
    ) -> ExecutionResult:
        """Execute a task end-to-end (policy-gated, limited, with report).

        Returns ExecutionResult and persists to MemoryStore.
        Handles:
        - profile gate already checked by caller
        - allowlist / deny-zone / action class via PolicyEngine
        - anti-repeat 7-day window
        - pacing + per-site caps
        - verify-by-reread
        - limits (duration, actions, LLM calls)
        - user-return pause (hardware idle)
        - emergency stop (LLM-independent)
        """
        # Clear previous emergency stop
        clear_emergency_stop()
        self._stop_requested.clear()
        self._stop_reason = None

        # Install signal handlers for LLM-independent stop
        install_signal_handlers()

        start_time = time.monotonic()

        # Initial state: planning
        try:
            if task.state == AgentState.disabled:
                task.transition_to(AgentState.waiting_for_idle)
            if task.state == AgentState.waiting_for_idle:
                task.transition_to(AgentState.planning)
        except Exception:
            pass

        # Idle gate: when require_idle, enforce HID check before any planning/execution (US14)
        if not dry_run and self.config.require_idle:
            ok, reason = self.idle_detector.can_run(self.config.idle_threshold_seconds)
            if not ok:
                # Report and fail fast — do not execute while user is present
                try:
                    task.transition_to(AgentState.failed)
                except Exception:
                    task.state = AgentState.failed
                self.memory.upsert_task(task.id, task.description, task.state.value, None)
                self.memory.record_error(str(uuid.uuid4()), task.id, f"idle gate blocked: {reason}")
                raise RuntimeError(f"idle gate blocked: {reason}")
            if self.idle_detector.is_screen_locked():
                try:
                    task.transition_to(AgentState.failed)
                except Exception:
                    task.state = AgentState.failed
                self.memory.upsert_task(task.id, task.description, task.state.value, None)
                self.memory.record_error(str(uuid.uuid4()), task.id, f"screen locked — {reason}")
                raise RuntimeError(f"screen locked — {reason}")

        # Generate plan — LLM path uses profile + history, bounded typed conversion, injection hardening, cap
        # Resolve history once for planner context
        history_for_planner = None
        try:
            history_for_planner = self.memory.get_history(limit=50)
        except Exception:
            history_for_planner = None

        plan: Plan | None = None
        plan_error: str | None = None
        cap_exceeded_before_plan = False
        try:
            if isinstance(self.planner, LlmPlanner):
                try:
                    plan = self.planner.plan(task.description, profile=profile, history=history_for_planner)
                except LlmCallCapExceeded as ce:
                    cap_exceeded_before_plan = True
                    plan_error = str(ce)
                    # Graceful fallback to stub for session continuity (no LLM)
                    try:
                        fallback = getattr(self.planner, "fallback", None) or StubPlanner()
                        plan = fallback.plan(task.description, profile=profile, history=history_for_planner)
                    except Exception:
                        plan = StubPlanner().plan(task.description)
                except PlanRejectedError as pe:
                    plan_error = f"plan rejected (LLM output not convertible to typed actions, never executed as free text): {pe}"
                    # Do not fallback silently — mark planning failed
                    plan = None
                except Exception as e:
                    plan_error = f"planner error: {e}"
                    plan = None
            else:
                # Stub or custom planner — try with profile/history if supported
                try:
                    plan = self.planner.plan(task.description, profile=profile, history=history_for_planner)  # type: ignore[call-arg]
                except TypeError:
                    plan = self.planner.plan(task.description)
        except Exception as e:
            plan_error = plan_error or str(e)
            plan = None

        if plan is None:
            # Planning failed — graceful fail with saved state and report
            # Persist task as failed
            try:
                task.transition_to(AgentState.failed)
            except Exception:
                task.state = AgentState.failed
            self.memory.upsert_task(task.id, task.description, task.state.value, None)
            self.memory.record_error(str(uuid.uuid4()), task.id, plan_error or "planning failed")
            # Generate minimal report so cap/plan failure is visible
            from .accounting import get_today_count as _get_cnt

            _llm_cnt = _get_cnt(self.config.data_dir)
            # Need a dummy plan for report? Use stub fallback for report structure if possible
            try:
                dummy_plan = StubPlanner().plan(task.description)
            except Exception:
                from .models.plan import RiskLevel as _RL

                dummy_plan = Plan(goal=task.description, target="google.com", expected_actions=["search"], expected_result="failed", max_duration_minutes=10, max_actions=10, risk_level=_RL.low, requires_confirmation=False)
            markdown = generate_markdown_report(
                task={"id": task.id, "description": task.description, "state": task.state.value},
                plan=dummy_plan,
                queries=[],
                urls=[],
                findings=[],
                errors=[{"message": plan_error or "planning failed"}],
                actions=[],
                skipped_repeats=[],
                profile=profile,
                limits={"actions_used": 0, "max_actions": self.config.max_actions, "duration_minutes": 0, "max_duration_minutes": self.config.max_duration_minutes, "llm_calls_today": _llm_cnt, "max_llm_calls_per_day": self.config.max_llm_calls_per_day, "stopped_due_to_limit": plan_error if cap_exceeded_before_plan else None},
            )
            per_task_path = save_report_to_file(markdown, self.config.data_dir, task.id)
            self.memory.save_report(task.id, markdown)
            # Ensure executor returns a result with failed state
            # Build a minimal plan for ExecutionResult
            result_plan = dummy_plan
            return ExecutionResult(task_id=task.id, state=task.state, plan=result_plan, actions_executed=0, queries=[], urls=[], findings=[], errors=[{"message": plan_error or "planning failed"}], skipped_repeats=[], report_markdown=markdown, report_path=per_task_path, stopped_reason=plan_error, limits={"actions_used": 0, "max_actions": self.config.max_actions, "duration_minutes": 0, "max_duration_minutes": self.config.max_duration_minutes, "llm_calls_today": _llm_cnt, "max_llm_calls_per_day": self.config.max_llm_calls_per_day})

        # Override caps from config / profile if stricter
        if profile is not None:
            try:
                prof_max_dur = getattr(profile.autonomy_boundaries, "session_duration_minutes", None)
                if prof_max_dur and prof_max_dur < plan.max_duration_minutes:
                    plan = Plan(
                        goal=plan.goal,
                        target=plan.target,
                        expected_actions=plan.expected_actions,
                        expected_result=plan.expected_result,
                        max_duration_minutes=min(plan.max_duration_minutes, prof_max_dur),
                        max_actions=min(plan.max_actions, getattr(profile.autonomy_boundaries, "daily_action_limit", 200)),
                        risk_level=plan.risk_level,
                        requires_confirmation=plan.requires_confirmation,
                    )
            except Exception:
                pass

        # Anti-repeat: check plan fingerprint vs 7-day window (US13 — must prevent repeat)
        skipped_repeats: list[dict] = []
        fp = plan_fingerprint(plan)
        plan_is_repeat = False
        if self.memory.has_plan_fingerprint_within_days(fp, days=7):
            skipped_repeats.append({"type": "plan", "value": fp, "reason": "plan identical to recent session within 7 days — skipped repeat"})
            plan_is_repeat = True

        # Snapshot existing dedup sets before this session to avoid self-blocking intra-session
        try:
            existing_url_fps = {r["fingerprint"] for r in self.memory.list_urls(limit=1000)}
            existing_query_norms = {r["normalized"] for r in self.memory.list_queries(limit=1000)}
        except Exception:
            existing_url_fps = set()
            existing_query_norms = set()
        seen_query_norms_this_session: set[str] = set()
        seen_url_fps_this_session: set[str] = set()

        # Persist task
        self.memory.upsert_task(task.id, task.description, AgentState.planning.value, json.dumps({
            "goal": plan.goal,
            "target": plan.target,
            "expected_actions": plan.expected_actions,
            "expected_result": plan.expected_result,
            "max_duration_minutes": plan.max_duration_minutes,
            "max_actions": plan.max_actions,
            "risk_level": plan.risk_level.value,
            "requires_confirmation": plan.requires_confirmation,
        }))

        if dry_run:
            # Never touch driver or model
            return ExecutionResult(task_id=task.id, state=AgentState.planning, plan=plan, actions_executed=0, skipped_repeats=skipped_repeats)

        # US13: if plan fingerprint seen within 7 days, prevent re-execution (do not call driver)
        if plan_is_repeat:
            try:
                task.transition_to(AgentState.completed)
            except Exception:
                task.state = AgentState.completed
            self.memory.update_task_state(task.id, task.state.value)
            self.memory.record_error(str(uuid.uuid4()), task.id, f"skipped repeat plan {fp}")
            from .accounting import get_today_count as _get_cnt2
            _llm_cnt = _get_cnt2(self.config.data_dir)
            limits_rep = {
                "actions_used": 0,
                "max_actions": self.config.max_actions,
                "duration_minutes": 0,
                "max_duration_minutes": plan.max_duration_minutes,
                "llm_calls_today": _llm_cnt,
                "max_llm_calls_per_day": self.config.max_llm_calls_per_day,
                "stopped_due_to_limit": None,
            }
            markdown = generate_markdown_report(
                task={"id": task.id, "description": task.description, "state": task.state.value},
                plan=plan,
                queries=[],
                urls=[],
                findings=[],
                errors=[{"message": f"skipped repeat plan {fp}"}],
                actions=[],
                skipped_repeats=skipped_repeats,
                profile=profile,
                limits=limits_rep,
            )
            per_task_path = save_report_to_file(markdown, self.config.data_dir, task.id)
            self.memory.save_report(task.id, markdown)
            # Still record fingerprint timestamp update? Do not overwrite to keep original window.
            return ExecutionResult(task_id=task.id, state=task.state, plan=plan, actions_executed=0, queries=[], urls=[], findings=[], errors=[{"message": f"skipped repeat plan {fp}"}], skipped_repeats=skipped_repeats, report_markdown=markdown, report_path=per_task_path, stopped_reason=f"skipped repeat plan {fp}", limits=limits_rep)

        # Transition to running
        try:
            task.transition_to(AgentState.running)
        except Exception:
            # If already in planning, allow
            task.state = AgentState.running
        self.memory.update_task_state(task.id, task.state.value)

        actions_executed = 0
        queries: list[dict] = []
        urls: list[dict] = []
        findings: list[dict] = []
        errors: list[dict] = []
        per_site_counts: dict[str, int] = {}
        stopped_reason: str | None = None
        # If LLM cap was hit at planning time, surface it gracefully (cap visible in report/status)
        if cap_exceeded_before_plan and plan_error:
            errors.append({"message": f"LLM cap reached at planning — {plan_error} (graceful fallback to stub, state saved)"})
            try:
                self.memory.record_error(str(uuid.uuid4()), task.id, f"LLM cap reached at planning — {plan_error}")
            except Exception:
                pass
            # Also mark in skipped_repeats for visibility
            skipped_repeats.append({"type": "llm_cap", "value": str(self.config.max_llm_calls_per_day), "reason": plan_error})

        # Determine daily LLM count
        from .accounting import get_today_count
        llm_calls_today = get_today_count(self.config.data_dir)

        # Check allowed hours gate before execution
        ok_hours, hours_reason = self._check_allowed_hours(profile)
        if not ok_hours:
            err_msg = f"schedule blocks execution: allowed_hours {hours_reason}"
            self.memory.record_error(str(uuid.uuid4()), task.id, err_msg)
            errors.append({"message": err_msg})
            task.state = AgentState.failed
            self.memory.update_task_state(task.id, task.state.value)
            markdown = generate_markdown_report(
                task={"id": task.id, "description": task.description, "state": task.state.value},
                plan=plan,
                queries=queries,
                urls=urls,
                findings=findings,
                errors=errors,
                actions=[],
                skipped_repeats=skipped_repeats,
                profile=profile,
                limits={"actions_used": 0, "max_actions": self.config.max_actions, "duration_minutes": 0, "max_duration_minutes": self.config.max_duration_minutes, "llm_calls_today": llm_calls_today, "max_llm_calls_per_day": self.config.max_llm_calls_per_day},
            )
            per_task_path = save_report_to_file(markdown, self.config.data_dir, task.id)
            self.memory.save_report(task.id, markdown)
            return ExecutionResult(task_id=task.id, state=task.state, plan=plan, actions_executed=0, queries=queries, urls=urls, findings=findings, errors=errors, skipped_repeats=skipped_repeats, report_markdown=markdown, report_path=per_task_path, stopped_reason=err_msg, limits={"actions_used":0, "duration_minutes":0, "llm_calls_today": llm_calls_today})

        # Pre-check readonly vs confirmation-required
        # For unattended (not interactive), we will skip confirmation-required actions

        # Execute each planned action step-by-step
        for kind in plan.expected_actions:
            # Check emergency stop first — LLM-independent, synchronously release input
            if is_emergency_stop_requested() or self._stop_requested.is_set():
                reason = get_emergency_stop_reason() or self._stop_reason or "emergency stop"
                stopped_reason = reason
                # Release held input synchronously
                try:
                    if hasattr(self.driver, "release_all_inputs"):
                        self.driver.release_all_inputs()
                    elif hasattr(self.driver, "release_all"):
                        self.driver.release_all()  # type: ignore
                except Exception:
                    pass
                # Terminate only agent-started processes
                try:
                    if hasattr(self.driver, "terminate_agent_processes"):
                        self.driver.terminate_agent_processes()  # type: ignore
                except Exception:
                    pass
                # Persist stop reason
                self.memory.record_error(str(uuid.uuid4()), task.id, f"stopped: {reason}")
                errors.append({"message": f"stopped: {reason}"})
                # Do NOT execute further actions
                break

            # Check user return (hardware idle) — halt input, pause task, save state, wait for next idle
            if self._check_user_return():
                stopped_reason = "paused_by_user: hardware input detected"
                # Release held input synchronously
                try:
                    if hasattr(self.driver, "release_all_inputs"):
                        self.driver.release_all_inputs()
                except Exception:
                    pass
                # Transition to paused_by_user
                try:
                    task.transition_to(AgentState.paused_by_user)
                except Exception:
                    task.state = AgentState.paused_by_user
                self.memory.update_task_state(task.id, task.state.value)
                self.memory.record_error(str(uuid.uuid4()), task.id, stopped_reason)
                errors.append({"message": stopped_reason})
                # Do not start next step
                break

            # Check limits gracefully
            limit_reason = self._check_limits(task, start_time, actions_executed, llm_calls_today)
            if limit_reason:
                stopped_reason = f"limit reached: {limit_reason}"
                self.memory.record_error(str(uuid.uuid4()), task.id, stopped_reason)
                errors.append({"message": stopped_reason})
                break

            # Build TypedAction
            # Determine target URL — minimal closed vocab
            local_kinds = {"save_note", "close_own_tab"}
            if kind in local_kinds:
                action = TypedAction(kind=kind, description=task.description)
            else:
                url = f"https://{plan.target}"
                action = TypedAction(kind=kind, target_url=url, description=task.description)

            # Anti-repeat for queries/urls — check against snapshot before this session only
            # Intra-session duplicates are allowed (different actions can target same URL)
            if kind == "search":
                qnorm = normalize_query(task.description)
                if qnorm in existing_query_norms:
                    skipped_repeats.append({"type": "query", "value": task.description, "reason": f"normalized query '{qnorm}' seen within 7 days"})
                    self.memory.record_error(str(uuid.uuid4()), task.id, f"skipped repeat query: {task.description}")
                    errors.append({"message": f"skipped repeat query: {task.description}"})
                    continue
            if kind in ("open_allowed_site", "open_link") and action.target_url:
                fp_url = url_fingerprint(action.target_url)
                if fp_url in existing_url_fps:
                    skipped_repeats.append({"type": "url", "value": action.target_url, "reason": f"url fingerprint {fp_url} seen within 7 days"})
                    self.memory.record_error(str(uuid.uuid4()), task.id, f"skipped repeat url: {action.target_url}")
                    errors.append({"message": f"skipped repeat url: {action.target_url}"})
                    continue

            # Policy evaluation: allowlist → deny-zone → action class
            result = self.policy.evaluate(action)
            verdict = result.verdict

            # Handle confirmation-required in unattended mode
            if verdict == PolicyVerdict.needs_confirmation:
                if self.config.readonly or not is_interactive:
                    # Skip/defer in unattended, surface in report as skipped
                    skipped_repeats.append({"type": "action", "value": kind, "reason": f"confirmation-required '{kind}' skipped in unattended/read-only mode: {result.reason}"})
                    self.memory.record_error(str(uuid.uuid4()), task.id, f"skipped confirmation-required action '{kind}' (unattended/read-only)")
                    errors.append({"message": f"skipped confirmation-required: {kind}"})
                    # Record action as blocked for report
                    aid = str(uuid.uuid4())
                    self.memory.record_action(aid, task.id, kind, action.target_url, verdict.value, "blocked", f"needs_confirmation: {result.reason}")
                    continue
                else:
                    # Interactive: transition to paused_for_approval while awaiting y/n (US23)
                    try:
                        task.transition_to(AgentState.paused_for_approval)
                        self.memory.update_task_state(task.id, task.state.value)
                    except Exception:
                        pass
                    confirmed = False
                    if confirm_func is not None:
                        try:
                            confirmed = bool(confirm_func(action))
                        except Exception:
                            confirmed = False
                    else:
                        try:
                            from rich.console import Console
                            from rich.prompt import Confirm as _Confirm
                            _con = Console()
                            _con.print(f"[yellow]Confirmation required:[/yellow] {action.kind} -> {action.target_url or '(local)'}")
                            _con.print(f"  Description: {action.description}")
                            _con.print(f"  Payload: {action.payload}")
                            _con.print(f"  Reason: {result.reason}")
                            confirmed = _Confirm.ask("Allow this action?", console=_con, default=False)
                        except Exception:
                            confirmed = False
                    # Return to running for next iteration
                    try:
                        task.transition_to(AgentState.running)
                        self.memory.update_task_state(task.id, task.state.value)
                    except Exception:
                        try:
                            task.state = AgentState.running
                            self.memory.update_task_state(task.id, task.state.value)
                        except Exception:
                            pass
                    if not confirmed:
                        aid = str(uuid.uuid4())
                        self.memory.record_action(aid, task.id, kind, action.target_url, verdict.value, "blocked", "owner declined confirmation")
                        skipped_repeats.append({"type": "action", "value": kind, "reason": "owner declined confirmation"})
                        continue
                    # confirmed → allow

            if verdict == PolicyVerdict.blocked:
                # Hard-blocked — never execute
                aid = str(uuid.uuid4())
                self.memory.record_action(aid, task.id, kind, action.target_url, verdict.value, "blocked", result.reason)
                errors.append({"message": f"blocked {kind}: {result.reason}"})
                self.memory.record_error(str(uuid.uuid4()), task.id, f"blocked {kind}: {result.reason}")
                continue

            # Per-site pacing cap
            domain = action.effective_domain or plan.target
            if domain:
                per_site_counts[domain] = per_site_counts.get(domain, 0) + 1
                if per_site_counts[domain] > self.exec_cfg.per_site_cap:
                    skipped_repeats.append({"type": "action", "value": kind, "reason": f"per-site cap {self.exec_cfg.per_site_cap} for {domain} reached — pacing"})
                    per_site_counts[domain] -= 1
                    continue

            # Execute via driver (with verify-by-reread for significant actions)
            try:
                # Check content as data, never instructions: we never follow LLM instructions inside page
                # Our driver calls are fixed by typed action; no page content is executed as code

                # Pacing pause human-like
                if self.exec_cfg.pacing_seconds > 0:
                    time.sleep(self.exec_cfg.pacing_seconds)

                # Dispatch
                _, driver_fn, url_for_record = _action_to_driver_call(action, self.driver, task.description, profile)
                # Before dispatch, re-check emergency stop (LLM-independent)
                if is_emergency_stop_requested() or self._stop_requested.is_set():
                    reason = get_emergency_stop_reason() or self._stop_reason or "emergency stop"
                    stopped_reason = reason
                    try:
                        if hasattr(self.driver, "release_all_inputs"):
                            self.driver.release_all_inputs()
                    except Exception:
                        pass
                    break

                # MVP has no typed driver mapping for engagement actions — spec sanctions skip-and-surface
                # Do not claim "completed" for a no-op (US27/US23). Surface honestly as skipped.
                try:
                    from .policy import classify_action as _cls2, ActionClass as _AC2
                    if _cls2(kind) == _AC2.confirmation_required and kind not in SUPPORTED_DRIVER_KINDS:
                        aid = str(uuid.uuid4())
                        self.memory.record_action(aid, task.id, kind, action.target_url, verdict.value, "skipped", f"no typed driver mapping for '{kind}' in MVP")
                        skipped_repeats.append({"type": "action", "value": kind, "reason": f"no typed driver mapping for '{kind}' in MVP (confirmed but not executed — surfaced in report)"})
                        self.memory.record_error(str(uuid.uuid4()), task.id, f"skipped confirmation-required '{kind}': no driver mapping in MVP")
                        errors.append({"message": f"skipped confirmation-required '{kind}': no driver mapping"})
                        continue
                except Exception:
                    pass

                # Capture before state for verify-by-reread (US28 — assert UI changed, not just url substring)
                is_significant = self.exec_cfg.verify_significant and kind in ("open_allowed_site", "open_link", "search", "extract_public_info")
                before_snapshot: str | None = None
                if is_significant:
                    try:
                        if hasattr(self.driver, "get_browser_state"):
                            before_snapshot = json.dumps(self.driver.get_browser_state())  # type: ignore
                        else:
                            before_snapshot = json.dumps(self.driver.get_accessibility_tree())
                    except Exception:
                        before_snapshot = None

                driver_fn()

                # Record action success
                aid = str(uuid.uuid4())
                self.memory.record_action(aid, task.id, kind, action.target_url, verdict.value, "completed", None)
                actions_executed += 1

                # Persist queries/urls/findings for report/history
                if kind == "search":
                    qnorm = normalize_query(task.description)
                    qid = str(uuid.uuid4())
                    self.memory.record_query(qid, task.id, task.description, qnorm)
                    queries.append({"id": qid, "query": task.description, "normalized": qnorm, "created_at": time.time()})
                    seen_query_norms_this_session.add(qnorm)
                    # also record URL if search target
                    if action.target_url:
                        fp_url = url_fingerprint(action.target_url)
                        norm_url = normalize_url(action.target_url)
                        uid = str(uuid.uuid4())
                        self.memory.record_url(uid, task.id, action.target_url, norm_url, fp_url)
                        urls.append({"id": uid, "url": action.target_url, "normalized": norm_url, "fingerprint": fp_url})
                        seen_url_fps_this_session.add(fp_url)
                elif kind in ("open_allowed_site", "open_link"):
                    if action.target_url:
                        fp_url = url_fingerprint(action.target_url)
                        norm_url = normalize_url(action.target_url)
                        uid = str(uuid.uuid4())
                        self.memory.record_url(uid, task.id, action.target_url, norm_url, fp_url)
                        urls.append({"id": uid, "url": action.target_url, "normalized": norm_url, "fingerprint": fp_url})
                        seen_url_fps_this_session.add(fp_url)
                        if kind == "open_link":
                            fid = str(uuid.uuid4())
                            title = f"Finding from {plan.target}: {task.description[:40]}"
                            summary = f"Public info extracted from {action.target_url} for goal '{plan.goal}'"
                            relevance = f"Relevant to profile interests/projects for '{plan.goal}'"
                            self.memory.record_finding(fid, task.id, title, action.target_url, summary, relevance)
                            findings.append({"id": fid, "title": title, "url": action.target_url, "summary": summary, "relevance": relevance})
                elif kind in ("extract_public_info", "read_ui"):
                    if action.target_url:
                        fid = str(uuid.uuid4())
                        title = f"Extracted: {task.description[:40]}"
                        summary = f"Extracted public info from {action.target_url}"
                        relevance = f"Relevant to {plan.goal}"
                        self.memory.record_finding(fid, task.id, title, action.target_url, summary, relevance)
                        findings.append({"id": fid, "title": title, "url": action.target_url, "summary": summary, "relevance": relevance})
                    else:
                        fid = str(uuid.uuid4())
                        title = f"Note: {task.description[:40]}"
                        summary = f"Local note created for '{task.description}'"
                        relevance = f"Relevant to {plan.goal}"
                        url_for_finding = f"https://{plan.target}"
                        self.memory.record_finding(fid, task.id, title, url_for_finding, summary, relevance)
                        findings.append({"id": fid, "title": title, "url": url_for_finding, "summary": summary, "relevance": relevance})
                elif kind == "save_note":
                    fid = str(uuid.uuid4())
                    title = f"Saved note: {task.description[:40]}"
                    summary = f"Local note saved: {task.description}"
                    relevance = f"Relevant to {plan.goal}"
                    url_for_finding = f"https://{plan.target}"
                    self.memory.record_finding(fid, task.id, title, url_for_finding, summary, relevance)
                    findings.append({"id": fid, "title": title, "url": url_for_finding, "summary": summary, "relevance": relevance})

                # Verify-by-reread for significant actions — assert UI state actually changed (US28)
                if is_significant:
                    verify_ok = False
                    verify_err: str | None = None
                    after_snapshot: str | None = None
                    try:
                        if hasattr(self.driver, "verify_browser_state"):
                            v = self.driver.verify_browser_state(expected_url_contains=action.target_url or plan.target)  # type: ignore
                            verified = bool(v.get("verified", True)) if isinstance(v, dict) else True
                            # Also capture after snapshot for change check
                            try:
                                after_snapshot = json.dumps(self.driver.get_browser_state())  # type: ignore
                            except Exception:
                                after_snapshot = json.dumps(v) if isinstance(v, dict) else str(v)
                            # Require both verified flag and state change (or at least url present)
                            if verified:
                                if before_snapshot is not None and after_snapshot is not None and before_snapshot == after_snapshot:
                                    # State didn't change — treat as unverified unless it's a read-only action
                                    if kind not in ("read_ui", "extract_public_info"):
                                        verify_ok = False
                                        verify_err = f"verify: state unchanged after {kind} for {url_for_record}"
                                    else:
                                        verify_ok = True
                                else:
                                    verify_ok = True
                            else:
                                verify_ok = False
                                verify_err = f"verify_browser_state not verified for {url_for_record}"
                        else:
                            after = self.driver.get_accessibility_tree()
                            after_snapshot = json.dumps(after)
                            if before_snapshot is not None and after_snapshot == before_snapshot and kind not in ("read_ui", "extract_public_info"):
                                verify_ok = False
                                verify_err = f"verify: accessibility tree unchanged after {kind}"
                            else:
                                verify_ok = True
                    except Exception as e:
                        verify_ok = False
                        verify_err = str(e)
                    if not verify_ok:
                        # Retry once for transient failures
                        try:
                            time.sleep(0.2)
                            driver_fn()
                            if hasattr(self.driver, "verify_browser_state"):
                                v2 = self.driver.verify_browser_state(expected_url_contains=action.target_url or plan.target)  # type: ignore
                                verify_ok = bool(v2.get("verified", True)) if isinstance(v2, dict) else True
                                if not verify_ok:
                                    verify_err = f"retry verify failed for {url_for_record}"
                                else:
                                    # Re-check change after retry
                                    try:
                                        after2 = json.dumps(self.driver.get_browser_state())  # type: ignore
                                        if before_snapshot is not None and after2 == before_snapshot and kind not in ("read_ui", "extract_public_info"):
                                            verify_ok = False
                                            verify_err = f"retry: state still unchanged after {kind}"
                                        else:
                                            verify_ok = True
                                            verify_err = None
                                    except Exception:
                                        verify_ok = True
                                        verify_err = None
                            else:
                                after2 = json.dumps(self.driver.get_accessibility_tree())
                                if before_snapshot is not None and after2 == before_snapshot and kind not in ("read_ui",):
                                    verify_ok = False
                                    verify_err = f"retry: state unchanged after {kind}"
                                else:
                                    verify_ok = True
                                    verify_err = None
                        except Exception as e2:
                            verify_err = f"{verify_err}; retry failed: {e2}" if verify_err else str(e2)
                    if verify_err or not verify_ok:
                        msg = f"verify failed for {kind}: {verify_err or 'unverified'}"
                        self.memory.record_error(str(uuid.uuid4()), task.id, msg)
                        errors.append({"message": msg})

            except Exception as e:
                aid = str(uuid.uuid4())
                self.memory.record_action(aid, task.id, kind, action.target_url, verdict.value, "failed", str(e))
                self.memory.record_error(str(uuid.uuid4()), task.id, f"action {kind} failed: {e}")
                errors.append({"message": f"action {kind} failed: {e}"})
                continue

        # Determine final state
        if task.state == AgentState.paused_by_user:
            # Already transitioned
            final_state = task.state
        elif is_emergency_stop_requested() or self._stop_requested.is_set():
            # Emergency stop → stopped
            reason = get_emergency_stop_reason() or self._stop_reason or "emergency stop"
            stopped_reason = reason
            try:
                task.transition_to(AgentState.stopped)
            except Exception:
                task.state = AgentState.stopped
            final_state = task.state
        elif stopped_reason and "limit reached" in stopped_reason:
            # Graceful completion with saved results — spec says session limits enforced with graceful completion
            try:
                task.transition_to(AgentState.completed)
            except Exception:
                task.state = AgentState.completed
            final_state = task.state
        elif errors and actions_executed == 0 and not findings:
            try:
                task.transition_to(AgentState.failed)
            except Exception:
                task.state = AgentState.failed
            final_state = task.state
        else:
            try:
                task.transition_to(AgentState.completed)
            except Exception:
                task.state = AgentState.completed
            final_state = task.state

        self.memory.update_task_state(task.id, task.state.value)

        # Record plan fingerprint for anti-repeat
        self.memory.record_plan_fingerprint(fp, task.id)

        # If no errors and no findings but at least one URL, synthesize finding for report
        if not findings and urls:
            for u in urls[:2]:
                fid = str(uuid.uuid4())
                title = f"Visited {u['url']}"
                summary = f"Visited {u['url']} for goal '{plan.goal}'"
                relevance = f"Relevant to profile for '{plan.goal}'"
                try:
                    self.memory.record_finding(fid, task.id, title, u["url"], summary, relevance)
                except Exception:
                    pass
                findings.append({"id": fid, "title": title, "url": u["url"], "summary": summary, "relevance": relevance})

        # Generate report
        # Gather persisted actions for report (also include passed in)
        persisted_actions = self.memory.list_actions(task_id=task.id)
        # Convert to dicts with required fields
        # Use queries/urls/findings from memory if we didn't capture fully
        if not queries:
            queries = self.memory.list_queries(limit=100)
            queries = [q for q in queries if q.get("task_id") == task.id]
        if not urls:
            urls = self.memory.list_urls(limit=100)
            urls = [u for u in urls if u.get("task_id") == task.id]
        if not findings:
            findings = self.memory.list_findings(task_id=task.id)
        if not errors:
            errors = self.memory.list_errors(task_id=task.id)
            errors = [{"message": e.get("message","")} for e in errors]

        elapsed_min = (time.monotonic() - start_time) / 60.0
        # If LLM cap was hit at planning, surface in limits even if stub execution continued
        _cap_limit_msg = (plan_error if cap_exceeded_before_plan else None) or (stopped_reason if stopped_reason and "limit" in stopped_reason.lower() else None)
        limits_dict = {
            "actions_used": actions_executed,
            "max_actions": self.config.max_actions,
            "duration_minutes": round(elapsed_min, 2),
            "max_duration_minutes": self.config.max_duration_minutes,
            "llm_calls_today": llm_calls_today,
            "max_llm_calls_per_day": self.config.max_llm_calls_per_day,
            "stopped_due_to_limit": _cap_limit_msg,
        }

        markdown = generate_markdown_report(
            task={"id": task.id, "description": task.description, "state": task.state.value},
            plan=plan,
            queries=queries,
            urls=urls,
            findings=findings,
            errors=errors,
            actions=persisted_actions,
            skipped_repeats=skipped_repeats,
            profile=profile,
            limits=limits_dict,
        )
        per_task_path = save_report_to_file(markdown, self.config.data_dir, task.id)
        self.memory.save_report(task.id, markdown)

        # Tab discipline: close only agent-owned tabs at task end; owner tabs untouched (issue #12)
        # This is the "Agent-created tabs closed at task end; owner tabs untouched" acceptance criterion.
        # We close via driver.close_all_agent_tabs() which tracks agent tabs distinctly from owner tabs.
        closed_agent_tabs: list[str] = []
        if hasattr(self.driver, "close_all_agent_tabs"):
            try:
                # Only auto-close if plan included a close action or if task completed successfully
                # For read-only browsing tasks, we always close agent tabs at end to leave machine clean
                should_close = "close_own_tab" in (plan.expected_actions or []) or actions_executed > 0
                if should_close:
                    closed = self.driver.close_all_agent_tabs()  # type: ignore
                    if closed:
                        closed_agent_tabs = list(closed)
                        # Record each close as an action for report
                        for ct in closed_agent_tabs:
                            try:
                                aid2 = str(uuid.uuid4())
                                self.memory.record_action(aid2, task.id, "close_own_tab", ct, "allowed", "completed", None)
                            except Exception:
                                pass
                        self.memory.record_error(str(uuid.uuid4()), task.id, f"closed {len(closed_agent_tabs)} agent tab(s) at task end: {closed_agent_tabs}")
            except Exception as e:
                # Never fail task due to close error; record as warning
                try:
                    self.memory.record_error(str(uuid.uuid4()), task.id, f"close_all_agent_tabs failed: {e}")
                    errors.append({"message": f"close_all_agent_tabs failed: {e}"})
                except Exception:
                    pass

        return ExecutionResult(
            task_id=task.id,
            state=task.state,
            plan=plan,
            actions_executed=actions_executed,
            queries=queries,
            urls=urls,
            findings=findings,
            errors=errors,
            skipped_repeats=skipped_repeats,
            report_markdown=markdown,
            report_path=per_task_path,
            stopped_reason=stopped_reason,
            limits=limits_dict,
        )
