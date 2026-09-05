from __future__ import annotations

import os
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from ..config import IdleCuaConfig
from ..app import IdleCua
from ..memory import MemoryStore
from ..providers.config import ProviderStore, ProviderConfig
from ..keychain import get_default_store
from ..profile.store import load_profile, save_profile
from ..profile.models import Profile, BrowserConsent
from ..profile.validate import validate_profile
from .lock import acquire_lock, get_lock_info, is_locked
from .humanize import humanize_action


class TaskCreate(BaseModel):
    goal: str
    decisions: dict | None = None


class SettingsPatch(BaseModel):
    session_duration_minutes: int | None = None
    daily_action_limit: int | None = None
    daily_llm_call_limit: int | None = None
    allowed_hours: str | None = None
    allowlist: list[str] | None = None
    deny_zones: list[str] | None = None
    # ADR-0003: single idle threshold in seconds, home is Profile
    idle_threshold_seconds: int | None = None
    readonly: bool | None = None
    require_idle: bool | None = None
    browser_consent: bool | None = None


class ProviderCreate(BaseModel):
    name: str
    base_url: str
    model: str
    api_key: str  # write-only, never returned; masked as •••• on read

    class Config:
        json_schema_extra = {"example": {"name": "openrouter", "base_url": "https://openrouter.ai/api/v1", "model": "anthropic/claude-3.5-sonnet", "api_key": "••••"}}


class PlanPreviewRequest(BaseModel):
    goal: str


# Global per-process scheduler state (live, not reconstructed from SQLite)
_scheduler_state = {
    "running": False,
    "pid": None,
    "started_at": None,
    "idle_threshold": 600,
}

# Watch-loop background worker (serve owns watch loop per ADR-0004)
_watch_thread: threading.Thread | None = None
_watch_stop_event = threading.Event()


def _resolve_data_dir(data_dir: Path | str | None = None) -> Path:
    if data_dir is not None:
        return Path(data_dir).expanduser()
    env = os.environ.get("IDLECUA_DATA_DIR") or os.environ.get("IDLE_CUA_DATA_DIR")
    if env:
        return Path(env).expanduser()
    return IdleCuaConfig().data_dir


def _get_effective_idle_threshold(data_dir: Path) -> int:
    """Thin caller per ADR-0002: delegate to the Application API single source."""
    try:
        return int(_get_idle_cua(data_dir).get_effective_idle_threshold())
    except Exception:
        pass
    try:
        cfg = IdleCuaConfig.load(data_dir)
        return int(getattr(cfg, "idle_threshold_seconds", 600))
    except Exception:
        return 600


def _get_idle_cua(data_dir: Path | None = None) -> IdleCua:
    resolved = _resolve_data_dir(data_dir)
    # Ensure data dir exists (auto-init)
    resolved.mkdir(parents=True, exist_ok=True)
    config = IdleCuaConfig.load(resolved)
    # Keep scheduler_state threshold in sync with the Application API single source (ADR-0003).
    try:
        _thr = int(IdleCua(config=config).get_effective_idle_threshold())
    except Exception:
        _thr = int(getattr(config, "idle_threshold_seconds", 600))
    _scheduler_state["idle_threshold"] = _thr
    return IdleCua(config=config)


def _is_demo_mode(data_dir: Path) -> bool:
    """Thin delegate to Application API — keep name/signature for backward compat."""
    try:
        return _get_idle_cua(data_dir).is_demo_mode()
    except Exception:
        return True


def _fetch_history_filtered(idle_app: IdleCua, task_id: str | None, limit: int = 200) -> dict:
    """Single helper for task-filtered history — used by both API and UI (DRY)."""
    if task_id:
        actions = idle_app.memory.list_actions(task_id=task_id)
        queries = [q for q in idle_app.memory.list_queries(limit=limit) if q.get("task_id") == task_id]
        urls = [u for u in idle_app.memory.list_urls(limit=limit) if u.get("task_id") == task_id]
        findings = idle_app.memory.list_findings(task_id=task_id)
        errors = idle_app.memory.list_errors(task_id=task_id)
        tasks_all = idle_app.memory.list_tasks(limit=limit)
        tasks = [t for t in tasks_all if t.get("id") == task_id]
    else:
        actions = idle_app.memory.list_actions()
        queries = idle_app.memory.list_queries(limit=limit)
        urls = idle_app.memory.list_urls(limit=limit)
        findings = idle_app.memory.list_findings(limit=limit)
        errors = idle_app.memory.list_errors()
        tasks = idle_app.memory.list_tasks(limit=limit)
    return {"actions": actions, "queries": queries, "urls": urls, "findings": findings, "errors": errors, "tasks": tasks}


def _honest_status(data_dir: Path, idle_seconds: float | None = None, watch_running: bool | None = None) -> dict:
    """Thin delegate to Application API — keep name/signature for backward compat."""
    try:
        return _get_idle_cua(data_dir).get_honest_status(watch_running=watch_running, idle_seconds=idle_seconds)
    except Exception:
        # Fallback minimal (should not happen in tests)
        return {
            "text": "Limited mode — stub planner · LLM off",
            "sub": "Sessions run on stub planner · LLM disabled",
            "level": "limited",
            "dot": "bg-amber-400",
            "banner_text": "Limited mode — stub planner · LLM off",
            "chip_text": "Limited mode",
            "hero_title": "Limited mode — stub planner",
            "hero_sub": f"Idle {(idle_seconds or 0):.0f}s / 600s · LLM off · threshold 600s",
        }


def _mask_key(key: str) -> str:
    if not key:
        return "—"
    key = key.strip()
    if len(key) <= 4:
        return "••••"
    # Show last 4 chars, mask rest
    return "sk-..." + key[-4:] if key.startswith("sk-") else "••••" + key[-4:]


def fmt_dt(value: Any) -> str:
    """Single human date format for the Local UI: `02 Sep 2026 · 17:01`.

    Replaces the three ad-hoc formats (raw ISO with microseconds in Tasks /
    Reports, sliced ISO in History, date-only on the dashboard) with one.
    Unparseable input falls back to a truncated readable string, never empty.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return "—"
    s = str(value).strip()
    try:
        iso = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%d %b %Y · %H:%M")
    except Exception:
        return s[:16].replace("T", " ")


def fmt_dt_s(value: Any) -> str:
    """fmt_dt with seconds — for same-minute histories (`02 Sep · 16:57:40`)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return "—"
    s = str(value).strip()
    try:
        iso = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%d %b · %H:%M:%S")
    except Exception:
        return s[:19].replace("T", " ")


def fmt_dur(seconds: Any) -> str:
    """One duration vocabulary for the whole UI — minutes, not seconds."""
    try:
        total = int(float(seconds or 0))
    except Exception:
        return "—"
    if total < 60:
        return f"{total} s"
    mins = total // 60
    if mins < 60:
        return f"{mins} min"
    hours, mins = divmod(mins, 60)
    return f"{hours} h {mins} min" if mins else f"{hours} h"


def human_label(action: str, target: str | None = None) -> str:
    """Jinja filter — one human action dictionary for Inspector, History, Reports."""
    return humanize_action(action or "", target)


def _notice_level(message: str) -> str:
    """Classify a history notice so routine notes are not all red errors.

    info: lifecycle notes (agent tabs closed at task end).
    warning: skipped repeats and retried verifications.
    error: everything else (genuine failures).
    """
    m = (message or "").lower()
    if m.startswith("closed ") or "tab(s)" in m:
        return "info"
    if "skipped repeat" in m or "retry" in m or "verify failed" in m:
        return "warning"
    return "error"


