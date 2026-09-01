from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

class RiskLevel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"

@dataclass(frozen=True)
class Plan:
    goal: str
    target: str
    expected_actions: list[str] = field(default_factory=list)
    expected_result: str = ""
    max_duration_minutes: int = 45
    max_actions: int = 50
    risk_level: RiskLevel = RiskLevel.low
    requires_confirmation: bool = False

    def __post_init__(self) -> None:
        if not self.goal or not self.goal.strip():
            raise ValueError("Plan.goal must be non-empty")
        if not self.target or not self.target.strip():
            raise ValueError("Plan.target must be non-empty")
        if self.max_duration_minutes <= 0 or self.max_duration_minutes > 45:
            raise ValueError("max_duration_minutes must be 1..45")
        if self.max_actions <= 0 or self.max_actions > 200:
            raise ValueError("max_actions must be 1..200")
        if not self.expected_actions:
            raise ValueError("expected_actions must be non-empty")
