from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass


@dataclass
class PermissionStatus:
    name: str
    granted: bool | None  # None = unknown / not checkable
    remediation: str


def _check_accessibility() -> PermissionStatus:
    """Check macOS Accessibility permission.

    We attempt multiple heuristics; if we cannot determine, report unknown with remediation.
    """
    name = "Accessibility"
    remediation = (
        "Grant Accessibility: System Settings -> Privacy & Security -> Accessibility "
        "-> enable your terminal (Terminal, iTerm, VS Code) and re-run."
    )
    if platform.system() != "Darwin":
        return PermissionStatus(name=name, granted=None, remediation=remediation + " (non-macOS: check skipped)")
    # Heuristic 1: try to query via AppleScript UI element access — not reliable, fallback to unknown
    # Heuristic 2: check if `tccutil` or `sqlite3` TCC db is readable — not reliable without SIP bypass
    # Best effort: if we can run `osascript` to check System Events, absence of error suggests granted for osascript.
    # However we cannot definitively know for the current app without native API.
    # Use approach: try to use pyobjc if available? Not available by default.
    # So we return unknown with remediation, unless user explicitly grants env override for testing.
    try:
        # Attempt a harmless Accessibility check via `osascript` — if it fails with err -1743, likely not granted
        result = subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to get name of every process'],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            # osascript has access, but does not guarantee our process has it. Mark as unknown.
            return PermissionStatus(
                name=name,
                granted=None,
                remediation=remediation + " (osascript succeeded but per-app grant still required)",
            )
        err = (result.stderr or "").lower()
        if "not authorized" in err or "-1743" in err or "1002" in err:
            return PermissionStatus(name=name, granted=False, remediation=remediation)
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return PermissionStatus(name=name, granted=None, remediation=remediation)


def _check_screen_recording() -> PermissionStatus:
    name = "Screen Recording"
    remediation = (
        "Grant Screen Recording: System Settings -> Privacy & Security -> Screen Recording "
        "-> enable your terminal/app (Terminal, iTerm, VS Code) and re-run. "
        "Required for screenshots and accessibility tree."
    )
    if platform.system() != "Darwin":
        return PermissionStatus(name=name, granted=None, remediation=remediation + " (non-macOS: check skipped)")
    # Heuristic: try `screencapture` to a temp file — fails if not granted on some macOS versions.
    # On newer macOS, screencapture still works but permission status is separate.
    # Use CGWindowListCreateImage style check if pyobjc available; else unknown.
    try:
        # Probe via `screencapture -x -t png` to /tmp — if permission denied, it prints message.
        # We use a dry check: use `CGDisplay` via ` Quartz` if available.
        try:
            import Quartz  # type: ignore

            # Attempt to get display image — if Screen Recording denied, this may return None or require permission
            # Not definitive across OS versions, so treat non-exception as unknown.
            _ = Quartz.CGWindowListCreateImage  # just check availability
            return PermissionStatus(
                name=name, granted=None, remediation=remediation + " (Quartz available — grant still required)"
            )
        except ImportError:
            pass
        # Fallback: try screencapture help — not definitive
        result = subprocess.run(
            ["screencapture", "-h"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        # If command exists, permission check is inconclusive
        return PermissionStatus(name=name, granted=None, remediation=remediation)
    except Exception:
        return PermissionStatus(name=name, granted=None, remediation=remediation)


def check_permissions() -> list[PermissionStatus]:
    return [_check_accessibility(), _check_screen_recording()]


def permissions_report_text(statuses: list[PermissionStatus] | None = None) -> str:
    if statuses is None:
        statuses = check_permissions()
    lines: list[str] = []
    lines.append("macOS Permission Checks")
    lines.append("=======================")
    for s in statuses:
        if s.granted is True:
            lines.append(f"[OK] {s.name}: granted")
        elif s.granted is False:
            lines.append(f"[MISSING] {s.name}: NOT granted")
            lines.append(f"  Remediation: {s.remediation}")
        else:
            # unknown
            lines.append(f"[UNKNOWN] {s.name}: could not determine (assuming NOT granted)")
            lines.append(f"  Remediation: {s.remediation}")
    lines.append("")
    lines.append("Note: On macOS, both Accessibility and Screen Recording must be granted")
    lines.append("to the terminal/app running IdleCUA. After granting, restart the app.")
    return "\n".join(lines)
