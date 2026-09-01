from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PlanItem:
    goal: str
    target: str
    expected_actions: list[str]
    max_duration_minutes: int
    max_actions: int
    risk: str  # low / medium / high
    confirmation_needed: bool


@dataclass
class Plan:
    task: str
    items: list[PlanItem]


def stub_plan(task: str) -> Plan:
    """Deterministic stub planner (no LLM) — bounded typed plan."""
    base = task.strip() or "research"
    return Plan(
        task=base,
        items=[
            PlanItem(
                goal=f"Research {base}",
                target="google.com",
                expected_actions=["search", "open link", "extract"],
                max_duration_minutes=10,
                max_actions=20,
                risk="low",
                confirmation_needed=False,
            ),
            PlanItem(
                goal=f"Summarize findings for {base}",
                target="local",
                expected_actions=["save note"],
                max_duration_minutes=5,
                max_actions=5,
                risk="low",
                confirmation_needed=False,
            ),
        ],
    )


def render_plan(plan: Plan) -> str:
    lines: list[str] = []
    lines.append(f"Task: {plan.task}")
    lines.append(f"Plan items: {len(plan.items)}")
    for i, item in enumerate(plan.items, 1):
        lines.append(f"  {i}. Goal: {item.goal}")
        lines.append(f"     Target: {item.target}")
        lines.append(f"     Expected actions: {', '.join(item.expected_actions)}")
        lines.append(f"     Max duration: {item.max_duration_minutes} min, Max actions: {item.max_actions}, Risk: {item.risk}")
        lines.append(f"     Confirmation needed: {item.confirmation_needed}")
    return "\n".join(lines)
