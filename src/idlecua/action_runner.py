"""Internal ActionRunner — dispatches and verifies one prepared Action.

Owned by TaskLifecycle; the previous broad execution module no longer owns
lifecycle behavior. Returns a typed outcome; persistence ordering and policy
decisions stay in TaskLifecycle.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ActionOutcome:
    kind: str
    status: str  # completed | failed | skipped | blocked | outcome_unknown
    error: str | None = None
    target_url: str | None = None
    verdict: str = "allowed"


def dispatch_one(
    action_kind: str,
    driver: Any,
    task_description: str,
    target_url: str | None = None,
) -> tuple[str, Callable, str | None]:
    """Map one typed action to a single driver call (closed vocabulary)."""
    # Import lazily to keep this module free of executor lifecycle imports.
    from .executor import _action_to_driver_call
    from .policy import TypedAction

    action = TypedAction(kind=action_kind, target_url=target_url, description=task_description)
    _kind, fn, url = _action_to_driver_call(action, driver, task_description, None)
    return _kind, fn, url


def verify_significant(driver: Any, kind: str, url_for_record: str | None, before_snapshot: str | None) -> str | None:
    """Verify-by-reread for significant actions. Returns error text or None."""
    import json as _json

    if kind not in ("open_allowed_site", "open_link", "search", "extract_public_info"):
        return None
    try:
        if hasattr(driver, "verify_browser_state"):
            v = driver.verify_browser_state(expected_url_contains=url_for_record)  # type: ignore
            verified = bool(v.get("verified", True)) if isinstance(v, dict) else True
            try:
                after_snapshot = _json.dumps(driver.get_browser_state())  # type: ignore
            except Exception:
                after_snapshot = _json.dumps(v) if isinstance(v, dict) else str(v)
            if verified:
                if before_snapshot is not None and after_snapshot is not None and before_snapshot == after_snapshot:
                    if kind not in ("read_ui", "extract_public_info"):
                        return f"verify: state unchanged after {kind} for {url_for_record}"
                return None
            return f"verify_browser_state not verified for {url_for_record}"
        after = driver.get_accessibility_tree()
        after_snapshot = _json.dumps(after)
        if before_snapshot is not None and after_snapshot == before_snapshot and kind not in ("read_ui", "extract_public_info"):
            return f"verify: accessibility tree unchanged after {kind}"
        return None
    except Exception as e:
        return str(e)


def snapshot_for_verify(driver: Any, kind: str | None = None) -> str | None:
    try:
        if hasattr(driver, "get_browser_state"):
            return json.dumps(driver.get_browser_state())  # type: ignore
        return json.dumps(driver.get_accessibility_tree())
    except Exception:
        return None
