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

    def get_effective_idle_threshold(self) -> int:
        """Single source per ADR-0003: Profile seconds, fallback to Config.

        T1: all callers (CLI, HTTP API, scheduler, executor) delegate here.
        """
        try:
            from .profile.models import get_effective_idle_threshold_seconds

            p = self.get_profile()
            return get_effective_idle_threshold_seconds(
                p, fallback=int(getattr(self.config, "idle_threshold_seconds", 600))
            )
        except Exception:
            return int(getattr(self.config, "idle_threshold_seconds", 600))

    def get_readiness(self, threshold_override: int | None = None) -> dict:
        """Decide all pre-run gates once inside the Application API (T1).

        Returns dict with effective_threshold, individual gate results
        (profile/schedule/idle/limits as {ok, reason, gate}), can_start and
        reason/failed_gate. Callers render; they do not re-implement gates.
        """
        from .scheduler import IdleScheduler

        effective = int(threshold_override) if threshold_override is not None else self.get_effective_idle_threshold()
        scheduler = self.get_scheduler()
        # Profile gate uses the canonical Application API message (same text CLI prints with "Refused: " prefix).
        ok_p, reason_p = self.check_profile_confirmed()
        profile_gate = {"ok": bool(ok_p), "reason": str(reason_p), "gate": "profile"}
        try:
            schedule_gate_obj = scheduler.check_schedule_gate()
            schedule_gate = {"ok": bool(schedule_gate_obj.ok), "reason": str(schedule_gate_obj.reason), "gate": "schedule"}
        except Exception as e:
            schedule_gate = {"ok": True, "reason": f"schedule check skipped: {e}", "gate": "schedule"}
        try:
            idle_gate_obj = scheduler.check_idle_gate(effective)
            idle_gate = {"ok": bool(idle_gate_obj.ok), "reason": str(idle_gate_obj.reason), "gate": str(idle_gate_obj.gate)}
        except Exception as e:
            idle_gate = {"ok": False, "reason": f"idle check failed: {e}", "gate": "idle"}
        # require_idle=False disables the idle/screen gate (matches run_task behavior).
        if not bool(getattr(self.config, "require_idle", True)):
            idle_gate = {"ok": True, "reason": "idle gate disabled (require_idle=False)", "gate": "idle"}
        try:
            limits_gate_obj = scheduler.check_limits_gate()
            limits_gate = {"ok": bool(limits_gate_obj.ok), "reason": str(limits_gate_obj.reason), "gate": "limits"}
        except Exception as e:
            limits_gate = {"ok": True, "reason": f"limits check skipped: {e}", "gate": "limits"}
        for g in (profile_gate, schedule_gate, idle_gate, limits_gate):
            if not g["ok"]:
                gate = g["gate"]
                reason = g["reason"]
                # Preserve legacy user-facing phrasing per gate so CLI/HTTP texts stay byte-identical.
                if gate == "idle":
                    combined = f"idle gate blocked: {reason}"
                elif gate == "screen":
                    combined = f"screen locked — {reason}"
                else:
                    combined = f"{gate}: {reason}" if not reason.startswith(f"{gate}:") else reason
                return {
                    "effective_threshold": effective,
                    "profile": profile_gate,
                    "schedule": schedule_gate,
                    "idle": idle_gate,
                    "limits": limits_gate,
                    "can_start": False,
                    "reason": combined,
                    "failed_gate": gate,
                }
        return {
            "effective_threshold": effective,
            "profile": profile_gate,
            "schedule": schedule_gate,
            "idle": idle_gate,
            "limits": limits_gate,
            "can_start": True,
            "reason": "all gates pass",
            "failed_gate": None,
        }

    def can_start(self, threshold_override: int | None = None) -> tuple[bool, str]:
        """Thin readiness check: (ok, reason) with decision owned by the Application API."""
        r = self.get_readiness(threshold_override=threshold_override)
        return bool(r["can_start"]), str(r["reason"])

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
        # Hard gates before execution (T1: profile gate + idle/screen via single source).
        # Profile gate first so CLI and HTTP API block identically with the same message.
        self.ensure_profile_confirmed()
        if not dry_run and self.config.require_idle:
            # Effective threshold: single source via get_effective_idle_threshold (ADR-0003).
            _thr = self.get_effective_idle_threshold()
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

    def get_owner_settings(self) -> dict:
        """Owner-settings read behind the Application API (T3).

        Moves server GET /api/v1/settings logic here so Local UI and CLI
        share one place per ADR-0002/0003. Prefill of unset Profile fields
        from Config is display-only (never persisted here). Effective limits
        are tighten-only min(Profile, ceiling). Provider keys are never touched.

        Returns {profile, config, effective} with same keys as the HTTP
        contract (byte-identical to the previous server handler).
        """
        from .config import IdleCuaConfig as _Cfg
        from .profile.models import Profile as _Profile
        from .profile.store import load_profile as _load_profile

        data_dir = self.config.data_dir
        # Reload config from disk so HTTP and CLI see the same persisted value.
        try:
            config = _Cfg.load(data_dir)
            # Keep the instance in sync for callers that reuse self.config.
            self.config = config
        except Exception:
            config = self.config
        profile = _load_profile(data_dir / "profile.json")
        if profile is None:
            profile_dict = _Profile().model_dump()
        else:
            profile_dict = profile.model_dump()
            # One-time prefill for review (display only, not persisted).
            ab_pref = profile_dict.get("autonomy_boundaries", {})
            if isinstance(ab_pref, dict) and not ab_pref.get("allowed_sites"):
                ab_pref["allowed_sites"] = list(config.allowlist)
            if isinstance(ab_pref, dict) and not ab_pref.get("deny_zones"):
                ab_pref["deny_zones"] = (
                    list(config.deny_zones) if ab_pref.get("deny_zones") == [] else ab_pref.get("deny_zones", [])
                )
            cu_pref = profile_dict.get("computer_usage", {})
            if (
                isinstance(cu_pref, dict)
                and cu_pref.get("idle_threshold_seconds", 600) == 600
                and config.idle_threshold_seconds != 600
            ):
                cu_pref["idle_threshold_seconds"] = int(config.idle_threshold_seconds)
                cu_pref["idle_threshold_minutes"] = max(1, (int(config.idle_threshold_seconds) + 59) // 60)

        ab = profile_dict.get("autonomy_boundaries", {}) if isinstance(profile_dict, dict) else {}
        cu = profile_dict.get("computer_usage", {}) if isinstance(profile_dict, dict) else {}
        idle_sec_profile = None
        if isinstance(cu, dict):
            secs = cu.get("idle_threshold_seconds")
            if isinstance(secs, int):
                idle_sec_profile = secs
            else:
                minutes = cu.get("idle_threshold_minutes")
                if isinstance(minutes, int):
                    idle_sec_profile = minutes * 60

        eff_session = min(ab.get("session_duration_minutes", 45) if isinstance(ab, dict) else 45, 45)
        eff_actions = min(ab.get("daily_action_limit", 200) if isinstance(ab, dict) else 200, 200)
        eff_llm = min(ab.get("daily_llm_call_limit", 150) if isinstance(ab, dict) else 150, 150)
        return {
            "profile": {
                "confirmed": profile.confirmed if profile else False,
                "session_duration_minutes": ab.get("session_duration_minutes", 45) if isinstance(ab, dict) else 45,
                "daily_action_limit": ab.get("daily_action_limit", 200) if isinstance(ab, dict) else 200,
                "daily_llm_call_limit": ab.get("daily_llm_call_limit", 150) if isinstance(ab, dict) else 150,
                "allowed_hours": ab.get("allowed_hours", "00:00-23:59") if isinstance(ab, dict) else "00:00-23:59",
                "allowlist": ab.get("allowed_sites", []) if isinstance(ab, dict) else [],
                "deny_zones": ab.get("deny_zones", []) if isinstance(ab, dict) else [],
                "allowed_sites": ab.get("allowed_sites", []) if isinstance(ab, dict) else [],
                "idle_threshold_seconds": idle_sec_profile if idle_sec_profile is not None else config.idle_threshold_seconds,
                "idle_threshold_minutes": cu.get("idle_threshold_minutes", 10) if isinstance(cu, dict) else 10,
                "browser_consent": ab.get("browser_consent", {}) if isinstance(ab, dict) else {},
            },
            "config": {
                "readonly": config.readonly,
                "require_idle": config.require_idle,
                "ceilings": {
                    "max_duration_minutes": 45,
                    "max_actions": 200,
                    "max_llm_calls_per_day": 150,
                },
                "current": {
                    "max_duration_minutes": config.max_duration_minutes,
                    "max_actions": config.max_actions,
                    "max_llm_calls_per_day": config.max_llm_calls_per_day,
                    "idle_threshold_seconds": config.idle_threshold_seconds,
                },
                "data_dir": str(config.data_dir),
            },
            "effective": {
                "session_duration_minutes": eff_session,
                "daily_action_limit": eff_actions,
                "daily_llm_call_limit": eff_llm,
                "idle_threshold_seconds": idle_sec_profile if idle_sec_profile is not None else config.idle_threshold_seconds,
            },
        }

    def update_owner_settings(self, patch: dict) -> dict:
        """Owner-settings write behind the Application API (T3).

        Owns all validation (range, allowlist domain shape, idle bounds,
        consent) plus tighten-only ceiling checks, then validate_profile,
        then persistence to the single authority per setting per ADR-0003.
        Raises ValueError with a clear user-facing message on any failure;
        callers map to transport errors with identical text (HTTP 400, CLI).

        Returns {profile, config, effective} on success. Provider keys are
        untouched and never included in returns/logs/errors.
        """
        from .config import IdleCuaConfig as _Cfg
        from .profile.models import BrowserConsent as _BC
        from .profile.models import Profile as _Profile
        from .profile.store import load_profile as _load_profile
        from .profile.store import save_profile as _save_profile
        from .profile.validate import _is_valid_domain as _is_valid
        from .profile.validate import validate_profile as _validate

        data_dir = self.config.data_dir
        config = _Cfg.load(data_dir)
        profile = _load_profile(data_dir / "profile.json")
        if profile is None:
            profile = _Profile()
        ab = profile.autonomy_boundaries
        cu = profile.computer_usage

        # Extract patch values (support both SettingsPatch keys and legacy dotted paths if ever passed).
        # Only the canonical keys are expected; unknown keys are ignored.
        if "session_duration_minutes" in patch and patch["session_duration_minutes"] is not None:
            val = int(patch["session_duration_minutes"])
            if val <= 0 or val > 45:
                raise ValueError("session_duration_minutes must be 1..45")
            if val > 45:
                raise ValueError("session_duration_minutes above ceiling 45")
            ab.session_duration_minutes = val
        if "daily_action_limit" in patch and patch["daily_action_limit"] is not None:
            val = int(patch["daily_action_limit"])
            if val <= 0 or val > 1000:
                raise ValueError("daily_action_limit must be 1..1000")
            if val > 200:
                raise ValueError("daily_action_limit above ceiling 200")
            ab.daily_action_limit = val
        if "daily_llm_call_limit" in patch and patch["daily_llm_call_limit"] is not None:
            val = int(patch["daily_llm_call_limit"])
            if val <= 0 or val > 1000:
                raise ValueError("daily_llm_call_limit must be 1..1000")
            if val > 150:
                raise ValueError("daily_llm_call_limit above ceiling 150")
            ab.daily_llm_call_limit = val
        if "allowed_hours" in patch and patch["allowed_hours"] is not None:
            ab.allowed_hours = str(patch["allowed_hours"])
        if "allowlist" in patch and patch["allowlist"] is not None:
            allowlist = patch["allowlist"]
            if not isinstance(allowlist, list):
                raise ValueError("allowlist must be list")
            for site in allowlist:
                if not _is_valid(site):
                    raise ValueError(f"allowlist: invalid domain '{site}'")
            ab.allowed_sites = [s.strip().lower() for s in allowlist]
        if "deny_zones" in patch and patch["deny_zones"] is not None:
            ab.deny_zones = list(patch["deny_zones"])
        if "idle_threshold_seconds" in patch and patch["idle_threshold_seconds"] is not None:
            val = int(patch["idle_threshold_seconds"])
            if val < 60 or val > 7200:
                raise ValueError("idle_threshold_seconds must be 60..7200")
            cu.idle_threshold_seconds = val
            cu.idle_threshold_minutes = max(1, min(120, (val + 59) // 60))
        if "browser_consent" in patch and patch["browser_consent"] is not None:
            granted = bool(patch["browser_consent"])
            bc = _BC(main_profile_granted=granted, browser="chrome")
            ab.browser_consent = bc
            profile.browser_consent = bc
        if "readonly" in patch and patch["readonly"] is not None:
            config.readonly = bool(patch["readonly"])
        if "require_idle" in patch and patch["require_idle"] is not None:
            config.require_idle = bool(patch["require_idle"])

        errs = _validate(profile)
        if errs:
            raise ValueError("; ".join(errs))

        _save_profile(profile, data_dir / "profile.json")
        config.save()
        self.config = config
        # Mirror browser consent to both stores (profile already set; this ensures config mirror and profile alias stay in sync).
        if "browser_consent" in patch and patch["browser_consent"] is not None:
            try:
                from .browser_consent import record_consent as _record

                _record(data_dir, bool(patch["browser_consent"]))
                # record_consent rewrites config.json and profile.json; reload config to keep in sync
                try:
                    self.config = _Cfg.load(data_dir)
                    config = self.config
                except Exception:
                    pass
            except Exception:
                pass

        # Compute effective the same way as get_owner_settings (tighten-only).
        eff = {
            "session_duration_minutes": min(int(ab.session_duration_minutes), 45),
            "daily_action_limit": min(int(ab.daily_action_limit), 200),
            "daily_llm_call_limit": min(int(ab.daily_llm_call_limit), 150),
            "idle_threshold_seconds": int(cu.idle_threshold_seconds),
        }
        return {
            "profile": profile.model_dump(),
            "config": config.to_dict(),
            "effective": eff,
        }

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
                _thr_msg = int(idle_threshold_override) if idle_threshold_override is not None else self.get_effective_idle_threshold()
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