# Snake_case action kinds that may leak into generated reports, mapped once to the
# same human labels the Inspector and History use (single UI dictionary).
_ACTION_LABEL_KEYS: dict[str, str] = {}
for _k in (
    "open_allowed_site", "open_link", "read_ui", "extract_public_info",
    "save_note", "create_note", "save_link", "close_own_tab", "close_own_app",
    "open_app", "send_message", "submit_form", "form_submit", "edit_document",
):
    _ACTION_LABEL_KEYS[_k] = humanize_action(_k)


def _render_markdown(md: str) -> str:
    """Minimal Markdown → HTML for session reports (headings, bold, code, lists).

    Deliberately small: no new dependency, no italic (single underscores appear
    in raw report spans and URLs). Input is HTML-escaped first.
    """
    import html as _html
    import re as _re

    def _inline(s: str) -> str:
        s = _html.escape(s)
        s = _re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = _re.sub(r"`([^`]+?)`", r"<code class='mono text-[13px] bg-zinc-100 px-1 rounded'>\1</code>", s)
        # Reports are generated with internal snake_case kinds (read_ui, save_note…).
        # Render the same human labels the rest of the UI uses (one dictionary).
        for _key in sorted(_ACTION_LABEL_KEYS, key=len, reverse=True):
            s = _re.sub(r"\b" + _key + r"\b", _ACTION_LABEL_KEYS[_key], s)
        return s

    out: list[str] = []
    in_list = False
    for ln in (md or "").splitlines() + [""]:
        st = ln.strip()
        if st.startswith("### "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h3 class='text-[15px] font-semibold mt-4'>{_inline(st[4:])}</h3>")
        elif st.startswith("## "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h2 class='text-base font-semibold mt-5'>{_inline(st[3:])}</h2>")
        elif st.startswith("# "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h1 class='text-lg font-semibold mt-5'>{_inline(st[2:])}</h1>")
        elif st.startswith("- "):
            if not in_list:
                out.append("<ul class='list-disc ml-5 mt-2 space-y-1 text-[15px] leading-relaxed'>")
                in_list = True
            _item = st[2:].strip()
            # Empty report sections say "(none)" — render as quiet None, not content.
            if _item == "(none)":
                out.append("<li class='text-zinc-400 italic'>None</li>")
            elif _item.startswith("(no ") and _item.endswith(")"):
                out.append(f"<li class='text-zinc-500'>{_inline(_item[1:-1])}</li>")
            elif _item.startswith("Actions planned:"):
                # "- Actions planned: open_allowed_site, search, …" — one label per kind.
                _kinds = [_k.strip().strip("`") for _k in _item[len("Actions planned:"):].split(",")]
                _labels = [humanize_action(_k) for _k in _kinds if _k]
                out.append(f"<li>Actions planned: {_inline(', '.join(_labels))}</li>")
            else:
                _m = _re.match(r"([a-z][a-z_]+)( — not executed.*)$", _item)
                if _m and ("_" in _m.group(1)):
                    out.append(f"<li>{_inline(humanize_action(_m.group(1)) + _m.group(2))}</li>")
                else:
                    out.append(f"<li>{_inline(_item)}</li>")
        elif not st:
            if in_list:
                out.append("</ul>")
                in_list = False
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            # A whole-line _..._ span (report meta line) renders as muted text.
            if len(st) > 2 and st.startswith("_") and st.endswith("_"):
                out.append(f"<p class='mt-2 text-sm text-zinc-500'>{_inline(st[1:-1])}</p>")
            else:
                out.append(f"<p class='mt-2 text-[15px] leading-relaxed'>{_inline(st)}</p>")
    return "".join(out)


def _sidebar_ctx(data_dir: Path, idle_app: IdleCua) -> dict:
    """Shared sidebar view-model so every page shows the same agent status."""
    lock_info = get_lock_info(data_dir)
    watch_loop = {
        "running": bool(_scheduler_state.get("running")),
        "pid": _scheduler_state.get("pid") or (lock_info.get("pid") if lock_info else None),
        "started_at": _scheduler_state.get("started_at"),
        "lock": lock_info,
    }
    try:
        demo_mode = bool(idle_app.is_demo_mode())
    except Exception:
        demo_mode = True
    try:
        threshold = int(idle_app.get_effective_idle_threshold())
    except Exception:
        threshold = 600
    return {
        "watch_loop": watch_loop,
        "lock_info": lock_info,
        "effective_idle_threshold": threshold,
        "demo_mode": demo_mode,
    }


def _get_masked_provider(store: ProviderStore, name: str) -> dict:
    cfg = store.get(name)
    # Try to get real key length but don't leak
    kc = get_default_store(store.data_dir)
    key = kc.get(name) or ""
    masked = _mask_key(key) if key else "not set"
    return {
        "name": cfg.name,
        "base_url": cfg.base_url,
        "model": cfg.model,
        "masked_key": masked,
        "selected": store.is_selected(name),
    }


def _enrich_plan_verdicts(idle_app: IdleCua, plan) -> list[dict]:
    """Humanize plan verdicts for Local UI rendering (shared by preview endpoints)."""
    verdicts = idle_app.get_plan_verdicts(plan)
    for v in verdicts:
        v["label"] = humanize_action(v["action"], v.get("domain") or plan.target)
        if v["verdict"] == "blocked" and "allowlist" in v.get("reason", "").lower():
            v["allowlist_link"] = "/settings#allowlist"
        if v["verdict"] == "needs-confirmation":
            v["unattended_action"] = "skipped in unattended runs"
    return verdicts


def _is_plan_skipped(idle_app: IdleCua, task_id: str) -> bool:
    """A plan-level anti-repeat skip looks completed but ran zero actions.

    Showing it as green `completed` misleads; the UI maps it to `skipped`.
    """
    try:
        errors = idle_app.memory.list_errors(task_id=task_id)
    except Exception:
        return False
    if not any("skipped repeat plan" in (e.get("message") or "") for e in errors):
        return False
    try:
        return len(idle_app.memory.list_actions(task_id=task_id)) == 0
    except Exception:
        return False


def _ui_state_for_task(idle_app: IdleCua, raw_state: str, task_id: str) -> str:
    """Map internal AgentState to the one UI vocabulary (queued/running/…/skipped)."""
    if raw_state == "waiting_for_idle":
        return "queued"
    if raw_state == "disabled":
        return "queued"
    if raw_state == "completed" and _is_plan_skipped(idle_app, task_id):
        return "skipped"
    return raw_state


def _enrich_tasks_with_results(idle_app: IdleCua, tasks: list[dict]) -> list[dict]:
    """Shared task enrichment for the Local UI (dashboard + /tasks).

    Adds `ui_state` (waiting_for_idle/disabled → queued) and `result_str`
    (findings/urls counts, reused per #32) plus the raw counts. Urls are
    fetched once and partitioned per task instead of scanned per task.
    """
    try:
        all_urls = idle_app.memory.list_urls(limit=1000)
    except Exception:
        all_urls = []
    urls_by_task: dict[str, int] = {}
    for u in all_urls:
        tid = u.get("task_id")
        if tid:
            urls_by_task[tid] = urls_by_task.get(tid, 0) + 1
    enriched = []
    for t in tasks:
        tid = t.get("id", "")
        try:
            findings = idle_app.memory.list_findings(task_id=tid)
        except Exception:
            findings = []
        n_findings = len(findings) if findings else 0
        n_urls = urls_by_task.get(tid, 0)
        state = t.get("state", "unknown")
        ui_state = _ui_state_for_task(idle_app, state, tid)
        if ui_state == "skipped":
            result_str = "skipped — duplicate"
        elif state in ("waiting_for_idle", "disabled", "queued"):
            result_str = "—"
        else:
            result_str = f"{n_findings} findings · {n_urls} urls"
        enriched.append({
            **t,
            "ui_state": ui_state,
            "findings_count": n_findings,
            "urls_count": n_urls,
            "result_str": result_str,
        })
    return enriched


def _watch_loop_worker(data_dir: Path, poll_interval: float = 5.0):
    """Background worker for ADR-0004/ADR-0006: serve owns the Watch loop.

    Thin idle-trigger adapter: polls the idle window, then submits one
    idle-triggered Start per window. The lifecycle owner selects and claims
    eligible work (oldest paused before oldest queued) under one gate path.
    No storage inspection, no gate evaluation, no state mutation here.
    """
    import time as _time

    from ..executor import is_emergency_stop_requested

    while not _watch_stop_event.is_set() and _scheduler_state.get("running"):
        try:
            if is_emergency_stop_requested():
                # Emergency stop disables the Watch loop; an explicit
                # scheduler start re-enables it (and clears the latch).
                _scheduler_state["running"] = False
                break
            idle_app = _get_idle_cua(data_dir)
            threshold = idle_app.get_effective_idle_threshold()
            _scheduler_state["idle_threshold"] = threshold
            got_idle = idle_app.get_scheduler().wait_for_idle(
                poll_interval=poll_interval, timeout=poll_interval, threshold_override=threshold
            )
            if not got_idle:
                continue
            if is_emergency_stop_requested():
                _scheduler_state["running"] = False
                break
            outcome = idle_app.start_task_lifecycle(task_id=None, trigger="idle", mode="unattended")
            if getattr(outcome, "category", None) == "already_active":
                _time.sleep(poll_interval)
                continue
        except Exception:
            pass
        # sleep before next poll if not stopped
        if _watch_stop_event.wait(poll_interval):
            break


def _start_watch_worker(data_dir: Path):
    global _watch_thread
    if _watch_thread and _watch_thread.is_alive():
        return
    _watch_stop_event.clear()
    _watch_thread = threading.Thread(target=_watch_loop_worker, args=(data_dir,), daemon=True)  # Python thread flag, not domain Watch loop term
    _watch_thread.start()


def _stop_watch_worker():
    _watch_stop_event.set()
    # thread will exit; we don't join long to avoid blocking API
    # but try quick join
    try:
        if _watch_thread and _watch_thread.is_alive():
            _watch_thread.join(timeout=0.5)
    except Exception:
        pass


def create_app(data_dir: Path | str | None = None, test_mode: bool = False) -> FastAPI:
    """Factory for the IdleCUA serve FastAPI app.

    Thin caller of IdleCua — no business logic here.
    When test_mode True, the lock and real driver are not auto-acquired; TestClient can inject fakes via dependency override.
    """
    resolved_data_dir = _resolve_data_dir(data_dir)
    resolved_data_dir.mkdir(parents=True, exist_ok=True)

    app = FastAPI(
        title="IdleCUA Local API",
        description="IdleCUA Local HTTP API — thin caller of the Application API. Serves Local UI at / .",
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url="/api/redoc",
    )

    # Templates and static
    templates_dir = Path(__file__).parent / "templates"
    static_dir = Path(__file__).parent / "static"
    # Ensure static exists
    static_dir.mkdir(parents=True, exist_ok=True)
    # Create htmx vendor if not exists (minimal stub that does polling via JS if not loaded)
    htmx_path = static_dir / "htmx.min.js"
    if not htmx_path.exists():
        # Vendored htmx — we fetch minimal via CDN fallback? Instead write a tiny no-op that will be replaced by CDN if network available.
        # For offline, include a minimal htmx-like stub for polling? We'll write a real minimal polling shim.
        htmx_path.write_text(
            "/* htmx stub for IdleCUA serve — polling shim; replace with vendored htmx.min.js for full htmx */\n"
            "console.log('htmx stub loaded — consider vendoring real htmx.min.js');\n",
            encoding="utf-8",
        )

    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    templates = Jinja2Templates(directory=str(templates_dir)) if templates_dir.exists() else None
    if templates is not None:
        templates.env.filters["fmt_dt"] = fmt_dt
        templates.env.filters["fmt_dt_s"] = fmt_dt_s
        templates.env.filters["fmt_dur"] = fmt_dur
        templates.env.filters["human_label"] = human_label

    # Helper to get app instance per request (thin)
    def get_app_instance() -> IdleCua:
        return _get_idle_cua(resolved_data_dir)

    # -- API routes under /api/v1 --

    # Status — thin delegate to Application API (T2)
    @app.get("/api/v1/status")
    def api_status():
        data_dir = resolved_data_dir
        idle_app = _get_idle_cua(data_dir)
        lock_info = get_lock_info(data_dir)
        watch_loop = {
            "running": bool(_scheduler_state.get("running")),
            "pid": _scheduler_state.get("pid") or (lock_info.get("pid") if lock_info else None),
            "started_at": _scheduler_state.get("started_at"),
            "lock": lock_info,
        }
        enriched = idle_app.get_status_enriched(watch_loop=watch_loop)
        # Map to versioned contract keys exactly (no breaking change)
        return {
            "agent_state": enriched.get("agent_state", "unknown"),
            "idle_seconds": enriched.get("idle_seconds", 0.0),
            "idle_threshold_seconds": enriched.get("idle_threshold_seconds", 600),
            "screen_locked": enriched.get("screen_locked", False),
            "watch_loop": enriched.get("watch_loop", watch_loop),
            "demo_mode": enriched.get("demo_mode", False),
            "honest_status": enriched.get("honest_status", {}),
            "limits": enriched.get("limits", {}),
            "daily_usage": enriched.get("daily_usage", {}),
            "today_usage": enriched.get("today_usage", {}),
            "last_report": enriched.get("last_report"),
            "active_task": enriched.get("active_task"),
            "last_action": enriched.get("last_action"),
            "current_site": enriched.get("current_site"),
            "stop_command": enriched.get("stop_command"),
        }

    @app.get("/api/v1/tasks")
    def api_list_tasks():
        idle_app = _get_idle_cua(resolved_data_dir)
        tasks = idle_app.memory.list_tasks(limit=100)
        # Enrich with result counts
        enriched = []
        for t in tasks:
            tid = t["id"]
            # counts
            try:
                findings = idle_app.memory.list_findings(task_id=tid)
                urls = idle_app.memory.list_urls(limit=1000)
                # filter by task_id if needed? urls table has task_id
                task_urls = [u for u in urls if u.get("task_id") == tid] if urls and isinstance(urls[0], dict) and "task_id" in urls[0] else []
                # fallback: use memory.list only with task filter? memory.list_urls doesn't filter by task, but we can list all and filter
                # For findings we already filtered
                # errors
                errors = idle_app.memory.list_errors(task_id=tid)
            except Exception:
                findings = []
                task_urls = []
                errors = []
            # Map internal states to the one UI vocabulary (queued / skipped / …).
            state = t.get("state", "unknown")
            ui_state = _ui_state_for_task(idle_app, state, tid)
            enriched.append({
                **t,
                "state": state,
                "ui_state": ui_state,
                "findings_count": len(findings) if findings else 0,
                "urls_count": len(task_urls) if task_urls else 0,
                "errors_count": len(errors) if errors else 0,
            })
        return {"tasks": enriched}

    @app.post("/api/v1/tasks")
    def api_create_task(payload: TaskCreate):
        if not payload.goal or not payload.goal.strip():
            raise HTTPException(status_code=400, detail="goal must be non-empty")
        idle_app = _get_idle_cua(resolved_data_dir)
        # Queue-time decisions: "skip" maps to lifecycle skip types;
        # "approve" is rejected in v1 with a safe client error.
        skip_types: list[str] = []
        approvals: list[str] = []
        if payload.decisions:
            for kind, decision in payload.decisions.items():
                if decision == "skip":
                    skip_types.append(kind)
                elif decision == "approve":
                    approvals.append(kind)
        try:
            task = idle_app.create_task(payload.goal.strip(), skip_action_types=skip_types, approvals=approvals)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # Return with verdicts preview for immediate UI
        try:
            plan = idle_app.dry_run(task.description)
            verdicts = idle_app.get_plan_verdicts(plan)
            # Humanize
            for v in verdicts:
                v["label"] = humanize_action(v["action"], v.get("domain"))
                # For blocked, add allowlist link hint
                if v["verdict"] == "blocked" and "allowlist" in v.get("reason", "").lower():
                    v["allowlist_link"] = "/settings#allowlist"
        except Exception as e:
            verdicts = []
            plan = None
        return {
            "task": {"id": task.id, "description": task.description, "state": "queued", "ui_state": "queued"},
            "plan_preview": idle_app.plan_to_dict(plan) if plan else None,
            "verdicts": verdicts,
        }

    @app.get("/api/v1/tasks/{task_id}")
    def api_get_task(task_id: str):
        idle_app = _get_idle_cua(resolved_data_dir)
        data = idle_app.memory.get_task(task_id)
        if not data:
            raise HTTPException(status_code=404, detail="task not found")
        # Enrich with execution result
        # Get plan_json, findings etc.
        plan = None
        try:
            if data.get("plan_json"):
                plan = json.loads(data["plan_json"])
        except Exception:
            plan = None
        findings = idle_app.memory.list_findings(task_id=task_id)
        urls = []
        # urls filter
        try:
            all_urls = idle_app.memory.list_urls(limit=1000)
            urls = [u for u in all_urls if u.get("task_id") == task_id]
        except Exception:
            urls = []
        actions = idle_app.memory.list_actions(task_id=task_id)
        errors = idle_app.memory.list_errors(task_id=task_id)
        report = idle_app.memory.get_report(task_id)
        # Also get verdicts if plan available
        verdicts = []
        try:
            if plan:
                # reconstruct Plan-like dict for verdicts: need expected_actions
                expected = plan.get("expected_actions", [])
                for kind in expected:
                    from ..policy import TypedAction
                    ta = TypedAction(kind=kind, target_url=f"https://{plan.get('target','google.com')}") if kind not in ("save_note", "close_own_tab") else TypedAction(kind=kind)
                    res = idle_app.check_action(ta)
                    verdicts.append({
                        "action": kind,
                        "label": humanize_action(kind, plan.get("target")),
                        "verdict": res.verdict.value,
                        "reason": res.reason,
                        "domain": res.domain,
                    })
        except Exception:
            pass
        # Map state
        state = data.get("state", "unknown")
        ui_state = state
        if state == "waiting_for_idle":
            ui_state = "queued"
        return {
            "task": {**data, "ui_state": ui_state},
            "plan": plan,
            "findings": findings,
            "urls": urls,
            "actions": actions,
            "errors": errors,
            "report": report,
            "verdicts": verdicts,
            "result_counts": {"findings": len(findings), "urls": len(urls)},
        }

    @app.post("/api/v1/plans/preview")
    def api_preview_plan(payload: PlanPreviewRequest):
        """Dry-run plan preview without persisting a task (no side effects)."""
        goal = (payload.goal or "").strip()
        if not goal:
            raise HTTPException(status_code=400, detail="goal must be non-empty")
        idle_app = _get_idle_cua(resolved_data_dir)
        try:
            plan = idle_app.dry_run(goal)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
        verdicts = _enrich_plan_verdicts(idle_app, plan)
        return {
            "goal": goal,
            "plan": idle_app.plan_to_dict(plan),
            "verdicts": verdicts,
            "human_labels": [humanize_action(a, plan.target) for a in plan.expected_actions],
            "budget": {
                "max_duration_minutes": plan.max_duration_minutes,
                "max_actions": plan.max_actions,
                "risk_level": plan.risk_level.value,
                "requires_confirmation": plan.requires_confirmation,
            },
            "this_plan_budget": {
                "max_duration_minutes": plan.max_duration_minutes,
                "max_actions": plan.max_actions,
            },
        }

    @app.post("/api/v1/tasks/{task_id}/plan")
    def api_plan_preview(task_id: str):
        idle_app = _get_idle_cua(resolved_data_dir)
        data = idle_app.memory.get_task(task_id)
        if not data:
            raise HTTPException(status_code=404, detail="task not found")
        goal = data.get("description", "")
        # Dry-run — zero computer actions
        try:
            plan = idle_app.dry_run(goal)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
        verdicts = _enrich_plan_verdicts(idle_app, plan)
        # Ensure no driver calls happened — dry_run already guarantees
        return {
            "task_id": task_id,
            "goal": goal,
            "plan": idle_app.plan_to_dict(plan),
            "verdicts": verdicts,
            "human_labels": [humanize_action(a, plan.target) for a in plan.expected_actions],
            "budget": {
                "max_duration_minutes": plan.max_duration_minutes,
                "max_actions": plan.max_actions,
                "risk_level": plan.risk_level.value,
                "requires_confirmation": plan.requires_confirmation,
            },
            "this_plan_budget": {
                "max_duration_minutes": plan.max_duration_minutes,
                "max_actions": plan.max_actions,
            },
        }

    @app.post("/api/v1/tasks/{task_id}/run")
    def api_run_task(task_id: str):
        data_dir = resolved_data_dir
        idle_app = _get_idle_cua(data_dir)
        # One lifecycle owner: no session guard, gate evaluation, or state
        # reconstruction here — the seam returns structured outcomes.
        outcome = idle_app.start_task_lifecycle(task_id, trigger="explicit", mode="unattended")
        cat = getattr(outcome, "category", None)
        cat_v = getattr(cat, "value", cat)
        if cat_v == "not_found":
            raise HTTPException(status_code=404, detail="task not found")
        if cat_v == "already_active":
            raise HTTPException(status_code=409, detail="session already in flight")
        if cat_v == "invalid_transition":
            raise HTTPException(status_code=409, detail=str(getattr(outcome, "message", "invalid transition")))
        if cat_v == "invalid_request":
            raise HTTPException(status_code=400, detail=str(getattr(outcome, "message", "invalid request")))
        if cat_v == "not_ready":
            fg = getattr(outcome, "failed_gate", None)
            if fg == "profile":
                raw = str(getattr(outcome, "message", ""))
                raise HTTPException(status_code=423, detail=f"Refused: {raw}")
            elif fg == "idle":
                raw = str(getattr(outcome, "message", ""))
                raise HTTPException(status_code=423, detail=f"idle gate blocked: {raw}")
            elif fg == "screen":
                # Legacy HTTP contract returned exactly "screen locked" (no suffix); keep byte-identical.
                raise HTTPException(status_code=423, detail="screen locked")
            elif fg in ("schedule", "limits"):
                raw = str(getattr(outcome, "message", ""))
                raise HTTPException(status_code=423, detail=f"Refused: {raw}")
            else:
                raw = str(getattr(outcome, "message", ""))
                raise HTTPException(status_code=423, detail=f"Refused: {raw}")
        if cat_v == "execution_failed" and getattr(outcome, "state", None) not in ("failed", "paused_by_user", "stopped", "completed"):
            raise HTTPException(status_code=500, detail=str(getattr(outcome, "message", "execution failed")))
        # Safe structured result view (no raw provider/driver/SQLite errors).
        return idle_app.get_task_result(task_id)

    @app.delete("/api/v1/tasks/{task_id}")
    def api_cancel_task(task_id: str):
        """Owner cancels a queued or paused task — it will never start (state → stopped)."""
        idle_app = _get_idle_cua(resolved_data_dir)
        outcome = idle_app.cancel_task_sync(task_id)
        cat = getattr(getattr(outcome, "category", None), "value", getattr(outcome, "category", None))
        if cat == "not_found":
            raise HTTPException(status_code=404, detail="task not found")
        if cat == "invalid_transition":
            raise HTTPException(status_code=409, detail="only queued or paused tasks can be cancelled")
        if cat not in ("stopped", "ok"):
            raise HTTPException(status_code=500, detail=str(getattr(outcome, "message", "cancel failed")))
        return {"ok": True, "task_id": task_id, "state": "stopped"}

    @app.get("/api/v1/history")
    def api_history(task_id: str | None = None, limit: int = 200):
        idle_app = _get_idle_cua(resolved_data_dir)
        return _fetch_history_filtered(idle_app, task_id, limit)

    @app.get("/api/v1/reports")
    def api_list_reports():
        idle_app = _get_idle_cua(resolved_data_dir)
        reports = idle_app.list_reports(limit=50)
        # Return markdown bodies? For list we return metadata
        return {"reports": reports}

    @app.get("/api/v1/reports/{task_id}")
    def api_get_report(task_id: str):
        idle_app = _get_idle_cua(resolved_data_dir)
        rep = idle_app.get_report(task_id)
        if not rep:
            # Also try file
            rpath = resolved_data_dir / "reports" / f"{task_id}.md"
            if rpath.exists():
                try:
                    md = rpath.read_text(encoding="utf-8")
                    return {"task_id": task_id, "markdown": md}
                except Exception:
                    pass
            raise HTTPException(status_code=404, detail="report not found")
        # Ensure markdown is returned
        return {"task_id": task_id, "markdown": rep.get("markdown", ""), "created_at": rep.get("created_at")}

    @app.get("/api/v1/diagnostics")
    def api_diagnostics():
        """Thin caller per ADR-0002: decision lives in IdleCua.get_diagnostics()."""
        data_dir = resolved_data_dir
        idle_app = _get_idle_cua(data_dir)
        # Watch loop is observed live state (never owned by app); pass current server state.
        watch = dict(_scheduler_state)
        try:
            watch["idle_threshold"] = int(idle_app.get_effective_idle_threshold())
        except Exception:
            pass
        bundle = idle_app.get_diagnostics(watch_loop=watch)
        # HTTP contract unchanged (shape-preserving): expose only versioned keys, masked.
        # The bundle's driver dict carries extra structured fields for CLI
        # renderers (version/accessibility/screen_recording/probe_error) — the
        # versioned route exposes only {ok, message}.
        secrets = bundle.get("secrets_scan", {})
        _drv = bundle.get("driver", {"ok": False, "message": ""}) if isinstance(bundle.get("driver"), dict) else {}
        return {
            "permissions": bundle.get("permissions", []),
            "driver": {"ok": bool(_drv.get("ok", False)), "message": str(_drv.get("message", ""))},
            "profile": bundle.get("profile", {"valid": False, "confirmed": False, "errors": []}),
            "secrets_scan": {"ok": bool(secrets.get("ok", True)), "findings": list(secrets.get("findings") or [])},
            "scheduler_lock": bundle.get("scheduler_lock", {"locked": False, "info": None}),
            "watch_loop": bundle.get("watch_loop", watch),
        }

    @app.get("/api/v1/settings")
    def api_get_settings():
        """Thin caller per ADR-0002: decision lives in IdleCua.get_owner_settings()."""
        idle_app = _get_idle_cua(resolved_data_dir)
        return idle_app.get_owner_settings()

    @app.patch("/api/v1/settings")
    def api_patch_settings(payload: SettingsPatch):
        """Thin caller per ADR-0002: validation+write lives in IdleCua.update_owner_settings()."""
        idle_app = _get_idle_cua(resolved_data_dir)
        try:
            try:
                patch = payload.model_dump(exclude_none=True)
            except AttributeError:
                patch = payload.dict(exclude_none=True)
            result = idle_app.update_owner_settings(patch)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # Keep HTTP contract byte-identical: ok + profile + config (no effective leak in PATCH shape).
        return {"ok": True, "profile": result["profile"], "config": result["config"]}

    # Providers
    @app.get("/api/v1/providers")
    def api_list_providers():
        data_dir = resolved_data_dir
        store = ProviderStore.load(data_dir)
        lst = []
        for name in store.providers:
            lst.append(_get_masked_provider(store, name))
        return {"selected": store.selected, "providers": lst}

    @app.post("/api/v1/providers")
    def api_create_provider(payload: ProviderCreate):
        data_dir = resolved_data_dir
        # Validate
        try:
            cfg = ProviderConfig(name=payload.name, base_url=payload.base_url, model=payload.model)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        store = ProviderStore.load(data_dir)
        store.providers[cfg.name] = cfg
        if store.selected is None:
            store.selected = cfg.name
        store.save()
        # Store secret in keychain (never in file)
        kc = get_default_store(data_dir)
        try:
            kc.set(cfg.name, payload.api_key)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"failed to store key: {e}")
        return {"ok": True, "provider": _get_masked_provider(store, cfg.name)}

    @app.delete("/api/v1/providers/{name}")
    def api_delete_provider(name: str):
        data_dir = resolved_data_dir
        store = ProviderStore.load(data_dir)
        if name not in store.providers:
            raise HTTPException(status_code=404, detail="provider not found")
        try:
            store.remove(name)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        kc = get_default_store(data_dir)
        try:
            kc.delete(name)
        except Exception:
            pass
        return {"ok": True}

    @app.post("/api/v1/providers/{name}/test")
    def api_test_provider(name: str):
        data_dir = resolved_data_dir
        store = ProviderStore.load(data_dir)
        if name not in store.providers:
            raise HTTPException(status_code=404, detail="provider not found")
        cfg = store.get(name)
        kc = get_default_store(data_dir)
        api_key = kc.get(name)
        if not api_key:
            env_map = {
                "openrouter": "OPENROUTER_API_KEY",
                "opencode-go": "OPENCODE_GO_API_KEY",
            }
            env_var = env_map.get(name) or f"{name.upper().replace('-', '_')}_API_KEY"
            api_key = os.environ.get(env_var) or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            return JSONResponse(status_code=400, content={"ok": False, "error": f"No API key for provider '{name}'"})

        try:
            from ..providers.openai_adapter import OpenAICompatibleProvider

            provider = OpenAICompatibleProvider(config=cfg, api_key=api_key, data_dir=data_dir)
            status, msg = provider.test_vision()
            ok = status.value == "ok"
            return {"ok": ok, "status": status.value, "message": msg}
        except Exception as e:
            return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

    @app.post("/api/v1/providers/{name}/select")
    def api_select_provider(name: str):
        data_dir = resolved_data_dir
        store = ProviderStore.load(data_dir)
        if name not in store.providers:
            raise HTTPException(status_code=404, detail="provider not found")
        store.select(name)
        return {"ok": True, "selected": name}

    @app.post("/api/v1/profile/confirm")
    def api_confirm_profile():
        """One-click confirm of default (conservative) Profile with prefill from Config."""
        data_dir = resolved_data_dir
        # If already confirmed, return it
        existing = load_profile(data_dir / "profile.json")
        if existing is not None and existing.confirmed:
            # Check validity — if valid, return
            errs = validate_profile(existing)
            if not errs:
                return {"ok": True, "profile": existing.model_dump(), "already_confirmed": True}
        # Build default profile via interview defaults (conservative)
        from ..profile.interview import run_interview, save_confirmed_profile
        from rich.console import Console
        import io

        def _input(q):
            return ""

        def _confirm(_):
            return True

        _con = Console(file=io.StringIO(), width=80)
        profile, _, _ = run_interview(console=_con, input_func=_input, confirm_func=_confirm)
        # Prefill: inherit current Config values into unset Profile fields for one-time migration, shown for review
        # ADR-0003: one-time prefill that inherits current Config values into unset Profile fields, shown for review, so migration never silently overwrites confirmed Profile.
        # Our profile is new, so we inherit from config as defaults where empty?
        config = IdleCuaConfig.load(data_dir)
        # Prefill allowlist etc. if profile's allowlist is default but config's differs? For new profile, interview already gave defaults; we keep them.
        # But also ensure idle threshold reflects config default
        try:
            # If config has custom idle_threshold_seconds different from default 600, prefill into profile
            if config.idle_threshold_seconds != 600:
                profile.computer_usage.idle_threshold_minutes = max(1, config.idle_threshold_seconds // 60)
        except Exception:
            pass
        # Save
        ppath = data_dir / "profile.json"
        save_profile(profile, ppath)
        # Mirror to config for browser consent etc.
        try:
            from ..browser_consent import has_consent

            # Already mirrored via interview? Ensure
            pass
        except Exception:
            pass
        return {"ok": True, "profile": profile.model_dump(), "already_confirmed": False}

    @app.post("/api/v1/profile/revoke-browser")
    def api_revoke_browser():
        data_dir = resolved_data_dir
        profile = load_profile(data_dir / "profile.json")
        if profile is None:
            raise HTTPException(status_code=404, detail="no profile")
        from ..profile.models import BrowserConsent as _BC

        bc = _BC(main_profile_granted=False)
        profile.autonomy_boundaries.browser_consent = bc
        profile.browser_consent = bc
        profile.touch()
        save_profile(profile, data_dir / "profile.json")
        try:
            cfg = IdleCuaConfig.load(data_dir)
            cfg.record_browser_consent(False, browser="chrome", granted_at=None)
        except Exception:
            pass
        return {"ok": True}

    @app.post("/api/v1/scheduler/start")
    def api_scheduler_start():
        data_dir = resolved_data_dir
        # An explicit scheduler start clears a latched Emergency stop so
        # autonomous work can resume on renewed owner intent.
        try:
            _get_idle_cua(data_dir).clear_emergency_stop()
        except Exception:
            pass
        # Try to acquire lock. Serve already holds the process-level lock
        # (CLI acquires at startup, then runs uvicorn in-process), so the
        # self-owned conflict below is the normal path for UI starts
        # (ADR-0004: serve owns the Watch loop). No blind early-return on
        # cached running-state: always reconcile with the lock file so a
        # dead worker is healed and a foreign owner still 409s.
        try:
            info = acquire_lock(data_dir)
            _scheduler_state["running"] = True
            _scheduler_state["pid"] = info["pid"]
            _scheduler_state["started_at"] = info["started_at"]
            # Start background worker that owns watch loop (ADR-0004)
            if not test_mode:
                _start_watch_worker(data_dir)
            return {"ok": True, "state": _scheduler_state, "lock": info}
        except RuntimeError as e:
            if "already running" in str(e):
                # Serve holds the process-level lock itself (CLI acquires at
                # startup, then runs uvicorn in-process): starting the Watch
                # loop from the UI is self-owned, not a conflict (ADR-0004).
                existing = get_lock_info(data_dir)
                try:
                    owner_pid = int(existing.get("pid")) if existing else None
                except Exception:
                    owner_pid = None
                if owner_pid is not None and owner_pid == os.getpid():
                    _scheduler_state["running"] = True
                    _scheduler_state["pid"] = owner_pid
                    _scheduler_state["started_at"] = existing.get("started_at")
                    if not test_mode:
                        _start_watch_worker(data_dir)
                    return {"ok": True, "state": _scheduler_state, "lock": existing}
                raise HTTPException(status_code=409, detail=str(e))
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/v1/scheduler/stop")
    def api_scheduler_stop():
        # Stop halts the Watch loop and clears running-state only.
        # It must NOT release the process-level serve lock: the lock guards
        # "exactly one serve per data dir" for the lifetime of the process
        # (acquired in cli.py, released on serve exit). Releasing here would
        # let a second serve start on the same data dir while this one runs.
        _scheduler_state["running"] = False
        _scheduler_state["pid"] = None
        _scheduler_state["started_at"] = None
        _stop_watch_worker()
        return {"ok": True, "state": _scheduler_state}

    @app.post("/api/v1/emergency-stop")
    def api_emergency_stop(request: Request):
        data_dir = resolved_data_dir
        # Accept optional reason
        reason = "emergency stop"
        try:
            body = {}
            # try json
            import asyncio

            # sync read? Use request.json if available
            # For simplicity, check query
            reason_q = request.query_params.get("reason")
            if reason_q:
                reason = reason_q
        except Exception:
            pass
        idle_app = _get_idle_cua(data_dir)
        # Routed through the lifecycle owner: idempotent, releases input,
        # stops Agent-started processes, records a distinct stop cause.
        try:
            idle_app.request_emergency_stop(reason)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
        # Stop scheduler as well?
        _scheduler_state["running"] = False
        return {"ok": True, "reason": reason}

    # -- UI routes --

    @app.get("/", response_class=HTMLResponse)
    def ui_dashboard(request: Request):
        if templates is None:
            return HTMLResponse("<html><body>IdleCUA — templates missing</body></html>")
        data_dir = resolved_data_dir
        idle_app = _get_idle_cua(data_dir)
        config = idle_app.config
        # Status — thin delegate to Application API (T2)
        lock_info_dash = get_lock_info(data_dir)
        watch_loop_dash = {
            "running": bool(_scheduler_state.get("running")),
            "pid": _scheduler_state.get("pid") or (lock_info_dash.get("pid") if lock_info_dash else None),
            "started_at": _scheduler_state.get("started_at"),
            "lock": lock_info_dash,
        }
        enriched_dash = idle_app.get_status_enriched(watch_loop=watch_loop_dash)
        honest = enriched_dash.get("honest_status", {})
        # Getting started checklist
        profile = load_profile(data_dir / "profile.json")
        store = ProviderStore.load(data_dir)
        # Provider key check — single source via Application API demo badge
        has_key = not bool(enriched_dash.get("demo_mode", True))
        # perms
        try:
            from ..profile.permissions import check_permissions

            perms = check_permissions()
            perms_ok = all(p.granted is True for p in perms)
        except Exception:
            perms_ok = False
            perms = []
        browser_ok = False
        try:
            if profile and profile.autonomy_boundaries.browser_consent.main_profile_granted:
                browser_ok = True
        except Exception:
            pass
        checklist = [
            {"id": "profile", "label": "Profile confirmed", "desc": "Safe defaults — read-only, 45 min sessions", "done": bool(profile and profile.confirmed), "cta": "Confirmed ✓" if profile and profile.confirmed else "Confirm defaults"},
            {"id": "provider", "label": "Provider key", "desc": "No AI key — demo planner only" if not has_key else "AI key configured — full runs enabled", "done": has_key, "cta": "Add key" if not has_key else "Configured"},
            {"id": "permissions", "label": "macOS permissions", "desc": "Accessibility & Screen Recording granted" if perms_ok else "Grant Accessibility & Screen Recording", "done": perms_ok, "cta": "Granted" if perms_ok else "Check"},
            {"id": "browser", "label": "Browser consent", "desc": "Agent may use its own Chrome tabs" if browser_ok else "Let the agent use its own Chrome tabs", "done": browser_ok, "cta": "Granted" if browser_ok else "Grant"},
        ]
        done_count = sum(1 for c in checklist if c["done"])
        # Today's usage meters
        from ..accounting import get_today_count

        llm_today = get_today_count(data_dir)
        # Tasks (shared enrichment: ui_state + result summary)
        tasks = idle_app.memory.list_tasks(limit=20)
        tasks_enriched = _enrich_tasks_with_results(idle_app, tasks)
        # Failure reason for Retry (failed tasks only)
        for t in tasks_enriched:
            failure_reason = ""
            if t.get("state") == "failed":
                try:
                    errors = idle_app.memory.list_errors(task_id=t.get("id", ""))
                    if errors:
                        failure_reason = errors[0].get("message", "")[:80]
                except Exception:
                    pass
            t["failure_reason"] = failure_reason

        # Inspector for first task? Use most recent
        inspector_task = tasks_enriched[0] if tasks_enriched else None
        inspector = None
        if inspector_task:
            # try to get plan verdicts
            try:
                goal = inspector_task.get("description","")
                plan = idle_app.dry_run(goal)
                verdicts = idle_app.get_plan_verdicts(plan)
                for v in verdicts:
                    v["label"] = humanize_action(v["action"], v.get("domain") or plan.target)
                    if v["verdict"] == "blocked":
                        v["allowlist_link"] = "/settings#allowlist"
                inspector = {
                    "task": inspector_task,
                    "plan": idle_app.plan_to_dict(plan),
                    "verdicts": verdicts,
                    "budget": {"duration": plan.max_duration_minutes, "actions": plan.max_actions},
                }
            except Exception:
                inspector = {"task": inspector_task}

        effective_idle = int(enriched_dash.get("idle_threshold_seconds", 600))
        # Today meters from enriched limits (single source; mirrors api_status)
        _limits_dash = enriched_dash.get("limits", {})
        _daily = enriched_dash.get("daily_usage", {})
        try:
            _idle_seconds = float(enriched_dash.get("idle_seconds", 0.0) or 0.0)
        except Exception:
            _idle_seconds = 0.0
        # Deep-link from /tasks rows: ?inspect=<id> pre-selects the task.
        inspect_id = (request.query_params.get("inspect") or "").strip() or None
        if inspect_id and not any(t.get("id") == inspect_id for t in tasks_enriched):
            inspect_id = None
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "active": "dashboard",
                "config": config,
                "effective_idle_threshold": effective_idle,
                "idle_threshold": effective_idle,
                "idle_seconds": _idle_seconds,
                "honest": honest,
                "demo_mode": bool(enriched_dash.get("demo_mode", False)),
                "checklist": checklist,
                "done_count": done_count,
                "total": len(checklist),
                "today": {
                    "actions": _daily.get("actions", {}).get("used", 0),
                    "actions_limit": _daily.get("actions", {}).get("limit", config.max_actions),
                    "llm_calls": _daily.get("llm_calls", {}).get("used", _limits_dash.get("llm_calls_today", 0)),
                    "llm_limit": _daily.get("llm_calls", {}).get("limit", config.max_llm_calls_per_day),
                    "duration": _daily.get("duration", {}).get("used", 0),
                    "duration_limit": _daily.get("duration", {}).get("limit", config.max_duration_minutes),
                },
                "watch_loop": enriched_dash.get("watch_loop", watch_loop_dash),
                "tasks": tasks_enriched,
                "inspector": inspector,
                "inspect_id": inspect_id,
                "lock_info": lock_info_dash,
            },
        )

    @app.get("/ui/status", response_class=HTMLResponse)
    def ui_status_fragment(request: Request):
        # htmx polling fragment — thin delegate to Application API status (T2)
        data_dir = resolved_data_dir
        idle_app = _get_idle_cua(data_dir)
        lock_info_frag = get_lock_info(data_dir)
        watch_loop_frag = {
            "running": bool(_scheduler_state.get("running")),
            "pid": _scheduler_state.get("pid") or (lock_info_frag.get("pid") if lock_info_frag else None),
            "started_at": _scheduler_state.get("started_at"),
            "lock": lock_info_frag,
        }
        enriched_frag = idle_app.get_status_enriched(watch_loop=watch_loop_frag)
        honest = enriched_frag.get("honest_status", {})
        demo_mode = bool(enriched_frag.get("demo_mode", False))
        if templates is None:
            return HTMLResponse(f"<div>{honest.get('text','')}</div>")
        try:
            idle_seconds = float(enriched_frag.get("idle_seconds", 0.0) or 0.0)
        except Exception:
            idle_seconds = 0.0
        try:
            idle_threshold = int(enriched_frag.get("idle_threshold_seconds", 600))
        except Exception:
            idle_threshold = 600
        return templates.TemplateResponse(
            request,
            "partials/status_banner.html",
            {
                "honest": honest,
                "demo_mode": demo_mode,
                "idle_seconds": idle_seconds,
                "idle_threshold": idle_threshold,
            },
        )

    @app.get("/tasks", response_class=HTMLResponse)
    def ui_tasks(request: Request):
        if templates is None:
            return HTMLResponse("<html><body>Tasks</body></html>")
        data_dir = resolved_data_dir
        idle_app = _get_idle_cua(data_dir)
        raw_tasks = idle_app.memory.list_tasks(limit=100)
        # Reuse the dashboard enrichment so Result no longer duplicates State.
        tasks_enriched = _enrich_tasks_with_results(idle_app, raw_tasks)
        # Counts for the segmented state filter (over the unfiltered list).
        state_counts: dict[str, int] = {}
        for t in tasks_enriched:
            st = t.get("ui_state", "unknown")
            state_counts[st] = state_counts.get(st, 0) + 1
        total_count = len(tasks_enriched)
        state_filter = (request.query_params.get("state") or "").strip()
        if state_filter and state_filter != "all":
            tasks_enriched = [t for t in tasks_enriched if t.get("ui_state") == state_filter]
        return templates.TemplateResponse(
            request,
            "tasks.html",
            {
                "active": "tasks",
                "tasks": tasks_enriched,
                "state_filter": state_filter or "all",
                "state_counts": state_counts,
                "total_count": total_count,
                **_sidebar_ctx(data_dir, idle_app),
            },
        )

    @app.get("/history", response_class=HTMLResponse)
    def ui_history(request: Request):
        if templates is None:
            return HTMLResponse("<html><body>History</body></html>")
        idle_app = _get_idle_cua(resolved_data_dir)
        task_filter = request.query_params.get("task_id") or request.query_params.get("task")
        # Reuse single helper for API/UI filtering (DRY)
        hist = _fetch_history_filtered(idle_app, task_filter, limit=200)
        # Classify notices so routine notes are not rendered as red errors.
        for e in hist.get("errors", []):
            e["level"] = _notice_level(e.get("message", ""))
        tasks = idle_app.memory.list_tasks(limit=100)
        return templates.TemplateResponse(
            request,
            "history.html",
            {
                "active": "history",
                "history": hist,
                "tasks": tasks,
                "selected_task": task_filter,
                **_sidebar_ctx(resolved_data_dir, idle_app),
            },
        )

    @app.get("/reports", response_class=HTMLResponse)
    def ui_reports(request: Request):
        if templates is None:
            return HTMLResponse("<html><body>Reports</body></html>")
        idle_app = _get_idle_cua(resolved_data_dir)
        reports = idle_app.list_reports(limit=20)
        # Show the task goal instead of a bare hex id so reports are findable,
        # plus a one-line summary (state · findings · pages) for the list.
        try:
            all_urls = idle_app.memory.list_urls(limit=1000)
        except Exception:
            all_urls = []
        urls_by_task: dict[str, int] = {}
        for u in all_urls:
            tid = u.get("task_id")
            if tid:
                urls_by_task[tid] = urls_by_task.get(tid, 0) + 1
        for r in reports:
            tid = r.get("task_id", "")
            try:
                task = idle_app.memory.get_task(tid)
                r["title"] = (task.get("description") or "Untitled task") if task else "Untitled task"
                raw_state = (task.get("state") or "unknown") if task else "unknown"
            except Exception:
                r["title"] = "Untitled task"
                raw_state = "unknown"
            r["ui_state"] = _ui_state_for_task(idle_app, raw_state, tid)
            try:
                n_findings = len(idle_app.memory.list_findings(task_id=tid))
            except Exception:
                n_findings = 0
            r["summary"] = f"{n_findings} findings · {urls_by_task.get(tid, 0)} pages"
        return templates.TemplateResponse(
            request,
            "reports.html",
            {"active": "reports", "reports": reports, **_sidebar_ctx(resolved_data_dir, idle_app)},
        )

    @app.get("/reports/{task_id}", response_class=HTMLResponse)
    def ui_report_detail(task_id: str, request: Request):
        if templates is None:
            return HTMLResponse("<html><body>Report</body></html>")
        idle_app = _get_idle_cua(resolved_data_dir)
        rep = idle_app.get_report(task_id)
        if not rep:
            rpath = resolved_data_dir / "reports" / f"{task_id}.md"
            if rpath.exists():
                md = rpath.read_text(encoding="utf-8")
                rep = {"markdown": md}
            else:
                raise HTTPException(status_code=404, detail="report not found")
        md_text = rep.get("markdown", "")
        try:
            task = idle_app.memory.get_task(task_id)
            title = (task.get("description") or "Untitled task") if task else "Untitled task"
            raw_state = (task.get("state") or "unknown") if task else "unknown"
        except Exception:
            title = "Untitled task"
            raw_state = "unknown"
        ui_state = _ui_state_for_task(idle_app, raw_state, task_id)
        # The stored report repeats its own H1 and a raw-ISO Generated line —
        # the page header already shows the goal and a human date.
        import re as _re

        lines = md_text.splitlines()
        while lines and not lines[0].strip():
            lines.pop(0)
        if lines and lines[0].lstrip().startswith("# IdleCUA Session Report"):
            lines.pop(0)
        md_text = "\n".join(lines)
        md_text = _re.sub(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?",
            lambda m: fmt_dt(m.group(0)),
            md_text,
            count=1,
        )
        md_html = _render_markdown(md_text)
        return templates.TemplateResponse(
            request,
            "report_detail.html",
            {
                "active": "reports",
                "task_id": task_id,
                "title": title,
                "ui_state": ui_state,
                "created_at": rep.get("created_at", ""),
                "markdown": md_text,
                "markdown_html": md_html,
                **_sidebar_ctx(resolved_data_dir, idle_app),
            },
        )

    @app.get("/settings", response_class=HTMLResponse)
    def ui_settings(request: Request):
        if templates is None:
            return HTMLResponse("<html><body>Settings</body></html>")
        data_dir = resolved_data_dir
        config = IdleCuaConfig.load(data_dir)
        profile = load_profile(data_dir / "profile.json")
        idle_app = _get_idle_cua(data_dir)
        return templates.TemplateResponse(
            request,
            "settings.html",
            {"active": "settings", "config": config, "profile": profile, **_sidebar_ctx(data_dir, idle_app)},
        )

    @app.get("/diagnostics", response_class=HTMLResponse)
    def ui_diagnostics(request: Request):
        """Thin caller per ADR-0002: decision lives in IdleCua.get_diagnostics()."""
        if templates is None:
            return HTMLResponse("<html><body>Diagnostics</body></html>")
        data_dir = resolved_data_dir
        idle_app = _get_idle_cua(data_dir)
        watch = dict(_scheduler_state)
        try:
            watch["idle_threshold"] = int(idle_app.get_effective_idle_threshold())
        except Exception:
            pass
        bundle = idle_app.get_diagnostics(watch_loop=watch)
        perms_data = bundle.get("permissions", [])
        prof = bundle.get("profile", {})
        profile_valid = bool(prof.get("valid"))
        profile_errors = list(prof.get("errors") or [])
        lock = bundle.get("scheduler_lock", {}).get("info")
        watch_loop = bundle.get("watch_loop", watch)
        drv = bundle.get("driver", {}) if isinstance(bundle.get("driver"), dict) else {}
        secrets = bundle.get("secrets_scan", {}) if isinstance(bundle.get("secrets_scan"), dict) else {}
        # One-line summary for the page header (ok / warnings / errors).
        ok_count = sum(1 for p in perms_data if p.get("granted") is True)
        warn_count = sum(1 for p in perms_data if p.get("granted") is not True)
        if drv.get("ok"):
            ok_count += 1
        else:
            warn_count += 1
        if profile_valid:
            ok_count += 1
        else:
            warn_count += 1
        if secrets.get("ok", True):
            ok_count += 1
        else:
            warn_count += 1
        from datetime import timezone as _tz

        return templates.TemplateResponse(
            request,
            "diagnostics.html",
            {
                "active": "diagnostics",
                "perms": perms_data,
                "profile_valid": profile_valid,
                "profile_errors": profile_errors,
                "lock": lock,
                "watch_loop": watch_loop,
                "driver_ok": bool(drv.get("ok", False)),
                "driver_message": str(drv.get("message", "")),
                "driver_version": str(drv.get("version") or ""),
                "driver_accessibility": drv.get("accessibility"),
                "driver_screen_recording": drv.get("screen_recording"),
                "secrets_ok": bool(secrets.get("ok", True)),
                "secrets_findings": list(secrets.get("findings") or []),
                "diag_summary": f"{ok_count} OK · {warn_count} need attention" if warn_count else f"{ok_count} OK",
                "checked_at": datetime.now(_tz.utc).isoformat(),
                **_sidebar_ctx(data_dir, idle_app),
            },
        )

    return app
