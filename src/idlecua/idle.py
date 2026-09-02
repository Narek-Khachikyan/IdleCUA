from __future__ import annotations

import ctypes
import ctypes.util
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional


class IdleDetector(ABC):
    """Hardware-level idle detection. Synthetic input must never mask return."""

    @abstractmethod
    def seconds_since_last_input(self) -> float:
        """Seconds since last hardware input (HID). Synthetic must not reset."""
        ...

    def is_idle(self, threshold_seconds: int = 600) -> bool:
        try:
            return self.seconds_since_last_input() >= threshold_seconds
        except Exception:
            return False

    @abstractmethod
    def is_screen_locked(self) -> bool:
        ...

    def can_run(self, threshold_seconds: int = 600) -> tuple[bool, str]:
        """Gate check: idle >= threshold, screen unlocked."""
        if self.is_screen_locked():
            return False, "screen locked"
        idle = self.seconds_since_last_input()
        if idle < threshold_seconds:
            return False, f"idle {idle:.1f}s < threshold {threshold_seconds}s"
        return True, f"idle {idle:.1f}s >= threshold {threshold_seconds}s"


class QuartzIdleDetector(IdleDetector):
    """macOS Quartz HID hardware-event idle timer.

    Uses CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateHIDSystemState, kCGAnyInputEventType)
    so synthetic app-level events never reset the clock. Validated by day-1 spike.

    Falls back to combined state or time-based if Quartz unavailable.
    """

    def __init__(self) -> None:
        self._use_quartz = False
        self._quartz_func = None
        self._watchdog_start = time.monotonic()
        self._watchdog_last_hardware = self._watchdog_start
        self._probe_quartz()

    def _probe_quartz(self) -> None:
        if sys.platform != "darwin":
            return
        try:
            # Try pyobjc Quartz if available
            try:
                from Quartz import CGEventSourceSecondsSinceLastEventType, kCGEventSourceStateHIDSystemState, kCGAnyInputEventType  # type: ignore

                def _call() -> float:
                    return float(CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateHIDSystemState, kCGAnyInputEventType))

                # test call
                v = _call()
                if v >= 0:
                    self._quartz_func = _call
                    self._use_quartz = True
                    return
            except Exception:
                pass

            # Fallback: ctypes via ApplicationServices / CoreGraphics
            cgs = ctypes.util.find_library("CoreGraphics")
            if not cgs:
                return
            lib = ctypes.CDLL(cgs)
            # CGEventSourceSecondsSinceLastEventType(CGEventSourceStateID, CGEventType) -> CFTimeInterval (double)
            try:
                lib.CGEventSourceSecondsSinceLastEventType.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
                lib.CGEventSourceSecondsSinceLastEventType.restype = ctypes.c_double
                # Constants: kCGEventSourceStateHIDSystemState = 1, kCGAnyInputEventType = ~0U (or 0xFFFFFFFF), but we use 0xFFFFFFFF
                # Use 1 and 0xFFFFFFFF
                def _ct_call() -> float:
                    return float(lib.CGEventSourceSecondsSinceLastEventType(1, 0xFFFFFFFF))

                v = _ct_call()
                if v >= 0 and v < 1e9:
                    self._quartz_func = _ct_call
                    self._use_quartz = True
            except Exception:
                pass
        except Exception:
            self._use_quartz = False

    def seconds_since_last_input(self) -> float:
        if self._use_quartz and self._quartz_func is not None:
            try:
                v = float(self._quartz_func())
                if v >= 0 and v < 1e9:
                    return v
            except Exception:
                pass
        # Fallback when Quartz HID unavailable: conservative — report not idle (0) to avoid
        # running while owner is present undetected. Synthetic input must never mask return,
        # and without HID we cannot prove idle, so we refuse to claim idle. Callers that
        # need time-based fallback must explicitly call note_hardware_input() on real input.
        # Using watchdog_start alone would incorrectly report growing idle and mask presence.
        if self._watchdog_last_hardware != self._watchdog_start:
            return time.monotonic() - self._watchdog_last_hardware
        return 0.0

    def is_screen_locked(self) -> bool:
        if sys.platform != "darwin":
            return False
        try:
            # Check via ioreg or python
            # Use `ioreg -n Root -d1 | grep CGSSessionScreenIsLocked` style
            out = subprocess.run(
                ["python3", "-c", "import Quartz; print(Quartz.CGSessionCopyCurrentDictionary())"],
                capture_output=True, text=True, timeout=1.0,
            )
            if out.returncode == 0 and "CGSSessionScreenIsLocked" in out.stdout:
                return "True" in out.stdout and "CGSSessionScreenIsLocked = 1" in out.stdout
        except Exception:
            pass
        try:
            # Fallback: check via `pgrep ScreenSaverEngine` or `loginwindow` lock?
            # Use `ioreg` via subprocess
            r = subprocess.run(["ioreg", "-n", "Root", "-d1"], capture_output=True, text=True, timeout=0.8)
            if r.returncode == 0:
                if "CGSSessionScreenIsLocked" in r.stdout:
                    # crude parse
                    return "kCGSSessionScreenIsLocked = 1" in r.stdout or '"CGSSessionScreenIsLocked" = Yes' in r.stdout
        except Exception:
            pass
        # If we cannot determine, assume not locked (hard gate default is unlocked required, so false is permissive)
        return False

    def note_hardware_input(self) -> None:
        """For watchdog fallback: external caller can signal hardware input detected."""
        self._watchdog_last_hardware = time.monotonic()


