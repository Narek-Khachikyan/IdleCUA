from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .config import IdleCuaConfig
from .contracts.computer import ComputerDriver, FakeComputerDriver
from .contracts.model import FakeModelProvider, ModelProvider
from .models.plan import Plan
from .models.task import Task
from .planner import StubPlanner

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

    def run_once(self, task_description: str, *, dry_run: bool = False) -> Plan:
        if dry_run:
            return self.dry_run(task_description)
        # Real execution not yet implemented in the walking skeleton.
        raise NotImplementedError(
            "Non-dry-run execution is not implemented in the walking skeleton. Use dry_run=True."
        )

    async def arun_once(self, task_description: str, *, dry_run: bool = False) -> Plan:
        if dry_run:
            return self.dry_run(task_description)
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
        return {
            "goal": plan.goal,
            "target": plan.target,
            "expected_actions": plan.expected_actions,
            "expected_result": plan.expected_result,
            "max_duration_minutes": plan.max_duration_minutes,
            "max_actions": plan.max_actions,
            "risk_level": plan.risk_level.value,
            "requires_confirmation": plan.requires_confirmation,
        }

    def plan_to_json(self, plan: Plan) -> str:
        return json.dumps(self.plan_to_dict(plan), indent=2)
