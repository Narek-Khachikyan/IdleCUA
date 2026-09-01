from __future__ import annotations

from pathlib import Path

from .config import IdleCuaConfig
from .profile.store import load_profile
from .profile.validate import validate_profile
from .computer import ComputerDriver, FakeComputerDriver
from .model_provider import ModelProvider, FakeModelProvider
from .state import AgentStateMachine
from .planner import stub_plan, Plan


class ProfileNotConfirmedError(RuntimeError):
    pass


class Application:
    """Public SDK — thin over profile/config, enforces hard gate."""

    def __init__(
        self,
        config: IdleCuaConfig | None = None,
        computer: ComputerDriver | None = None,
        model_provider: ModelProvider | None = None,
    ) -> None:
        self.config = config or IdleCuaConfig()
        self.computer = computer or FakeComputerDriver()
        self.model_provider = model_provider or FakeModelProvider()
        self.state_machine = AgentStateMachine(initial="disabled")

    def ensure_profile_confirmed(self) -> None:
        profile = load_profile(self.config.profile_path)
        if profile is None:
            raise ProfileNotConfirmedError(
                f"No profile found at {self.config.profile_path}. Run `idle-cua profile interview` and confirm."
            )
        if not profile.confirmed:
            raise ProfileNotConfirmedError(
                f"Profile at {self.config.profile_path} is unconfirmed. Complete `idle-cua profile interview` and confirm, or `idle-cua profile show` to inspect. Autonomous runs are blocked until the profile is confirmed."
            )
        # Also optionally validate
        errors = validate_profile(profile)
        if errors:
            raise ProfileNotConfirmedError(
                f"Profile at {self.config.profile_path} is confirmed but invalid: {'; '.join(errors)}. Run `idle-cua profile validate`."
            )

    def create_plan(self, task: str) -> Plan:
        # Creating a plan is allowed even unconfirmed (dry-run audit), but running is gated.
        return stub_plan(task)

    async def run_task(self, task: str, dry_run: bool = False) -> Plan:
        """Create plan and, if not dry_run, enforce profile gate before executing."""
        plan = stub_plan(task)
        if dry_run:
            return plan
        # Hard gate: no autonomous action while unconfirmed
        self.ensure_profile_confirmed()
        # For MVP with fake driver, we don't actually execute actions here; real loop lands in #6.
        # Transition to planning -> running -> completed for demo, but only if profile confirmed.
        self.state_machine.transition("waiting_for_idle")
        self.state_machine.transition("planning")
        self.state_machine.transition("running")
        # No ComputerDriver calls for stub; real execution would iterate plan.items
        self.state_machine.transition("completed")
        return plan

    def check_profile_confirmed(self) -> tuple[bool, str]:
        try:
            self.ensure_profile_confirmed()
            return True, "Profile is confirmed and valid."
        except ProfileNotConfirmedError as e:
            return False, str(e)
