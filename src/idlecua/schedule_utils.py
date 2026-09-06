"""Shared schedule helper — single source for allowed_hours parsing.

Used by TaskLifecycle and IdleScheduler to avoid duplicated HH:MM parsing.
"""
from __future__ import annotations

import datetime as dt


def is_within_allowed_hours(allowed: str, now: dt.time | None = None) -> tuple[bool, str]:
    """Return (ok, allowed) whether now is within allowed HH:MM-HH:MM.

    Handles overnight wrap (e.g., 22:00-06:00). Returns (True, "24/7") for
    00:00-23:59 shortcut. On parse failure, returns (False, allowed) (fail-closed).
    """
    if allowed is None:
        return True, "no profile"
    allowed = allowed.strip() or "00:00-23:59"
    if allowed == "00:00-23:59":
        return True, "24/7"
    try:
        start_s, end_s = allowed.split("-")
        sh, sm = map(int, start_s.split(":"))
        eh, em = map(int, end_s.split(":"))
        start_t = dt.time(sh, sm)
        end_t = dt.time(eh, em)
        cur = now or dt.datetime.now().time()
        if start_t <= end_t:
            ok = start_t <= cur <= end_t
        else:
            ok = cur >= start_t or cur <= end_t
        return ok, allowed
    except Exception:
        return False, allowed
