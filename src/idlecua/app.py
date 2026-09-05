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


# Tighten-only ceilings per ADR-0003 (Config owns safety ceilings; Profile values
# above ceiling are validation rejections, never silently clamped).
_MAX_DURATION_MINUTES = 45
_MAX_ACTIONS = 200
_MAX_LLM_CALLS_PER_DAY = 150


def _effective_limits(
    session_minutes: int, action_limit: int, llm_limit: int, idle_seconds: int
) -> dict:
    """Single tighten-only derivation shared by get/update_owner_settings."""
    return {
        "session_duration_minutes": min(int(session_minutes), _MAX_DURATION_MINUTES),
        "daily_action_limit": min(int(action_limit), _MAX_ACTIONS),
        "daily_llm_call_limit": min(int(llm_limit), _MAX_LLM_CALLS_PER_DAY),
        "idle_threshold_seconds": int(idle_seconds),
    }


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
        self._lifecycle = None
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

    @property
    def lifecycle(self):
        """Single lifecycle owner behind the Application API (ADR-0006)."""
        from .task_lifecycle import TaskLifecycle

        # Keep an LLM-backed planner's refs in sync (data_dir, policy, memory).
        if isinstance(self.planner, LlmPlanner):
            try:
                self.planner.memory = self.memory
                self.planner.policy_engine = self.policy
                from pathlib import Path as _P

                self.planner.data_dir = _P(self.config.data_dir).expanduser()
                self.planner.max_llm_calls_per_day = int(getattr(self.config, "max_llm_calls_per_day", 150) or 150)
            except Exception:
                pass
        if self._lifecycle is None:
            self._lifecycle = TaskLifecycle(
                self.config,
                self.memory,
                driver=self.computer,
                model_provider=self.model_provider,
                planner=self.planner,
                policy=self.policy,
                idle_detector=self.idle_detector,
            )
        else:
            self._lifecycle.config = self.config
            try:
                self._lifecycle._memory = self.memory
            except Exception:
                pass
            self._lifecycle.driver = self.computer
            self._lifecycle.model_provider = self.model_provider
            self._lifecycle.planner = self.planner
            self._lifecycle.policy = self.policy
            self._lifecycle.idle_detector = self.idle_detector
        return self._lifecycle

    # -- task model --

    def create_task(
        self,
        description: str,
        *,
        skip_action_types: list[str] | tuple[str, ...] | None = None,
        approvals: list[str] | tuple[str, ...] | None = None,
    ) -> Task:
        """Enqueue via the lifecycle seam: immutable goal, waiting_for_idle."""
        from .task_lifecycle import Enqueue, OutcomeCategory

        out = self.lifecycle.handle(
            Enqueue(
                goal=description,
                skip_action_types=tuple(skip_action_types or ()),
                approvals=tuple(approvals or ()),
            )
        )
        if out.category == OutcomeCategory.invalid_request:
            raise ValueError(out.message)
        if out.category != OutcomeCategory.ok or not out.task_id:
            raise RuntimeError(out.message or "enqueue failed")
        return Task(description=description.strip(), id=out.task_id, state=AgentState(out.state or "waiting_for_idle"))

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
        """One readiness path owned by the lifecycle seam (ADR-0006).

        Shape-preserving delegate: CLI/HTTP texts stay byte-identical.
        """
        r = self.lifecycle._readiness()
        effective = int(threshold_override) if threshold_override is not None else int(r.get("threshold", 600))
        # Re-evaluate idle text with override when provided (same gate, same path).
        if threshold_override is not None:
            try:
                ig = self.get_scheduler().check_idle_gate(effective)
                idle_gate = {"ok": bool(ig.ok), "reason": str(ig.reason), "gate": str(ig.gate)}
                if not bool(getattr(self.config, "require_idle", True)):
                    idle_gate = {"ok": True, "reason": "idle gate disabled (require_idle=False)", "gate": "idle"}
                r = dict(r)
                r["idle"] = idle_gate
                r["threshold"] = effective
                if not idle_gate["ok"]:
                    r["can_start"] = False
                    r["reason"] = f"idle gate blocked: {idle_gate['reason']}" if idle_gate["gate"] == "idle" else f"screen locked — {idle_gate['reason']}"
                    r["failed_gate"] = idle_gate["gate"]
            except Exception:
                pass
        return {
            "effective_threshold": effective,
            "profile": r.get("profile", {}),
            "schedule": r.get("schedule", {}),
            "idle": r.get("idle", {}),
            "limits": r.get("limits", {}),
            "can_start": bool(r.get("can_start")),
            "reason": str(r.get("reason", "")),
            "failed_gate": r.get("failed_gate"),
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
        """Run a task end-to-end via the lifecycle seam (ADR-0006)."""
        from .task_lifecycle import OutcomeCategory, Start

        if isinstance(task_or_description, str):
            task = self.create_task(task_or_description)
        else:
            task = task_or_description
        if dry_run:
            return self.dry_run(task.description)
        # Resolve the lifecycle identity: a Task object that was never enqueued
        # (legacy caller shape) is claimed by id when present, else enqueued.
        task_id = task.id
        try:
            existing = self.memory.get_task(task_id)
        except Exception:
            existing = None
        if existing is None:
            try:
                fresh = self.create_task(task.description)
                task_id = fresh.id
            except Exception:
                pass
        out = self.lifecycle.handle(
            Start(
                task_id=task_id,
                trigger="explicit",
                mode="interactive" if is_interactive else "unattended",
                confirm_func=confirm_func,
            )
        )
        if out.category in (OutcomeCategory.not_ready, OutcomeCategory.already_active):
            if out.failed_gate == "profile":
                self.ensure_profile_confirmed()
            raise RuntimeError(out.message or "cannot start")
        if out.category == OutcomeCategory.not_found:
            raise RuntimeError(out.message or "task not found")
        if out.category in (OutcomeCategory.invalid_request, OutcomeCategory.invalid_transition):
            raise RuntimeError(out.message or "invalid task transition")
        if out.category == OutcomeCategory.execution_failed and out.state not in ("failed", "paused_by_user", "stopped", "completed"):
            raise RuntimeError(out.message or "execution failed")
        return self._execution_result_from_lifecycle(out.task_id or task_id)

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

    def _execution_result_from_lifecycle(self, task_id: str):
        """Shape-preserving bridge: lifecycle snapshot -> legacy ExecutionResult."""
        from pathlib import Path as _P

        from .executor import ExecutionResult
        from .models.plan import Plan
        from .models.plan import RiskLevel as _RL
        from .models.state import AgentState as _AS

        row = self.memory.get_task(task_id) or {}
        state_v = str(row.get("state", "failed"))
        try:
            state = _AS(state_v)
        except Exception:
            state = _AS.failed
        plan = None
        try:
            if row.get("plan_json"):
                pj = json.loads(row["plan_json"])
                plan = Plan(
                    goal=pj.get("goal", row.get("description", "")),
                    target=pj.get("target", "google.com"),
                    expected_actions=list(pj.get("expected_actions", ["search"])),
                    expected_result=pj.get("expected_result", ""),
                    max_duration_minutes=int(pj.get("max_duration_minutes", 45)),
                    max_actions=int(pj.get("max_actions", 50)),
                    risk_level=_RL(pj.get("risk_level", "low")),
                    requires_confirmation=bool(pj.get("requires_confirmation", False)),
                )
        except Exception:
            plan = None
        if plan is None:
            try:
                plan = self.dry_run(row.get("description", "task"))
            except Exception:
                plan = Plan(goal=row.get("description", "task"), target="google.com", expected_actions=["search"], expected_result="", max_duration_minutes=10, max_actions=10, risk_level=_RL.low, requires_confirmation=False)
        try:
            actions = self.memory.list_actions(task_id=task_id)
        except Exception:
            actions = []
        # Cleanup closes ("cleanup: ...") are tab discipline, not plan work.
        n_completed = sum(
            1
            for a in actions
            if a.get("status") == "completed" and not str(a.get("error") or "").startswith("cleanup:")
        )
        try:
            queries = [q for q in self.memory.list_queries(limit=1000) if q.get("task_id") == task_id]
        except Exception:
            queries = []
        try:
            urls = [u for u in self.memory.list_urls(limit=1000) if u.get("task_id") == task_id]
        except Exception:
            urls = []
        try:
            findings = self.memory.list_findings(task_id=task_id)
        except Exception:
            findings = []
        try:
            errs = self.memory.list_errors(task_id=task_id)
        except Exception:
            errs = []
        try:
            rep = self.memory.get_report(task_id)
            md = rep.get("markdown", "") if rep else ""
        except Exception:
            md = ""
        if not md:
            try:
                rp = _P(self.config.data_dir) / "reports" / f"{task_id}.md"
                if rp.exists():
                    md = rp.read_text(encoding="utf-8")
            except Exception:
                md = ""
        # Rebuild skipped_repeats view from persisted skips/blocks for CLI parity.
        # The lifecycle persists every skip as an action row (blocked/skipped)
        # and/or an error row; surface both so approval/queue-skip/no-mapping
        # and plan-repeat decisions stay visible through the legacy shape.
        skipped_view: list[dict] = []
        for a in actions:
            st = str(a.get("status", ""))
            if st in ("blocked", "skipped"):
                err = str(a.get("error", "") or "")
                skipped_view.append({"type": "action", "value": str(a.get("kind", "")), "reason": err or st})
        for e in errs:
            m = str(e.get("message", ""))
            low = m.lower()
            if "skipped repeat plan" in low:
                skipped_view.append({"type": "plan", "value": m.split()[-1] if m.split() else "", "reason": m})
            elif "skipped repeat" in low or "skipped confirmation" in low or "queue-time skip" in low or "no typed driver mapping" in low or "owner declined" in low or "llm cap" in low:
                if not any(s.get("reason") == m for s in skipped_view):
                    skipped_view.append({"type": "action", "value": task_id[:8], "reason": m})
        report_path = None
        try:
            cand = _P(self.config.data_dir) / "reports" / f"{task_id}.md"
            report_path = cand if cand.exists() else None
        except Exception:
            report_path = None
        stopped_reason = None
        if errs:
            stopped_reason = str(errs[-1].get("message", "")) or None
        try:
            from .accounting import get_today_count as _cnt

            llm_c = int(_cnt(self.config.data_dir))
        except Exception:
            llm_c = 0
        limits = {
            "actions_used": n_completed,
            "max_actions": int(getattr(self.config, "max_actions", 200)),
            "duration_minutes": 0,
            "max_duration_minutes": int(getattr(self.config, "max_duration_minutes", 45)),
            "llm_calls_today": llm_c,
            "max_llm_calls_per_day": int(getattr(self.config, "max_llm_calls_per_day", 150)),
        }
        return ExecutionResult(
            task_id=task_id, state=state, plan=plan, actions_executed=n_completed,
            queries=queries, urls=urls, findings=findings, errors=errs,
            skipped_repeats=skipped_view, report_markdown=md, report_path=report_path,
            stopped_reason=stopped_reason, limits=limits,
        )

    async def create_task_async(self, description: str) -> Task:
        return self.create_task(description)

    async def cancel_task(self, task_id: str, reason: str = "cancelled") -> Task | None:
        from .task_lifecycle import Cancel, OutcomeCategory

        out = self.lifecycle.handle(Cancel(task_id=task_id, reason=reason))
        if out.category == OutcomeCategory.not_found:
            return None
        if out.category not in (OutcomeCategory.stopped, OutcomeCategory.ok):
            return None
        data = self.memory.get_task(task_id)
        if not data:
            return None
        return Task(description=data["description"], id=data["id"], state=AgentState(data["state"]))

    def request_emergency_stop(self, reason: str = "emergency stop") -> None:
        """LLM-independent stop routed through the lifecycle owner."""
        from .task_lifecycle import EmergencyStop

        try:
            self.lifecycle.handle(EmergencyStop(reason=reason or "emergency stop"))
        except Exception:
            from .executor import request_emergency_stop as _req

            _req(reason)
            try:
                if hasattr(self.computer, "release_all_inputs"):
                    self.computer.release_all_inputs()
                if hasattr(self.computer, "terminate_agent_processes"):
                    self.computer.terminate_agent_processes()  # type: ignore
            except Exception:
                pass
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

        eff_session = min(ab.get("session_duration_minutes", 45) if isinstance(ab, dict) else 45, _MAX_DURATION_MINUTES)
        eff_actions = min(ab.get("daily_action_limit", 200) if isinstance(ab, dict) else 200, _MAX_ACTIONS)
        eff_llm = min(ab.get("daily_llm_call_limit", 150) if isinstance(ab, dict) else 150, _MAX_LLM_CALLS_PER_DAY)
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
            # Range error vs tighten-only ceiling rejection use distinct texts so
            # callers can tell "invalid range" apart from "above Config ceiling".
            # The ceiling text keeps the valid range suffix so existing
            # `match="1..45"` assertions keep passing.
            if val <= 0:
                raise ValueError("session_duration_minutes must be 1..45")
            if val > _MAX_DURATION_MINUTES:
                raise ValueError(
                    f"session_duration_minutes above ceiling {_MAX_DURATION_MINUTES} (must be 1..{_MAX_DURATION_MINUTES})"
                )
            ab.session_duration_minutes = val
        if "daily_action_limit" in patch and patch["daily_action_limit"] is not None:
            val = int(patch["daily_action_limit"])
            if val <= 0 or val > 1000:
                raise ValueError("daily_action_limit must be 1..1000")
            if val > _MAX_ACTIONS:
                raise ValueError(f"daily_action_limit above ceiling {_MAX_ACTIONS}")
            ab.daily_action_limit = val
        if "daily_llm_call_limit" in patch and patch["daily_llm_call_limit"] is not None:
            val = int(patch["daily_llm_call_limit"])
            if val <= 0 or val > 1000:
                raise ValueError("daily_llm_call_limit must be 1..1000")
            if val > _MAX_LLM_CALLS_PER_DAY:
                raise ValueError(f"daily_llm_call_limit above ceiling {_MAX_LLM_CALLS_PER_DAY}")
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
        eff = _effective_limits(
            ab.session_duration_minutes,
            ab.daily_action_limit,
            ab.daily_llm_call_limit,
            cu.idle_threshold_seconds,
        )
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

    def is_demo_mode(self) -> bool:
        """Demo badge decision — True when no provider key is configured.

        Reuses ProviderStore + Keychain + env fallback (same as server's
        _is_demo_mode). Stub output must never be presented as LLM work.
        Secrets are masked reads only, never leaked.
        """
        try:
            from .keychain import get_default_store
            from .providers.config import ProviderStore

            store = ProviderStore.load(self.config.data_dir)
            selected = store.get_selected()
            if selected is None:
                return True
            kc = get_default_store(self.config.data_dir)
            key = kc.get(selected.name)
            if not key:
                import os

                env_map = {
                    "openrouter": "OPENROUTER_API_KEY",
                    "opencode-go": "OPENCODE_GO_API_KEY",
                }
                env_var = env_map.get(selected.name) or f"{selected.name.upper().replace('-', '_')}_API_KEY"
                key = os.environ.get(env_var) or os.environ.get("OPENAI_API_KEY")
            return not bool(key)
        except Exception:
            return True

    def get_honest_status(self, watch_running: bool | None = None, idle_seconds: float | None = None) -> dict:
        """Single status source driving banner, header chip, and hero (T2).

        Precedence: running session → Limited mode (no provider) → Waiting for idle → Ready.
        Uses self.get_effective_idle_threshold(), self.is_demo_mode(), memory, idle_detector.
        watch_running is observed live state (never owned by app).
        """
        threshold = self.get_effective_idle_threshold()
        # Check for running session via memory active task
        try:
            tasks = self.memory.list_tasks(limit=5)
            active = None
            for t in tasks:
                if t.get("state") in ("running", "planning", "waiting_for_idle", "paused_by_user"):
                    active = t
                    break
            if active and active.get("state") in ("running", "planning"):
                goal = active.get("description", "")[:50]
                return {
                    "text": f"Running — {goal}",
                    "sub": "Session in progress",
                    "level": "running",
                    "dot": "bg-blue-500",
                    "banner_text": f"Running — {goal}",
                    "chip_text": "running",
                    "hero_title": f"Running — {goal}",
                    "hero_sub": "Session in progress — see inspector",
                }
        except Exception:
            pass

        demo = self.is_demo_mode()
        if demo:
            try:
                demo_idle = float(idle_seconds) if idle_seconds is not None else float(self.idle_detector.seconds_since_last_input())
            except Exception:
                demo_idle = float(idle_seconds or 0)
            return {
                "text": "Limited mode — stub planner · LLM off",
                "sub": "Sessions run on stub planner · LLM disabled",
                "level": "limited",
                "dot": "bg-amber-400",
                "banner_text": "Limited mode — stub planner · LLM off",
                "chip_text": "Limited mode",
                "hero_title": "Limited mode — stub planner",
                "hero_sub": f"Idle {demo_idle:.0f}s / {threshold}s · LLM off · threshold {threshold}s",
            }

        try:
            secs = float(idle_seconds) if idle_seconds is not None else float(self.idle_detector.seconds_since_last_input())
        except Exception:
            secs = float(idle_seconds or 0)

        if secs < threshold:
            remaining = threshold - secs
            mins = int(remaining // 60)
            secs_r = int(remaining % 60)
            countdown = f"{mins}m {secs_r}s" if mins else f"{secs_r}s"
            return {
                "text": f"Waiting for idle {secs:.0f}s / {threshold}s",
                "sub": f"Starts when you stay idle · {countdown} remaining",
                "level": "waiting",
                "dot": "bg-amber-400",
                "banner_text": f"Waiting for idle {secs:.0f}s / {threshold}s",
                "chip_text": "Waiting for idle",
                "hero_title": "Waiting for idle",
                "hero_sub": f"Idle {secs:.0f}s / {threshold}s · threshold {threshold}s · Watch loop {'Running' if watch_running else 'Stopped'}",
            }

        return {
            "text": "Ready — idle threshold met",
            "sub": "Agent will start at next idle window",
            "level": "ready",
            "dot": "bg-emerald-500",
            "banner_text": "Ready — idle threshold met",
            "chip_text": "Ready",
            "hero_title": "Ready — idle threshold met",
            "hero_sub": f"Idle {secs:.0f}s / {threshold}s · Next session when idle window holds",
        }

    def get_status_enriched(self, watch_loop: dict | None = None) -> dict:
        """Enriched status behind the Application API (T2).

        Extends get_status() with idle_threshold_seconds, demo_mode,
        honest_status, watch_loop passthrough, limits, daily_usage/today_usage
        duplicates, last_report. Does NOT change existing get_status() keys;
        callers format the returned data.
        watch_loop is observed live state (never owned by app).
        """
        base = self.get_status()
        threshold = self.get_effective_idle_threshold()
        demo = self.is_demo_mode()
        # Derive watch_running for honest_status
        watch_running = None
        if isinstance(watch_loop, dict):
            watch_running = bool(watch_loop.get("running"))
        elif isinstance(watch_loop, bool):
            watch_running = bool(watch_loop)
        honest = self.get_honest_status(watch_running=watch_running, idle_seconds=base.get("idle_seconds"))
        from .accounting import get_today_count

        try:
            llm_today = int(get_today_count(self.config.data_dir))
        except Exception:
            llm_today = int(base.get("llm_calls_today", 0) or 0)
        max_actions = int(getattr(self.config, "max_actions", 200))
        max_duration = int(getattr(self.config, "max_duration_minutes", 45))
        max_llm = int(getattr(self.config, "max_llm_calls_per_day", 150))
        limits = {
            "actions_used_today": None,
            "max_actions": max_actions,
            "max_duration_minutes": max_duration,
            "llm_calls_today": llm_today,
            "max_llm_calls_per_day": max_llm,
        }
        daily_usage = {
            "actions": {"used": 0, "limit": max_actions},
            "llm_calls": {"used": llm_today, "limit": max_llm},
            "duration": {"used": 0, "limit": max_duration},
        }
        # today_usage mirrors daily_usage (same observed counters, kept as a
        # separate key for HTTP/CLI shape compat — one literal, two keys).
        today_usage = {
            "actions": dict(daily_usage["actions"]),
            "llm_calls": dict(daily_usage["llm_calls"]),
            "duration": dict(daily_usage["duration"]),
        }
        last_report = None
        try:
            reports = self.list_reports(limit=1)
            if reports:
                last_report = reports[0]
        except Exception:
            pass
        enriched = dict(base)
        enriched.update(
            {
                "idle_threshold_seconds": threshold,
                "demo_mode": demo,
                "honest_status": honest,
                "watch_loop": watch_loop,
                "limits": limits,
                "daily_usage": daily_usage,
                "today_usage": today_usage,
                "last_report": last_report,
            }
        )
        return enriched

    def get_diagnostics(
        self,
        project_root: Path | str | None = None,
        watch_loop: dict | None = None,
    ) -> dict:
        """Diagnostics decision bundle behind the Application API (T4).

        Single source for: macOS Permissions checks, ComputerDriver probe,
        Profile validity, secrets scan, scheduler lock, watch loop observed
        state, plus effective threshold/limits for context.

        Callers (CLI doctor, HTTP diagnostics) only render; they do not
        re-implement decisions. Secrets are masked (no snippet, source+pattern
        only). Profile validity uses T3 settings authority
        (check_profile_confirmed / validate_profile). Imports inside method
        to avoid cycles and hard driver dep.
        """
        # Effective threshold/limits for context (tighten-only via T3).
        try:
            effective_threshold = int(self.get_effective_idle_threshold())
        except Exception:
            effective_threshold = int(getattr(self.config, "idle_threshold_seconds", 600))
        try:
            owner = self.get_owner_settings()
            effective_limits = owner.get("effective", {})
        except Exception:
            effective_limits = {
                "session_duration_minutes": 45,
                "daily_action_limit": 200,
                "daily_llm_call_limit": 150,
                "idle_threshold_seconds": effective_threshold,
            }

        # Permissions (remediation identical on both surfaces).
        try:
            from .profile.permissions import check_permissions

            perms = check_permissions()
            perms_data = [
                {"name": p.name, "granted": p.granted, "remediation": p.remediation} for p in perms
            ]
        except Exception as e:
            perms_data = [
                {"name": "permissions_check", "granted": None, "remediation": f"check failed: {e}"}
            ]

        # Driver probe (avoid hard dep on cua_driver). Structured fields are the
        # contract for renderers; `message` stays human-readable and identical.
        driver_ok = False
        driver_msg = "cua-driver not installed — pip install cua-driver==0.23.2"
        driver_version: str = "unknown"
        driver_accessibility: bool | None = None
        driver_screen_recording: bool | None = None
        driver_probe_error: str | None = None
        try:
            import cua_driver  # type: ignore

            ver = getattr(cua_driver, "__version__", "unknown")
            driver_version = str(ver)
            driver_ok = True
            driver_msg = f"cua-driver {ver}"
            try:
                status = cua_driver.current_mac_os_permission_status()
                acc = getattr(status, "accessibility", "?")
                scr = getattr(status, "screen_recording", "?")
                driver_msg += f" — accessibility={acc} screen_recording={scr}"
                if isinstance(acc, bool):
                    driver_accessibility = acc
                elif str(acc).lower().startswith("true"):
                    driver_accessibility = True
                elif str(acc).lower().startswith("false"):
                    driver_accessibility = False
                if isinstance(scr, bool):
                    driver_screen_recording = scr
                elif str(scr).lower().startswith("true"):
                    driver_screen_recording = True
                elif str(scr).lower().startswith("false"):
                    driver_screen_recording = False
            except Exception as e:
                driver_probe_error = str(e)
                driver_msg += f" — probe failed: {e}"
        except (ImportError, ModuleNotFoundError):
            driver_ok = False
            driver_msg = "cua-driver not installed — pip install cua-driver==0.23.2"
        except Exception as e:
            driver_ok = False
            driver_probe_error = str(e)
            driver_msg = f"cua-driver probe failed: {e}"

        # Profile validity via T3 authority (check_profile_confirmed + validate_profile).
        try:
            from .profile.validate import validate_profile

            profile = self.get_profile()
            if profile is None:
                profile_valid = False
                profile_confirmed = False
                profile_errors = ["No profile found"]
            else:
                errs = validate_profile(profile)
                profile_confirmed = bool(getattr(profile, "confirmed", False))
                if not profile_confirmed:
                    profile_valid = False
                    profile_errors = list(errs) + ["Profile is unconfirmed"]
                else:
                    profile_valid = len(errs) == 0
                    profile_errors = list(errs)
        except Exception as e:
            profile_valid = False
            profile_confirmed = False
            profile_errors = [f"profile check failed: {e}"]

        # Secrets scan (masked, never leak snippet or key).
        try:
            from .secrets_scan import scan_project

            if project_root is None:
                # Auto-detect repo root (same logic CLI/server used before).
                candidates = [Path.cwd(), Path(__file__).resolve().parents[2]]
                detected = None
                for cand in candidates:
                    try:
                        if (cand / ".git").exists():
                            detected = cand
                            break
                        if (cand / "pyproject.toml").exists() and (cand / "src").exists():
                            if detected is None:
                                detected = cand
                    except Exception:
                        continue
                if detected is None:
                    detected = Path(__file__).resolve().parents[2]
                proj_root = detected
            else:
                proj_root = Path(project_root).expanduser().resolve()
            scan = scan_project(project_root=proj_root, data_dir=self.config.data_dir)
            secrets_ok = bool(scan.ok)
            # Masked: only source + pattern, truncated to contract limit (first 5).
            secrets_findings = [
                {"source": f.source, "pattern": f.pattern} for f in scan.findings[:5]
            ]
            # Keep counts for CLI detailed report without leaking.
            secrets_scanned_files = int(getattr(scan, "scanned_files", 0))
            secrets_scanned_tables = int(getattr(scan, "scanned_db_tables", 0))
        except Exception as e:
            secrets_ok = False
            secrets_findings = [{"source": "scan_failed", "pattern": str(e)}]
            secrets_scanned_files = 0
            secrets_scanned_tables = 0

        # Scheduler lock (import inside to avoid cycle).
        try:
            from .server.lock import get_lock_info, is_locked

            lock_info = get_lock_info(self.config.data_dir)
            locked = bool(is_locked(self.config.data_dir))
        except Exception:
            lock_info = None
            locked = False

        # Watch loop observed state (never owned by app; passed in or default).
        if isinstance(watch_loop, dict):
            watch = dict(watch_loop)
            if "idle_threshold" not in watch:
                watch["idle_threshold"] = effective_threshold
            for k in ("running", "pid", "started_at", "idle_threshold"):
                if k not in watch:
                    watch[k] = None if k != "running" else False
        else:
            watch = {
                "running": False,
                "pid": lock_info.get("pid") if isinstance(lock_info, dict) else None,
                "started_at": None,
                "idle_threshold": effective_threshold,
            }

        return {
            "permissions": perms_data,
            "driver": {
                "ok": driver_ok,
                "message": driver_msg,
                "version": driver_version,
                "accessibility": driver_accessibility,
                "screen_recording": driver_screen_recording,
                "probe_error": driver_probe_error,
            },
            "profile": {"valid": profile_valid, "confirmed": profile_confirmed, "errors": profile_errors},
            "secrets_scan": {
                "ok": secrets_ok,
                "findings": secrets_findings,
                "scanned_files": secrets_scanned_files,
                "scanned_db_tables": secrets_scanned_tables,
            },
            "scheduler_lock": {"locked": locked, "info": lock_info},
            "watch_loop": watch,
            "effective_idle_threshold": effective_threshold,
            "effective_limits": effective_limits,
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

    def start_task_lifecycle(
        self,
        task_id: str | None = None,
        *,
        trigger: str = "explicit",
        mode: str = "unattended",
        confirm_func: Callable | None = None,
    ):
        """Thin delegate: submit one Start command to the lifecycle owner."""
        from .task_lifecycle import Start

        return self.lifecycle.handle(
            Start(task_id=task_id, trigger=trigger, mode=mode, confirm_func=confirm_func)
        )

    def cancel_task_sync(self, task_id: str, reason: str = "cancelled"):
        """Thin delegate: submit one Cancel command to the lifecycle owner."""
        from .task_lifecycle import Cancel

        return self.lifecycle.handle(Cancel(task_id=task_id, reason=reason))

    def get_task_result(self, task_id: str) -> dict:
        """Stable per-task result view for HTTP/CLI adapters (reads only)."""
        res = self._execution_result_from_lifecycle(task_id)
        return {
            "task_id": task_id,
            "state": res.state.value if hasattr(res.state, "value") else str(res.state),
            "actions_executed": res.actions_executed,
            "findings": res.findings,
            "urls": res.urls,
            "errors": res.errors,
            "report_path": str(res.report_path) if res.report_path else None,
        }

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
        """Wait for idle (if wait=True) then run one idle-triggered session.

        The description is enqueued once; the lifecycle owner selects and
        claims work (oldest paused before oldest queued) under one decision path.
        """
        from .task_lifecycle import OutcomeCategory

        scheduler = self.get_scheduler()
        if wait:
            ok = scheduler.wait_for_idle(
                poll_interval=poll_interval, timeout=timeout, threshold_override=idle_threshold_override
            )
            if not ok:
                _thr_msg = int(idle_threshold_override) if idle_threshold_override is not None else self.get_effective_idle_threshold()
                raise RuntimeError(f"Timed out waiting for idle (threshold {_thr_msg}s)")
        task = self.create_task(task_description)
        out = self.start_task_lifecycle(
            task.id,
            trigger="idle",
            mode="interactive" if is_interactive else "unattended",
            confirm_func=confirm_func,
        )
        if out.category in (OutcomeCategory.not_ready, OutcomeCategory.already_active):
            if out.failed_gate == "profile":
                self.ensure_profile_confirmed()
            raise RuntimeError(out.message or "cannot start")
        if out.category in (OutcomeCategory.not_found, OutcomeCategory.invalid_request, OutcomeCategory.invalid_transition):
            raise RuntimeError(out.message or "invalid task transition")
        return self._execution_result_from_lifecycle(out.task_id or task.id)

    def run_idle_loop(
        self,
        task_description: str | None = None,
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
        """Watch loop behind the Application API: poll, then idle-triggered Starts.

        An explicit description is enqueued once up front; every window submits
        `Start(task_id=None, trigger="idle")` so the owner resumes the oldest
        paused Task before starting the oldest queued one.
        """
        if task_description:
            try:
                self.create_task(task_description)
            except Exception:
                pass
        mode = "interactive" if is_interactive else "unattended"

        def _start():
            return self.start_task_lifecycle(task_id=None, trigger="idle", mode=mode, confirm_func=confirm_func)

        return self.get_scheduler().run_loop(
            start_fn=_start,
            poll_interval=poll_interval,
            idle_threshold_override=idle_threshold_override,
            max_sessions=max_sessions,
            once=once,
            timeout_per_wait=timeout_per_wait,
            on_event=on_event,
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