class FakeIdleDetector(IdleDetector):
    """In-memory fake for tests — manual control over idle time and lock state.

    - `idle_seconds` controls seconds_since_last_input() return.
    - `locked` controls is_screen_locked().
    - `hardware_input()` simulates user return: sets idle to 0 and records event.
    - Supports `advance(seconds)` to simulate time passing while idle.
    """

    def __init__(self, idle_seconds: float = 1000.0, locked: bool = False) -> None:
        self._idle = float(idle_seconds)
        self._locked = bool(locked)
        self._hardware_events: list[float] = []

    def seconds_since_last_input(self) -> float:
        return float(self._idle)

    def is_screen_locked(self) -> bool:
        return bool(self._locked)

    # helpers for tests

    def set_idle(self, seconds: float) -> None:
        self._idle = float(seconds)

    def set_locked(self, locked: bool) -> None:
        self._locked = bool(locked)

    def hardware_input(self) -> None:
        """Simulate user hardware input — resets idle to 0."""
        self._idle = 0.0
        self._hardware_events.append(time.monotonic())

    def advance(self, seconds: float) -> None:
        self._idle += float(seconds)

    def can_run(self, threshold_seconds: int = 600) -> tuple[bool, str]:
        if self._locked:
            return False, "screen locked (fake)"
        if self._idle < threshold_seconds:
            return False, f"fake idle {self._idle:.1f}s < threshold {threshold_seconds}s"
        return True, f"fake idle {self._idle:.1f}s >= threshold {threshold_seconds}s"


# Input journal for held keys/buttons release

class InputJournal:
    """Records held input so emergency stop / user-return can release synchronously."""

    def __init__(self) -> None:
        self._held_keys: set[str] = set()
        self._held_buttons: set[str] = set()
        self._log: list[str] = []

    def press_key(self, key: str) -> None:
        self._held_keys.add(key)
        self._log.append(f"press:{key}")

    def release_key(self, key: str) -> None:
        self._held_keys.discard(key)
        self._log.append(f"release:{key}")

    def hold_button(self, button: str = "left") -> None:
        self._held_buttons.add(button)
        self._log.append(f"hold:{button}")

    def release_button(self, button: str = "left") -> None:
        self._held_buttons.discard(button)
        self._log.append(f"release_button:{button}")

    def release_all(self) -> list[str]:
        """Synchronously release all held keys/buttons. Returns released items."""
        released: list[str] = []
        for k in list(self._held_keys):
            released.append(f"key:{k}")
            self._held_keys.discard(k)
        for b in list(self._held_buttons):
            released.append(f"button:{b}")
            self._held_buttons.discard(b)
        if released:
            self._log.append(f"release_all:{','.join(released)}")
        return released

    @property
    def held_keys(self) -> set[str]:
        return set(self._held_keys)

    @property
    def held_buttons(self) -> set[str]:
        return set(self._held_buttons)

    @property
    def log(self) -> list[str]:
        return list(self._log)

    def reset(self) -> None:
        self._held_keys.clear()
        self._held_buttons.clear()
        self._log.clear()

    def is_empty(self) -> bool:
        return not self._held_keys and not self._held_buttons
