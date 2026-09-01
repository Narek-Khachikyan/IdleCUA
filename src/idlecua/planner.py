from __future__ import annotations

import hashlib

from .models.plan import Plan, RiskLevel

# Bounded typed action vocabulary — closed, grows only on demonstrated need.
_ALLOWED_ACTIONS = [
    "open_allowed_site",
    "search",
    "read_ui",
    "scroll",
    "open_link",
    "extract_public_info",
    "save_note",
    "close_own_tab",
]

# Simple deterministic risk heuristic for the stub.
_HIGH_RISK_KEYWORDS = {"delete", "purchase", "payment", "post", "publish", "send", "message"}
_MEDIUM_RISK_KEYWORDS = {"follow", "like", "download", "edit"}

def _risk_for(description: str) -> RiskLevel:
    lower = description.lower()
    if any(k in lower for k in _HIGH_RISK_KEYWORDS):
        return RiskLevel.high
    if any(k in lower for k in _MEDIUM_RISK_KEYWORDS):
        return RiskLevel.medium
    return RiskLevel.low

def _target_for(description: str) -> str:
    """Deterministically pick a first-tier target for the plan."""
    lower = description.lower()
    if "reddit" in lower:
        return "reddit.com"
    if "youtube" in lower or "video" in lower:
        return "youtube.com"
    if "github" in lower or "code" in lower:
        return "github.com"
    if "paper" in lower or "arxiv" in lower:
        return "arxiv.org"
    if "x.com" in lower or "twitter" in lower or "tweet" in lower:
        return "x.com"
    # deterministic fallback: hash -> one of first-tier
    h = int(hashlib.sha256(description.encode()).hexdigest(), 16)
    first_tier = ["x.com", "reddit.com", "youtube.com"]
    return first_tier[h % len(first_tier)]

def _expected_actions_for(description: str, risk: RiskLevel) -> list[str]:
    # Deterministic, bounded; high-risk still plans read-only actions in the skeleton.
    base = ["open_allowed_site", "search", "read_ui", "scroll", "extract_public_info", "save_note"]
    # vary slightly by hash so different descriptions produce visibly different but bounded plans
    h = int(hashlib.sha256(description.encode()).hexdigest(), 16)
    if h % 3 == 0:
        return base
    if h % 3 == 1:
        return ["open_allowed_site", "search", "read_ui", "open_link", "extract_public_info", "save_note"]
    return ["open_allowed_site", "search", "read_ui", "scroll", "open_link", "extract_public_info", "save_note", "close_own_tab"]

class StubPlanner:
    """Deterministic stub planner — no LLM.

    Produces a bounded, typed Plan from a task description. Same input always yields
    the same output. Never calls ComputerDriver or ModelProvider.
    """

    def plan(self, task_description: str) -> Plan:
        if not task_description or not task_description.strip():
            raise ValueError("task_description must be non-empty")
        desc = task_description.strip()
        risk = _risk_for(desc)
        target = _target_for(desc)
        actions = _expected_actions_for(desc, risk)
        # Bounded limits — 45 min / 200 actions are hard caps (see config).
        # Stub uses a deterministic small slice.
        h = int(hashlib.sha256(desc.encode()).hexdigest(), 16)
        max_duration = 10 + (h % 36)  # 10..45
        max_actions = 10 + (h % 41)   # 10..50 (well under 200 cap)
        requires_confirmation = risk != RiskLevel.low

        return Plan(
            goal=desc,
            target=target,
            expected_actions=actions,
            expected_result=f"Collected findings for: {desc}",
            max_duration_minutes=max_duration,
            max_actions=max_actions,
            risk_level=risk,
            requires_confirmation=requires_confirmation,
        )

# Convenience singleton
_default_planner = StubPlanner()

def plan_task(task_description: str) -> Plan:
    return _default_planner.plan(task_description)
