from __future__ import annotations

from enum import Enum

class AgentState(str, Enum):
    disabled = "disabled"
    waiting_for_idle = "waiting_for_idle"
    planning = "planning"
    running = "running"
    paused_by_user = "paused_by_user"
    paused_for_approval = "paused_for_approval"
    completed = "completed"
    failed = "failed"
    stopped = "stopped"

# Validated transition table — plain model, no library.
# See CONTEXT.md and ADR-0006 for state semantics.
# ADR-0006: terminal states are terminal (no reactivation); `disabled`
# describes the Agent, not an individual Task.
_VALID_TRANSITIONS: dict[AgentState, set[AgentState]] = {
    AgentState.disabled: {
        AgentState.waiting_for_idle,
        AgentState.stopped,
    },
    AgentState.waiting_for_idle: {
        AgentState.planning,
        AgentState.running,
        AgentState.stopped,
    },
    AgentState.planning: {
        AgentState.running,
        AgentState.failed,
        AgentState.stopped,
    },
    AgentState.running: {
        AgentState.completed,
        AgentState.failed,
        AgentState.paused_by_user,
        AgentState.paused_for_approval,
        AgentState.stopped,
    },
    AgentState.paused_by_user: {
        AgentState.running,
        AgentState.planning,
        AgentState.stopped,
    },
    AgentState.paused_for_approval: {
        AgentState.running,
        AgentState.stopped,
    },
    AgentState.completed: set(),
    AgentState.failed: set(),
    AgentState.stopped: set(),
}

def is_valid_transition(from_state: AgentState, to_state: AgentState) -> bool:
    return to_state in _VALID_TRANSITIONS.get(from_state, set())

def validate_transition(from_state: AgentState, to_state: AgentState) -> None:
    if not is_valid_transition(from_state, to_state):
        allowed = sorted(s.value for s in _VALID_TRANSITIONS.get(from_state, set()))
        hint = f" allowed from {from_state.value}: {allowed}" if allowed else ""
        raise ValueError(
            f"Illegal transition {from_state.value} -> {to_state.value}." + hint
        )
