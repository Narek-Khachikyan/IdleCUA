from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .state import AgentState, validate_transition

@dataclass
class Task:
    description: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    state: AgentState = AgentState.disabled
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not self.description or not self.description.strip():
            raise ValueError("Task description must be non-empty")
        # coerce str -> AgentState if needed
        if isinstance(self.state, str):
            self.state = AgentState(self.state)

    def transition_to(self, new_state: AgentState | str) -> None:
        if isinstance(new_state, str):
            new_state = AgentState(new_state)
        validate_transition(self.state, new_state)
        self.state = new_state

    def can_transition_to(self, new_state: AgentState | str) -> bool:
        if isinstance(new_state, str):
            try:
                new_state = AgentState(new_state)
            except ValueError:
                return False
        from .state import is_valid_transition
        return is_valid_transition(self.state, new_state)
