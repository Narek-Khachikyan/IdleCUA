"""Shared execution primitives.

The policy-gated execution loop now lives in `task_lifecycle.py` (ADR-0006):
`TaskLifecycle` is the only lifecycle authority. This module keeps only the
process-wide emergency-stop latch, the `ExecutionResult` shape consumed by the
Application API bridge, and the closed driver-call mapping used by the
internal `ActionRunner`.
"""

from __future__ import annotations

import time
import uuid
import threading
import signal
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .config import IdleCuaConfig
from .contracts.computer import ComputerDriver, FakeComputerDriver
from .contracts.model import ModelProvider, FakeModelProvider
from .dedup import normalize_query, normalize_url, url_fingerprint, plan_fingerprint
from .idle import IdleDetector, FakeIdleDetector, InputJournal
from .memory import MemoryStore
from .models.plan import Plan
from .models.state import AgentState
from .models.task import Task
from .planner import LlmCallCapExceeded, LlmPlanner, PlanRejectedError, StubPlanner
from .policy import PolicyEngine, TypedAction, PolicyVerdict
from .report import generate_markdown_report, save_report_to_file


# Global emergency-stop flag — LLM-independent, signal-safe

_emergency_stop_requested = threading.Event()
_emergency_stop_reason: str | None = None
_emergency_lock = threading.Lock()


def request_emergency_stop(reason: str = "emergency stop requested") -> None:
    with _emergency_lock:
        global _emergency_stop_reason
        _emergency_stop_reason = reason
        _emergency_stop_requested.set()


def clear_emergency_stop() -> None:
    with _emergency_lock:
        global _emergency_stop_reason
        _emergency_stop_reason = None
        _emergency_stop_requested.clear()


def is_emergency_stop_requested() -> bool:
    return _emergency_stop_requested.is_set()


def get_emergency_stop_reason() -> str | None:
    with _emergency_lock:
        return _emergency_stop_reason


def install_signal_handlers() -> None:
    """Install SIGINT/SIGTERM handlers that route through the same emergency path."""
    def handler(signum, frame):
        name = signal.Signals(signum).name if hasattr(signal, 'Signals') else str(signum)
        request_emergency_stop(f"SIG{name}")

    try:
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)
    except Exception:
        # May fail in non-main thread
        pass


@dataclass
class ExecutionResult:
    task_id: str
    state: AgentState
    plan: Plan
    actions_executed: int
    queries: list[dict] = field(default_factory=list)
    urls: list[dict] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    skipped_repeats: list[dict] = field(default_factory=list)
    report_markdown: str = ""
    report_path: Path | None = None
    stopped_reason: str | None = None
    limits: dict = field(default_factory=dict)


SUPPORTED_DRIVER_KINDS: set[str] = {
    "open_allowed_site",
    "open_link",
    "search",
    "read_ui",
    "scroll",
    "extract_public_info",
    "save_note",
    "close_own_tab",
    "open_app",
}


@dataclass
class ExecutorConfig:
    """Tunable execution parameters."""
    pacing_seconds: float = 0.05  # human-like pacing between actions
    per_site_cap: int = 20  # soft cap per domain
    verify_significant: bool = True


def _action_to_driver_call(
    action: TypedAction,
    driver: ComputerDriver,
    task_description: str,
    profile: Any | None = None,
) -> tuple[str, Callable, str | None]:
    """Map typed action to a single driver call — minimal closed vocabulary, no fallback chains."""
    kind = action.kind.strip()

    if kind in ("open_allowed_site", "open_link"):
        url = action.target_url or f"https://{action.effective_domain or 'google.com'}"
        def do():
            if hasattr(driver, "open_browser_tab") and hasattr(driver, "has_browser_consent"):
                try:
                    if bool(driver.has_browser_consent()):  # type: ignore
                        driver.open_browser_tab(url)  # type: ignore
                        return
                except Exception:
                    pass
            driver.open_url(url)
        return kind, do, url

    if kind == "search":
        query = action.description or task_description
        url = action.target_url or "https://google.com"
        def do_search():
            driver.open_url(url)
            driver.type_text(query)
            driver.press("Enter")
        return kind, do_search, url

    if kind == "read_ui":
        def do_read():
            driver.screenshot()
            try:
                driver.get_accessibility_tree()
            except Exception:
                pass
        return kind, do_read, action.target_url

    if kind == "scroll":
        def do_scroll():
            driver.scroll(0, 300)
        return kind, do_scroll, action.target_url

    if kind == "extract_public_info":
        def do_extract():
            driver.screenshot()
            try:
                driver.get_accessibility_tree()
            except Exception:
                pass
        return kind, do_extract, action.target_url

    if kind == "save_note":
        def do_save():
            pass
        return kind, do_save, None

    if kind == "close_own_tab":
        tab = action.target_url
        def do_close():
            if hasattr(driver, "close_tab"):
                try:
                    driver.close_tab(tab)  # type: ignore
                    return
                except Exception:
                    pass
            if hasattr(driver, "close_all_agent_tabs"):
                try:
                    driver.close_all_agent_tabs()  # type: ignore
                except Exception:
                    pass
        return kind, do_close, tab

    if kind == "open_app":
        raw = (action.description or task_description or "Calculator").strip()
        low = raw.lower()
        known = {
            "calculator": "Calculator",
            "textedit": "TextEdit",
            "google chrome": "Google Chrome",
            "system settings": "System Settings",
        }
        app_name = None
        for cand, mapped in known.items():
            if cand in low:
                app_name = mapped
                break
        if app_name is None:
            import re as _re
            m = _re.search(r"launch\s+([a-zA-Z ]+)", raw, _re.IGNORECASE)
            if m:
                app_name = m.group(1).strip().title()
            else:
                app_name = "Calculator"
        def do_open_app():
            driver.open_app(app_name)  # type: ignore
        return kind, do_open_app, None

    # Unknown/unsupported kinds are treated as no-op but still policy-checked upstream
    # For confirmation-required kinds with no typed driver mapping in MVP,
    # the caller must surface as skipped (spec sanctions skip-and-surface).
    def do_noop():
        pass
    return kind, do_noop, action.target_url


