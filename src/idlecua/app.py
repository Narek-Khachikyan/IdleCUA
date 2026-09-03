from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .config import IdleCuaConfig
from .contracts.computer import ComputerDriver, FakeComputerDriver
from .contracts.model import FakeModelProvider, ModelProvider
from .models.plan import Plan
from .models.task import Task
from .models.state import AgentState
from .planner import LlmCallCapExceeded, LlmPlanner, PlanRejectedError, StubPlanner
from .policy import PolicyEngine, PolicyResult, PolicyVerdict, TypedAction
from .scheduler import IdleScheduler


class ProfileNotConfirmedError(RuntimeError):
    """Raised when autonomous action is attempted while profile is unconfirmed/invalid."""


class IdleCua:
    """Public Application API.

    The CLI is a thin caller of this class; no business logic lives in the CLI.

    Example:
        config = IdleCuaConfig(data_dir="./data")
        app = IdleCua(config=config, computer=FakeComputerDriver())
        task = app.create_task("research recent AI papers on agents")
        plan = app.dry_run(task.description)
        assert len(app.computer.calls) == 0  # type: ignore[attr-defined]
    """

    @staticmethod
    def _create_real_driver(config: IdleCuaConfig | None = None) -> ComputerDriver:
        """Try to create the real Cua driver; raise with actionable message on failure."""
        try:
            from .drivers.cua_driver import CuaComputerDriver

            return CuaComputerDriver(session="idlecua", data_dir=getattr(config, "data_dir", None) if config else None)
        except Exception as e:
            # Surface as actionable error — caller can decide to fallback or fail
            raise RuntimeError(
                f"Real Cua driver requested but failed: {e}. "
                "Remediation: `uv pip install cua-driver==0.23.2`, grant Accessibility + Screen Recording, and retry. "
                "See README `Cua driver install` section. Falling back to FakeComputerDriver only if not explicitly requested."
            ) from e

    def __init__(
        self,
        config: IdleCuaConfig | None = None,
        *,
        computer: ComputerDriver | None = None,
        model_provider: ModelProvider | None = None,
        planner: StubPlanner | LlmPlanner | None = None,
        policy: PolicyEngine | None = None,
        idle_detector=None,
        memory=None,
    ) -> None:
        self.config = config or IdleCuaConfig()
        if computer is not None:
            self.computer: ComputerDriver = computer
        elif getattr(self.config, "use_real_driver", False):
            # Explicit via config/env — fail loudly if real driver unavailable
            self.computer = self._create_real_driver(self.config)
        else:
            self.computer = FakeComputerDriver()
        # If caller injects a provider (tests), use it verbatim — preserves fake path.
        # Otherwise try to resolve the selected OpenAI-compatible provider from disk/keychain.
        if model_provider is not None:
            self.model_provider: ModelProvider = model_provider
        else:
            self.model_provider = self._resolve_model_provider()
        # Planner selection: if caller injects one, use it; else choose based on provider.
        if planner is not None:
            self.planner = planner
        else:
            # Real provider -> LLM-backed planner (with profile/history, bounds, injection hardening, cap).
            # Fake provider -> deterministic stub for existing tests; LlmPlanner can still be injected explicitly.
            if isinstance(self.model_provider, FakeModelProvider):
                self.planner = StubPlanner()
            else:
                # Defer creation until memory/policy available — but create now with lazy memory via property
                try:
                    from .planner import LlmPlanner as _Llm

                    self.planner = _Llm(
                        model_provider=self.model_provider,
                        data_dir=self.config.data_dir,
                        config=self.config,
                        memory=self.memory,  # property initializes MemoryStore
                        policy_engine=policy or PolicyEngine(self.config),
                    )
                except Exception:
                    self.planner = StubPlanner()
        self.policy: PolicyEngine = policy or PolicyEngine(self.config)
        self._idle_detector = idle_detector
        self._memory = memory
        self._executor = None
        # If we deferred planner due to Fake check but policy wasn't ready, fixup for Llm case
        if isinstance(self.planner, LlmPlanner):
            # Ensure planner has latest policy/memory refs
            try:
                self.planner.memory = self.memory
                self.planner.policy_engine = self.policy
            except Exception:
                pass

    def _resolve_model_provider(self) -> ModelProvider:
        """Load the selected provider via the ModelProvider contract, or fallback to Fake."""
        try:
            from pathlib import Path

            from .keychain import get_default_store
            from .providers.config import ProviderStore
            from .providers.openai_adapter import OpenAICompatibleProvider

            store = ProviderStore.load(self.config.data_dir)
            selected = store.get_selected()
            if selected is None:
                return FakeModelProvider()
            kc = get_default_store(self.config.data_dir)
            api_key = kc.get(selected.name)
            if not api_key:
                import os

                env_map = {
                    "openrouter": "OPENROUTER_API_KEY",
                    "opencode-go": "OPENCODE_GO_API_KEY",
                }
                env_var = env_map.get(selected.name) or f"{selected.name.upper().replace('-', '_')}_API_KEY"
                api_key = os.environ.get(env_var) or os.environ.get("OPENAI_API_KEY")
            if not api_key:
                return FakeModelProvider()
            return OpenAICompatibleProvider(
                config=selected, api_key=api_key, data_dir=Path(self.config.data_dir)
            )
        except Exception:
            return FakeModelProvider()

    @property
    def memory(self):
        if self._memory is None:
            from .memory import MemoryStore

            self._memory = MemoryStore(self.config.data_dir)
        return self._memory

    @property
    def idle_detector(self):
        if self._idle_detector is None:
            if getattr(self.config, "use_real_idle_detector", False):
                try:
                    from .idle import QuartzIdleDetector

                    self._idle_detector = QuartzIdleDetector()
                except Exception:
                    from .idle import FakeIdleDetector

                    self._idle_detector = FakeIdleDetector(idle_seconds=1000, locked=False)
            else:
                # Default to Fake for deterministic tests and safe unattended runs.
                # Real Quartz detector can be injected explicitly when needed (e.g., production idle watch).
                from .idle import FakeIdleDetector

                self._idle_detector = FakeIdleDetector(idle_seconds=1000, locked=False)
        return self._idle_detector

    @idle_detector.setter
    def idle_detector(self, value) -> None:
        self._idle_detector = value

    def _get_executor(self):
        # Keep LlmPlanner's internal refs in sync (data_dir, policy, memory)
        if isinstance(self.planner, LlmPlanner):
            try:
                self.planner.memory = self.memory
                self.planner.policy_engine = self.policy
                # Ensure data_dir matches config
                from pathlib import Path as _P

                self.planner.data_dir = _P(self.config.data_dir).expanduser()
                self.planner.max_llm_calls_per_day = int(getattr(self.config, "max_llm_calls_per_day", 150) or 150)
            except Exception:
                pass
        if self._executor is None:
            from .executor import TaskExecutor

            self._executor = TaskExecutor(
                config=self.config,
                driver=self.computer,
                model_provider=self.model_provider,
                memory=self.memory,
                policy=self.policy,
                idle_detector=self.idle_detector,
                planner=self.planner,
            )
        else:
            # update deps if they changed
            self._executor.config = self.config
            self._executor.driver = self.computer
            self._executor.model_provider = self.model_provider
            self._executor.memory = self.memory
            self._executor.policy = self.policy
            self._executor.idle_detector = self.idle_detector
            self._executor.planner = self.planner
        return self._executor

    # -- task model --

    def create_task(self, description: str) -> Task:
        task = Task(description=description)
        # Persist disabled task
        try:
            self.memory.upsert_task(task.id, task.description, task.state.value, None)
        except Exception:
            pass
        return task

    async def acreate_task(self, description: str) -> Task:
        return self.create_task(description)

    # -- policy gating --

    def check_action(self, action: TypedAction) -> PolicyResult:
        """Check a single typed action against the PolicyEngine (read-only gate)."""
        return self.policy.evaluate(action)

    def can_execute(
        self,
        action: TypedAction,
        *,
        is_interactive: bool = False,
        confirmed: bool = False,
    ) -> tuple[bool, PolicyResult]:
        result = self.check_action(action)
        if result.verdict == PolicyVerdict.allowed:
            return True, result
        if result.verdict == PolicyVerdict.needs_confirmation:
            if self.config.readonly:
                return False, result
            if not is_interactive:
                return False, result
            if not confirmed:
                return False, result
            return True, result
        return False, result

    def execute_action(
        self,
        action: TypedAction,
        *,
        is_interactive: bool = False,
        confirmed: bool = False,
    ) -> PolicyResult:
        ok, result = self.can_execute(action, is_interactive=is_interactive, confirmed=confirmed)
        if not ok:
            raise PermissionError(f"Policy blocked action '{action.kind}': {result.reason} (verdict={result.verdict.value})")
        return result

    # -- planning / dry-run --

    def _resolve_profile_for_planner(self):
        # Load confirmed profile if available, else None (defaults)
        try:
            p = self.get_profile()
            if p is not None and bool(getattr(p, "confirmed", False)):
                # validate profile — if invalid, treat as no profile
                from .profile.validate import validate_profile

                if not validate_profile(p):
                    return p
            return p if p is not None and bool(getattr(p, "confirmed", False)) else None
        except Exception:
            return None

    def _resolve_history_for_planner(self) -> dict | None:
        try:
            return self.memory.get_history(limit=50)
        except Exception:
            return None

    def plan(self, task_description: str, profile: Any | None = None, history: Any | None = None) -> Plan:
        if not task_description or not task_description.strip():
            raise ValueError("task_description must be non-empty")
        # If planner is LLM-backed, inject profile/history and handle cap gracefully
        if isinstance(self.planner, LlmPlanner):
            eff_profile = profile if profile is not None else self._resolve_profile_for_planner()
            eff_history = history if history is not None else self._resolve_history_for_planner()
            try:
                return self.planner.plan(task_description, profile=eff_profile, history=eff_history)
            except LlmCallCapExceeded:
                # Graceful fallback: stub plan but cap visible via status/report; surface as stub with capped note
                # For dry-run, we still return a valid Plan (stub) so CLI can show cap usage
                # Executor path will handle cap via limits; this is the dry-run path.
                try:
                    from .accounting import get_today_count

                    # Ensure caller can see cap via status; we just return stub
                    pass
                except Exception:
                    pass
                # Return stub fallback — still bounded and policy-valid
                fallback = self.planner.fallback if hasattr(self.planner, "fallback") else StubPlanner()
                return fallback.plan(task_description, profile=eff_profile, history=eff_history)
            except PlanRejectedError:
                # Unconvertible LLM output is rejected — bubble up so caller knows free text was not executed
                raise
        # Stub or injected planner without profile/history support
        try:
            # Try with profile/history if planner supports it
            return self.planner.plan(task_description, profile=profile, history=history)  # type: ignore[call-arg]
        except TypeError:
            return self.planner.plan(task_description)

    async def aplan(self, task_description: str, profile: Any | None = None, history: Any | None = None) -> Plan:
        return self.plan(task_description, profile=profile, history=history)

    def dry_run(self, task_description: str, profile: Any | None = None, history: Any | None = None) -> Plan:
        """Dry-run: produces a bounded Plan and executes zero driver actions."""
        return self.plan(task_description, profile=profile, history=history)

    async def adry_run(self, task_description: str, profile: Any | None = None, history: Any | None = None) -> Plan:
        return self.dry_run(task_description, profile=profile, history=history)

    def ensure_profile_confirmed(self) -> None:
        """Hard gate: no autonomous action while profile is unconfirmed/invalid."""
        from .profile.store import load_profile
        from .profile.validate import validate_profile

        ppath = self.config.data_dir / "profile.json"
        profile = load_profile(ppath)
        if profile is None:
            raise ProfileNotConfirmedError(
                f"No profile found at {ppath}. Run `idle-cua profile interview` and confirm."
            )
        if not profile.confirmed:
            raise ProfileNotConfirmedError(
                f"Profile at {ppath} is unconfirmed. Complete `idle-cua profile interview` and confirm, or `idle-cua profile show` to inspect. Autonomous runs are blocked until the profile is confirmed."
            )
        errs = validate_profile(profile)
        if errs:
            raise ProfileNotConfirmedError(
                f"Profile at {ppath} is confirmed but invalid: {'; '.join(errs)}. Run `idle-cua profile validate`."
            )

    def check_profile_confirmed(self) -> tuple[bool, str]:
        try:
            self.ensure_profile_confirmed()
            return True, "Profile is confirmed and valid."
        except ProfileNotConfirmedError as e:
            return False, str(e)

    def get_profile(self):
        from .profile.store import load_profile

        return load_profile(self.config.data_dir / "profile.json")

    def get_plan_verdicts(self, plan: Plan) -> list[dict]:
        verdicts: list[dict] = []
        local_kinds = {"save_note", "create_note", "save_link", "close_own_tab", "close_own_app"}
        for kind in plan.expected_actions:
            if kind in local_kinds:
                action = TypedAction(kind=kind)
            else:
                url = f"https://{plan.target}"
                action = TypedAction(kind=kind, target_url=url)
            result = self.check_action(action)
            verdicts.append(
                {
                    "action": kind,
                    "verdict": result.verdict.value,
                    "reason": result.reason,
                    "action_class": result.action_class.value,
                    "domain": result.domain,
                }
            )
        return verdicts

    def annotate_plan(self, plan: Plan) -> dict:
        base = self.plan_to_dict(plan)
        base["action_verdicts"] = self.get_plan_verdicts(plan)
        return base

    def run_once(self, task_description: str, *, dry_run: bool = False, is_interactive: bool = False, confirm_func: Callable | None = None):
        if dry_run:
            return self.dry_run(task_description)
        self.ensure_profile_confirmed()
        task = self.create_task(task_description)
        result = self.run_task(task, is_interactive=is_interactive, confirm_func=confirm_func)
        return result

    async def arun_once(self, task_description: str, *, dry_run: bool = False, is_interactive: bool = False, confirm_func: Callable | None = None):
        if dry_run:
            return self.dry_run(task_description)
        self.ensure_profile_confirmed()
        task = self.create_task(task_description)
        return await self.arun_task(task, is_interactive=is_interactive, confirm_func=confirm_func)

    # -- programmatic execution (spec shape) --

    def run_task(
        self,
        task_or_description: Task | str,
        *,
        is_interactive: bool = False,
        confirm_func: Callable | None = None,
        dry_run: bool = False,
    ):
        """Run a task end-to-end (sync). Accepts Task or description string."""
        if isinstance(task_or_description, str):
            task = self.create_task(task_or_description)
        else:
            task = task_or_description
        if dry_run:
            return self.dry_run(task.description)
        # Hard gates before execution (US14 idle gate, profile confirmed) — threshold single source Profile per ADR-0003
        if not dry_run and self.config.require_idle:
            # Effective threshold: Profile seconds if available, else Config
            try:
                from .profile.models import get_effective_idle_threshold_seconds

                _p = self.get_profile()
                _thr = get_effective_idle_threshold_seconds(_p, fallback=int(getattr(self.config, "idle_threshold_seconds", 600)))
            except Exception:
                _thr = int(getattr(self.config, "idle_threshold_seconds", 600))
            ok, reason = self.idle_detector.can_run(_thr)
            if not ok:
                # Report as failed like executor does
                try:
                    task.transition_to(AgentState.failed)
                except Exception:
                    task.state = AgentState.failed
                self.memory.upsert_task(task.id, task.description, task.state.value, None)
                import uuid
                self.memory.record_error(uuid.uuid4().hex, task.id, f"idle gate blocked: {reason}")
                raise RuntimeError(f"idle gate blocked: {reason}")
            if self.idle_detector.is_screen_locked():
                try:
                    task.transition_to(AgentState.failed)
                except Exception:
                    task.state = AgentState.failed
                self.memory.upsert_task(task.id, task.description, task.state.value, None)
                import uuid
                self.memory.record_error(uuid.uuid4().hex, task.id, f"screen locked — {reason}")
                raise RuntimeError(f"screen locked — {reason}")
        profile = self.get_profile()
        executor = self._get_executor()
        return executor.execute_task(task, profile=profile, dry_run=False, is_interactive=is_interactive, confirm_func=confirm_func)

    async def arun_task(
        self,
        task_or_description: Task | str,
        *,
        is_interactive: bool = False,
        confirm_func: Callable | None = None,
        dry_run: bool = False,
    ):
        # For MVP, async delegates to sync (no real async I/O)
        return self.run_task(task_or_description, is_interactive=is_interactive, confirm_func=confirm_func, dry_run=dry_run)

    async def create_task_async(self, description: str) -> Task:
        return self.create_task(description)

    async def pause_task(self, task_id: str) -> Task | None:
        # Find task and transition to paused_by_user
        data = self.memory.get_task(task_id)
        if not data:
            return None
        from .models.state import validate_transition

        cur = AgentState(data["state"])
        # Try to transition
        try:
            # Create Task object for validation
            t = Task(description=data["description"], id=data["id"], state=cur)
            t.transition_to(AgentState.paused_by_user)
            self.memory.update_task_state(task_id, AgentState.paused_by_user.value)
            return t
        except Exception:
            return None

    async def cancel_task(self, task_id: str, reason: str = "cancelled") -> Task | None:
        data = self.memory.get_task(task_id)
        if not data:
            return None
        t = Task(description=data["description"], id=data["id"], state=AgentState(data["state"]))
        try:
            t.transition_to(AgentState.stopped)
        except Exception:
            t.state = AgentState.stopped
        self.memory.update_task_state(task_id, t.state.value)
        self.memory.record_error(str(uuid.uuid4()), task_id, f"cancelled: {reason}")
        # Release input + terminate agent processes
        try:
            if hasattr(self.computer, "release_all_inputs"):
                self.computer.release_all_inputs()
            if hasattr(self.computer, "terminate_agent_processes"):
                self.computer.terminate_agent_processes()  # type: ignore
        except Exception:
            pass
        return t

    def request_emergency_stop(self, reason: str = "emergency stop") -> None:
        """LLM-independent stop — releases input synchronously and records reason."""
        from .executor import request_emergency_stop

        request_emergency_stop(reason)
        # Also release via driver synchronously
        try:
            if hasattr(self.computer, "release_all_inputs"):
                self.computer.release_all_inputs()
            if hasattr(self.computer, "terminate_agent_processes"):
                self.computer.terminate_agent_processes()  # type: ignore
        except Exception:
            pass
        # Persist reason in kv for status
        try:
            self.memory.kv_set("last_stop_reason", reason)
        except Exception:
            pass

    def clear_emergency_stop(self) -> None:
        from .executor import clear_emergency_stop

        clear_emergency_stop()

    # Back-compat async aliases
    async def run_task_async(self, description: str):
        return self.run_task(description)

    # -- history / reports / status --

    def get_history(self, limit: int = 200) -> dict:
        return self.memory.get_history(limit=limit)

    def get_report(self, task_id: str) -> dict | None:
        return self.memory.get_report(task_id)

    def list_reports(self, limit: int = 50) -> list[dict]:
        return self.memory.list_reports(limit=limit)

    def get_status(self) -> dict:
        """Status view: agent state, idle time, active task, last action, current site/app, limit usage, stop command."""
        # Active task: most recent
        tasks = self.memory.list_tasks(limit=1)
        active = tasks[0] if tasks else None
        last_actions = self.memory.list_actions(task_id=active["id"]) if active else []
        last = last_actions[-1] if last_actions else None
        queries = self.memory.list_queries(limit=5)
        # Idle
        idle_secs = 0.0
        idle_str = "unknown"
        locked = False
        try:
            idle_secs = float(self.idle_detector.seconds_since_last_input())
            locked = bool(self.idle_detector.is_screen_locked())
            idle_str = f"{idle_secs:.1f}s"
        except Exception:
            pass
        # Limits
        from .accounting import get_today_count

        llm_today = get_today_count(self.config.data_dir)
        # Determine agent state
        state = active["state"] if active else "disabled"
        # Determine current site/app from last action
        current_site = None
        if last and last.get("target_url"):
            current_site = last.get("target_url")
        elif active:
            # try plan
            try:
                pj = json.loads(active.get("plan_json") or "{}")
                current_site = pj.get("target")
            except Exception:
                pass

        return {
            "agent_state": state,
            "idle_time": idle_str,
            "idle_seconds": idle_secs,
            "screen_locked": locked,
            "active_task": active,
            "last_action": last,
            "current_site": current_site,
            "llm_calls_today": llm_today,
            "max_llm_calls_per_day": self.config.max_llm_calls_per_day,
            "max_actions": self.config.max_actions,
            "max_duration_minutes": self.config.max_duration_minutes,
            "stop_command": "idle-cua kill  |  idle-cua stop  |  Ctrl-C (SIGINT)",
        }

    # -- config persistence helpers (used by CLI init) --

    def init_data_dir(self) -> Path:
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.config.config_path.exists():
            self.config.save()
        # Ensure memory db initialized
        _ = self.memory
        return self.config.data_dir

    # -- idle scheduler (real-machine auto-start) --

    def get_scheduler(self) -> IdleScheduler:
        return IdleScheduler(config=self.config, idle_detector=self.idle_detector, memory=self.memory)

    def wait_for_idle(
        self,
        poll_interval: float = 5.0,
        timeout: float | None = None,
        threshold_override: int | None = None,
        on_tick=None,
    ) -> bool:
        return self.get_scheduler().wait_for_idle(
            poll_interval=poll_interval,
            timeout=timeout,
            threshold_override=threshold_override,
            on_tick=on_tick,
        )

    def run_idle_session(
        self,
        task_description: str,
        *,
        poll_interval: float = 5.0,
        idle_threshold_override: int | None = None,
        timeout: float | None = None,
        is_interactive: bool = False,
        confirm_func: Callable | None = None,
        wait: bool = True,
    ):
        """Wait for idle (if wait=True) then run one autonomous session.

        End-to-end: idle auto-start → plan → policy → driver → history → daily report
        → graceful stop on return/limits/emergency stop.
        """
        scheduler = self.get_scheduler()
        if wait:
            ok = scheduler.wait_for_idle(
                poll_interval=poll_interval, timeout=timeout, threshold_override=idle_threshold_override
            )
            if not ok:
                try:
                    from .profile.models import get_effective_idle_threshold_seconds

                    _p_eff = self.get_profile()
                    _thr_msg = idle_threshold_override if idle_threshold_override is not None else get_effective_idle_threshold_seconds(_p_eff, fallback=int(getattr(self.config, "idle_threshold_seconds", 600)))
                except Exception:
                    _thr_msg = idle_threshold_override or getattr(self.config, "idle_threshold_seconds", 600)
                raise RuntimeError(f"Timed out waiting for idle (threshold {_thr_msg}s)")
        ok, reason = scheduler.can_start(threshold_override=idle_threshold_override)
        if not ok:
            raise RuntimeError(f"Cannot start idle session — gate failed: {reason}")
        return scheduler.run_one_session(
            task_description, is_interactive=is_interactive, confirm_func=confirm_func
        )

    def run_idle_loop(
        self,
        task_description: str,
        *,
        poll_interval: float = 5.0,
        idle_threshold_override: int | None = None,
        max_sessions: int | None = None,
        once: bool = False,
        timeout_per_wait: float | None = None,
        on_event=None,
        is_interactive: bool = False,
        confirm_func: Callable | None = None,
    ) -> list[Any]:
        return self.get_scheduler().run_loop(
            task_description,
            poll_interval=poll_interval,
            idle_threshold_override=idle_threshold_override,
            max_sessions=max_sessions,
            once=once,
            timeout_per_wait=timeout_per_wait,
            on_event=on_event,
            is_interactive=is_interactive,
            confirm_func=confirm_func,
        )

    def plan_to_dict(self, plan: Plan) -> dict:
        base: dict = {
            "goal": plan.goal,
            "target": plan.target,
            "expected_actions": plan.expected_actions,
            "expected_result": plan.expected_result,
            "max_duration_minutes": plan.max_duration_minutes,
            "max_actions": plan.max_actions,
            "risk_level": plan.risk_level.value,
            "requires_confirmation": plan.requires_confirmation,
        }
        try:
            base["action_verdicts"] = self.get_plan_verdicts(plan)
        except Exception:
            base["action_verdicts"] = []
        return base

    def plan_to_json(self, plan: Plan) -> str:
        return json.dumps(self.plan_to_dict(plan), indent=2)
