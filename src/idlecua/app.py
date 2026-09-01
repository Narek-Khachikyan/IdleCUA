from __future__ import annotations

import json
from pathlib import Path

from .config import IdleCuaConfig
from .contracts.computer import ComputerDriver, FakeComputerDriver
from .contracts.model import FakeModelProvider, ModelProvider
from .models.plan import Plan
from .models.task import Task
from .planner import StubPlanner
from .policy import PolicyEngine, PolicyResult, PolicyVerdict, TypedAction



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

    def __init__(
        self,
        config: IdleCuaConfig | None = None,
        *,
        computer: ComputerDriver | None = None,
        model_provider: ModelProvider | None = None,
        planner: StubPlanner | None = None,
        policy: PolicyEngine | None = None,
    ) -> None:
        self.config = config or IdleCuaConfig()
        self.computer: ComputerDriver = computer or FakeComputerDriver()
        # If caller injects a provider (tests), use it verbatim — preserves fake path.
        # Otherwise try to resolve the selected OpenAI-compatible provider from disk/keychain.
        if model_provider is not None:
            self.model_provider: ModelProvider = model_provider
        else:
            self.model_provider = self._resolve_model_provider()
        self.planner = planner or StubPlanner()
        self.policy: PolicyEngine = policy or PolicyEngine(self.config)

    def _resolve_model_provider(self) -> ModelProvider:
        """Load the selected provider via the ModelProvider contract, or fallback to Fake.

        This keeps the fake working in tests and on fresh installs with no provider
        configured, while the Application uses the real adapter when configured.
        """
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
                # Also try env fallbacks (OPENROUTER_API_KEY etc.) without file
                import os

                env_map = {
                    "openrouter": "OPENROUTER_API_KEY",
                    "opencode-go": "OPENCODE_GO_API_KEY",
                }
                env_var = env_map.get(selected.name) or f"{selected.name.upper().replace('-', '_')}_API_KEY"
                api_key = os.environ.get(env_var) or os.environ.get("OPENAI_API_KEY")
            if not api_key:
                return FakeModelProvider()
            # Wire accounting hook to this data_dir so daily cap can be enforced later
            return OpenAICompatibleProvider(
                config=selected, api_key=api_key, data_dir=Path(self.config.data_dir)
            )
        except Exception:
            return FakeModelProvider()

    # -- task model --

    def create_task(self, description: str) -> Task:
        return Task(description=description)

    async def acreate_task(self, description: str) -> Task:
        return self.create_task(description)

    # -- policy gating --

    def check_action(self, action: TypedAction) -> PolicyResult:
        """Check a single typed action against the PolicyEngine (read-only gate).

        Every typed action must pass this gate before dispatch. The layered
        evaluation order is: allowlist → deny-zones → action classification.
        """
        return self.policy.evaluate(action)

    def can_execute(
        self,
        action: TypedAction,
        *,
        is_interactive: bool = False,
        confirmed: bool = False,
    ) -> tuple[bool, PolicyResult]:
        """Whether an action may be dispatched, respecting readonly and confirmation.

        - allowed → True
        - needs_confirmation → only if not readonly, interactive, and confirmed
        - blocked → False (hard-blocked)

        The underlying verdict is still available as the second tuple element
        for dry-run labeling.
        """
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
        # blocked
        return False, result

    def execute_action(
        self,
        action: TypedAction,
        *,
        is_interactive: bool = False,
        confirmed: bool = False,
    ) -> PolicyResult:
        """Gate + dispatch a single action. Raises PermissionError if blocked.

        This is the single chokepoint every typed action must pass before any
        ComputerDriver call. Dry-run never calls this.
        """
        ok, result = self.can_execute(action, is_interactive=is_interactive, confirmed=confirmed)
        if not ok:
            raise PermissionError(f"Policy blocked action '{action.kind}': {result.reason} (verdict={result.verdict.value})")
        # In the skeleton we do not actually dispatch to ComputerDriver for typed actions
        # that would require real UI; the fake driver is used only in focused tests.
        # Here we record a synthetic dispatch for observability if needed.
        return result

    # -- planning / dry-run --

    def plan(self, task_description: str) -> Plan:
        if not task_description or not task_description.strip():
            raise ValueError("task_description must be non-empty")
        return self.planner.plan(task_description)

    async def aplan(self, task_description: str) -> Plan:
        return self.plan(task_description)

    def dry_run(self, task_description: str) -> Plan:
        """Deterministic dry-run: produces a bounded Plan and executes zero driver actions.

        This is the same as :meth:`plan` but named for the CLI/API promise
        that no computer action is performed. Never calls ComputerDriver or ModelProvider.
        """
        return self.plan(task_description)

    async def adry_run(self, task_description: str) -> Plan:
        return self.dry_run(task_description)

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

    def get_plan_verdicts(self, plan: Plan) -> list[dict]:
        """Return per-action policy verdicts for a plan (for dry-run labeling).

        Each expected_action string is evaluated as a TypedAction targeting
        plan.target (or locally if the action is local-only). This provides
        the allowed / needs-confirmation / blocked labels required by the CLI.
        """
        verdicts: list[dict] = []
        # Actions that are local and don't require a target URL
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
        """Return a dict representation of a plan annotated with policy verdicts."""
        base = self.plan_to_dict(plan)
        base["action_verdicts"] = self.get_plan_verdicts(plan)
        return base

    def run_once(self, task_description: str, *, dry_run: bool = False) -> Plan:
        if dry_run:
            return self.dry_run(task_description)
        # Hard gate before any autonomous work
        self.ensure_profile_confirmed()
        # Real execution not yet implemented in the walking skeleton.
        raise NotImplementedError(
            "Non-dry-run execution is not implemented in the walking skeleton. Use dry_run=True."
        )

    async def arun_once(self, task_description: str, *, dry_run: bool = False) -> Plan:
        if dry_run:
            return self.dry_run(task_description)
        self.ensure_profile_confirmed()
        raise NotImplementedError(
            "Non-dry-run execution is not implemented in the walking skeleton. Use dry_run=True."
        )

    # Back-compat aliases matching the MVP spec sketch
    async def create_task_async(self, description: str) -> Task:
        return self.create_task(description)

    # -- config persistence helpers (used by CLI init) --

    def init_data_dir(self) -> Path:
        """Create the data directory and default config file. Idempotent."""
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.config.config_path.exists():
            self.config.save()
        return self.config.data_dir

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
        # Always include per-action policy verdicts for dry-run labeling
        try:
            base["action_verdicts"] = self.get_plan_verdicts(plan)
        except Exception:
            # Never break serialization; policy is the gate, not storage
            base["action_verdicts"] = []
        return base

    def plan_to_json(self, plan: Plan) -> str:
        return json.dumps(self.plan_to_dict(plan), indent=2)
