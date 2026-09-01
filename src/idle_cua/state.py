from __future__ import annotations

VALID_STATES = {
    "disabled",
    "waiting_for_idle",
    "planning",
    "running",
    "paused_by_user",
    "paused_for_approval",
    "completed",
    "failed",
    "stopped",
}

# Allowed transitions per spec (plain validated transition model)
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "disabled": {"waiting_for_idle"},
    "waiting_for_idle": {"planning", "disabled", "stopped"},
    "planning": {"running", "failed", "stopped", "paused_by_user"},
    "running": {"paused_by_user", "paused_for_approval", "completed", "failed", "stopped"},
    "paused_by_user": {"waiting_for_idle", "stopped", "failed"},
    "paused_for_approval": {"running", "stopped", "failed"},
    "completed": {"waiting_for_idle", "disabled", "stopped"},
    "failed": {"waiting_for_idle", "disabled", "stopped"},
    "stopped": {"disabled", "waiting_for_idle"},
}


def validate_transition(from_state: str, to_state: str) -> None:
    if from_state not in VALID_STATES:
        raise ValueError(f"Unknown from_state: {from_state}")
    if to_state not in VALID_STATES:
        raise ValueError(f"Unknown to_state: {to_state}")
    allowed = ALLOWED_TRANSITIONS.get(from_state, set())
    if to_state not in allowed:
        raise ValueError(f"Illegal transition: {from_state} -> {to_state}")


class AgentStateMachine:
    def __init__(self, initial: str = "disabled") -> None:
        if initial not in VALID_STATES:
            raise ValueError(f"Unknown initial state: {initial}")
        self.state = initial

    def transition(self, to_state: str) -> None:
        validate_transition(self.state, to_state)
        self.state = to_state
