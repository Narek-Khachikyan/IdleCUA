from __future__ import annotations

import os
import time
import json
import uuid
import threading
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
from .lock import acquire_lock, release_lock, get_lock_info, is_locked
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


# Global per-process scheduler state (live, not reconstructed from SQLite)
_scheduler_state = {
    "running": False,
    "pid": None,
    "started_at": None,
    "idle_threshold": 600,
}

# Global in-flight session guard (one session at a time per data dir)
_session_lock = threading.Lock()
_active_session: dict[str, Any] = {}  # data_dir -> task_id or None

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


def _watch_loop_worker(data_dir: Path, poll_interval: float = 5.0):
    """Background worker for ADR-0004: serve owns watch loop — pick queued tasks at next idle window."""
    import time as _time

    while not _watch_stop_event.is_set() and _scheduler_state.get("running"):
        try:
            idle_app = _get_idle_cua(data_dir)
            config = idle_app.config
            # Idle/screen decision owned by the Application API (T1); worker only renders/waits.
            try:
                idle_secs = float(idle_app.idle_detector.seconds_since_last_input())
            except Exception:
                idle_secs = 0.0
            threshold = idle_app.get_effective_idle_threshold()
            _scheduler_state["idle_threshold"] = threshold
            if idle_secs < threshold or idle_app.idle_detector.is_screen_locked():
                _time.sleep(poll_interval)
                continue
            # Find next queued task (waiting_for_idle)
            tasks = idle_app.memory.list_tasks(limit=50)
            queued = None
            for t in tasks:
                if t.get("state") in ("waiting_for_idle", "queued", "disabled"):
                    queued = t
                    break
            if queued is None:
                _time.sleep(poll_interval)
                continue
            # One-session-in-flight guard
            with _session_lock:
                # Re-check nothing running
                cur = idle_app.memory.list_tasks(limit=20)
                if any(x.get("state") in ("running", "planning") for x in cur):
                    _time.sleep(poll_interval)
                    continue
                tid = queued["id"]
                goal = queued.get("description", "")
                try:
                    # Mark running
                    idle_app.memory.update_task_state(tid, "running")
                    _active_session[str(data_dir)] = tid
                    from ..models.task import Task as _Task
                    from ..models.state import AgentState as _AS

                    task_obj = _Task(description=goal, id=tid, state=_AS.running)
                    # Honor queue-time decisions (Approve/Skip) baked into plan — unattended but pre-decided
                    dec_raw = idle_app.memory.kv_get(f"task_decisions:{tid}")
                    dec = json.loads(dec_raw) if dec_raw else {}
                    has_approvals = any(v == "approve" for v in dec.values()) if isinstance(dec, dict) else False

                    def _decide(action):
                        kind = getattr(action, "kind", "")
                        d = dec.get(kind) if isinstance(dec, dict) else None
                        if d == "approve":
                            return True
                        if d == "skip":
                            return False
                        return False

                    if has_approvals:
                        result = idle_app.run_task(task_obj, is_interactive=True, confirm_func=_decide)
                    else:
                        result = idle_app.run_task(task_obj, is_interactive=False)
                    _active_session[str(data_dir)] = None
                except Exception as e:
                    try:
                        idle_app.memory.update_task_state(tid, "failed")
                        idle_app.memory.record_error(str(uuid.uuid4()), tid, str(e))
                    except Exception:
                        pass
                    _active_session[str(data_dir)] = None
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
            # Map internal states to UI states: waiting_for_idle -> queued, disabled -> queued?
            state = t.get("state", "unknown")
            ui_state = state
            if state == "waiting_for_idle":
                ui_state = "queued"
            elif state == "disabled":
                ui_state = "queued"
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
        task = idle_app.create_task(payload.goal.strip())
        # Mark as queued (waiting_for_idle) for idle window — update state to valid enum
        try:
            idle_app.memory.update_task_state(task.id, "waiting_for_idle")
            # Also try to store decisions if provided (queue-time verdicts)
            if payload.decisions:
                # Persist decisions as kv for this plan
                idle_app.memory.kv_set(f"task_decisions:{task.id}", json.dumps(payload.decisions))
        except Exception:
            pass
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
        verdicts = idle_app.get_plan_verdicts(plan)
        for v in verdicts:
            v["label"] = humanize_action(v["action"], v.get("domain") or plan.target)
            if v["verdict"] == "blocked" and "allowlist" in v.get("reason","").lower():
                v["allowlist_link"] = "/settings#allowlist"
            # For needs-confirmation, indicate skipped in unattended
            if v["verdict"] == "needs-confirmation":
                v["unattended_action"] = "skipped in unattended runs"
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
        data = idle_app.memory.get_task(task_id)
        if not data:
            raise HTTPException(status_code=404, detail="task not found")
        # Guard: one session in flight
        with _session_lock:
            # Check if any task is currently running/planning
            tasks = idle_app.memory.list_tasks(limit=20)
            for t in tasks:
                if t.get("state") in ("running", "planning") and t.get("id") != task_id:
                    raise HTTPException(status_code=409, detail="session already in flight")
            # Also check idle gate — decision owned by the Application API (T1 single source).
            # Profile gate first so HTTP blocks identically to CLI with the same message.
            ok_p, reason_p = idle_app.check_profile_confirmed()
            if not ok_p:
                raise HTTPException(status_code=423, detail=f"Refused: {reason_p}")
            config = idle_app.config
            if config.require_idle:
                thr = idle_app.get_effective_idle_threshold()
                ok, reason = idle_app.idle_detector.can_run(thr)
                if not ok:
                    raise HTTPException(status_code=423, detail=f"idle gate blocked: {reason}")
                if idle_app.idle_detector.is_screen_locked():
                    raise HTTPException(status_code=423, detail="screen locked")
            # Schedule/limits gates also decide inside the Application API; map to 423 with the same reason text.
            # Idle/screen/profile already mapped above with legacy messages; only surface other gates here
            # so the explicit idle/screen messages above stay byte-identical.
            _readiness = idle_app.get_readiness()
            if not _readiness["can_start"] and _readiness.get("failed_gate") not in ("idle", "screen", "profile"):
                raise HTTPException(status_code=423, detail=f"Refused: {_readiness['reason']}")
            # Now run — honor queue-time Approve/Skip decisions baked into plan
            try:
                # Load decisions if previously stored (Approve/Skip per action)
                decisions_raw = idle_app.memory.kv_get(f"task_decisions:{task_id}")
                decisions = json.loads(decisions_raw) if decisions_raw else {}

                # Build confirm func from decisions: approved actions run even unattended
                def _confirm_from_decisions(action):
                    kind = getattr(action, "kind", "")
                    # decisions maps action kind -> "approve" or "skip"
                    decision = decisions.get(kind) if isinstance(decisions, dict) else None
                    if decision == "approve":
                        return True
                    if decision == "skip":
                        return False
                    # default unattended behavior: skip needs-confirmation
                    return False

                # If any Approve decisions exist, run in interactive mode with decisions; otherwise unattended (skip)
                has_approvals = any(v == "approve" for v in decisions.values()) if isinstance(decisions, dict) else False
                is_interactive = bool(has_approvals)
                confirm_fn = _confirm_from_decisions if is_interactive else None

                # Set state to running
                idle_app.memory.update_task_state(task_id, "running")
                # Use run_task with description
                goal = data.get("description", "")
                # Need to create Task object? Use app.run_task which creates new task — but we want to run existing task id
                # Instead we will use executor directly with existing task
                from ..models.task import Task
                from ..models.state import AgentState

                raw_state = data.get("state", "disabled")
                # Map UI queued to valid enum waiting_for_idle
                if raw_state == "queued":
                    raw_state = "waiting_for_idle"
                try:
                    state_enum = AgentState(raw_state)
                except ValueError:
                    state_enum = AgentState.waiting_for_idle
                task_obj = Task(description=goal, id=task_id, state=state_enum)
                # Run with decisions: approved confirmation actions will now be included via interactive path
                result = idle_app.run_task(task_obj, is_interactive=is_interactive, confirm_func=confirm_fn) if is_interactive else idle_app.run_task(task_obj, is_interactive=False)
                # Update active session tracking
                _active_session[str(data_dir)] = None
                return {
                    "task_id": task_id,
                    "state": result.state.value if hasattr(result.state, "value") else str(result.state),
                    "actions_executed": result.actions_executed,
                    "findings": result.findings,
                    "urls": result.urls,
                    "errors": result.errors,
                    "report_path": str(result.report_path) if result.report_path else None,
                }
            except HTTPException:
                raise
            except Exception as e:
                # Mark as failed
                try:
                    idle_app.memory.update_task_state(task_id, "failed")
                    idle_app.memory.record_error(str(uuid.uuid4()), task_id, str(e))
                except Exception:
                    pass
                raise HTTPException(status_code=500, detail=str(e))

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
        data_dir = resolved_data_dir
        idle_app = _get_idle_cua(data_dir)
        # permissions
        try:
            from ..profile.permissions import check_permissions

            perms = check_permissions()
            perms_data = [{"name": p.name, "granted": p.granted, "remediation": p.remediation} for p in perms]
        except Exception as e:
            perms_data = [{"error": str(e)}]
        # driver probe
        try:
            import cua_driver  # type: ignore

            driver_ok = True
            driver_msg = f"cua-driver {getattr(cua_driver, '__version__', 'unknown')}"
            try:
                status = cua_driver.current_mac_os_permission_status()
                driver_msg += f" — accessibility={getattr(status, 'accessibility', '?')} screen_recording={getattr(status, 'screen_recording', '?')}"
            except Exception as e:
                driver_msg += f" — probe failed: {e}"
        except ImportError:
            driver_ok = False
            driver_msg = "cua-driver not installed — pip install cua-driver==0.23.2"
        # profile validity
        profile = load_profile(data_dir / "profile.json")
        if profile is None:
            profile_valid = False
            profile_errors = ["No profile found"]
        else:
            errs = validate_profile(profile)
            profile_valid = len(errs) == 0
            profile_errors = errs
            if not profile.confirmed:
                profile_valid = False
                profile_errors = profile_errors + ["Profile is unconfirmed"]
        # secrets scan
        try:
            from ..secrets_scan import scan_project

            scan = scan_project(project_root=Path(__file__).resolve().parents[2], data_dir=data_dir)
            secrets_ok = scan.ok
            secrets_findings = [{"source": f.source, "pattern": f.pattern} for f in scan.findings[:5]]
        except Exception as e:
            secrets_ok = False
            secrets_findings = [{"error": str(e)}]
        # scheduler lock
        lock = get_lock_info(data_dir)
        lock_ok = not is_locked(data_dir) or lock is not None
        return {
            "permissions": perms_data,
            "driver": {"ok": driver_ok, "message": driver_msg},
            "profile": {"valid": profile_valid, "confirmed": bool(profile.confirmed) if profile else False, "errors": profile_errors},
            "secrets_scan": {"ok": secrets_ok, "findings": secrets_findings},
            "scheduler_lock": {"locked": is_locked(data_dir), "info": lock},
            "watch_loop": _scheduler_state,
        }

    @app.get("/api/v1/settings")
    def api_get_settings():
        data_dir = resolved_data_dir
        config = IdleCuaConfig.load(data_dir)
        profile = load_profile(data_dir / "profile.json")
        # One-time prefill: unset Profile fields inherit current Config values for review (ADR-0003)
        # If profile exists and confirmed but has empty allowlist etc., prefill from config without persisting
        if profile is None:
            profile_dict = Profile().model_dump()
        else:
            profile_dict = profile.model_dump()
            # Prefill empty owner-intent fields from config for one-time migration display
            ab_pref = profile_dict.get("autonomy_boundaries", {})
            if isinstance(ab_pref, dict) and not ab_pref.get("allowed_sites"):
                ab_pref["allowed_sites"] = list(config.allowlist)
            if isinstance(ab_pref, dict) and not ab_pref.get("deny_zones"):
                # Use preseeded deny_zones as default if profile has none
                ab_pref["deny_zones"] = list(config.deny_zones) if ab_pref.get("deny_zones") == [] else ab_pref.get("deny_zones", [])
            # idle threshold: one field seconds in Profile; if profile has default seconds but config is non-default, surface for review (do not auto-persist)
            cu_pref = profile_dict.get("computer_usage", {})
            if isinstance(cu_pref, dict) and cu_pref.get("idle_threshold_seconds", 600) == 600 and config.idle_threshold_seconds != 600:
                cu_pref["idle_threshold_seconds"] = int(config.idle_threshold_seconds)
                # Keep minutes in sync for display
                cu_pref["idle_threshold_minutes"] = max(1, (int(config.idle_threshold_seconds) + 59) // 60)

        # Extract owner-intent from profile
        ab = profile_dict.get("autonomy_boundaries", {}) if isinstance(profile_dict, dict) else {}
        cu = profile_dict.get("computer_usage", {}) if isinstance(profile_dict, dict) else {}
        # Single authority: idle threshold lives in Profile seconds
        idle_sec_profile = None
        if isinstance(cu, dict):
            secs = cu.get("idle_threshold_seconds")
            if isinstance(secs, int):
                idle_sec_profile = secs
            else:
                minutes = cu.get("idle_threshold_minutes")
                if isinstance(minutes, int):
                    idle_sec_profile = minutes * 60

        # Effective limits: tighten-only min(Profile, ceiling) in one place
        eff_session = min(ab.get("session_duration_minutes", 45) if isinstance(ab, dict) else 45, 45)
        eff_actions = min(ab.get("daily_action_limit", 200) if isinstance(ab, dict) else 200, 200)
        eff_llm = min(ab.get("daily_llm_call_limit", 150) if isinstance(ab, dict) else 150, 150)
        return {
            "profile": {
                "confirmed": profile.confirmed if profile else False,
                "session_duration_minutes": ab.get("session_duration_minutes", 45) if isinstance(ab, dict) else 45,
                "daily_action_limit": ab.get("daily_action_limit", 200) if isinstance(ab, dict) else 200,
                "daily_llm_call_limit": ab.get("daily_llm_call_limit", 150) if isinstance(ab, dict) else 150,
                "allowed_hours": ab.get("allowed_hours", "00:00-23:59") if isinstance(ab, dict) else "00:00-23:59",
                "allowlist": ab.get("allowed_sites", []) if isinstance(ab, dict) else [],
                "deny_zones": ab.get("deny_zones", []) if isinstance(ab, dict) else [],
                "allowed_sites": ab.get("allowed_sites", []) if isinstance(ab, dict) else [],
                "idle_threshold_seconds": idle_sec_profile if idle_sec_profile is not None else config.idle_threshold_seconds,
                "idle_threshold_minutes": cu.get("idle_threshold_minutes", 10) if isinstance(cu, dict) else 10,
                "browser_consent": ab.get("browser_consent", {}) if isinstance(ab, dict) else {},
            },
            "config": {
                "readonly": config.readonly,
                "require_idle": config.require_idle,
                "ceilings": {
                    "max_duration_minutes": 45,
                    "max_actions": 200,
                    "max_llm_calls_per_day": 150,
                },
                "current": {
                    "max_duration_minutes": config.max_duration_minutes,
                    "max_actions": config.max_actions,
                    "max_llm_calls_per_day": config.max_llm_calls_per_day,
                    "idle_threshold_seconds": config.idle_threshold_seconds,
                },
                "data_dir": str(config.data_dir),
            },
            "effective": {
                "session_duration_minutes": eff_session,
                "daily_action_limit": eff_actions,
                "daily_llm_call_limit": eff_llm,
                "idle_threshold_seconds": idle_sec_profile if idle_sec_profile is not None else config.idle_threshold_seconds,
            },
        }

    @app.patch("/api/v1/settings")
    def api_patch_settings(payload: SettingsPatch):
        data_dir = resolved_data_dir
        config = IdleCuaConfig.load(data_dir)
        profile = load_profile(data_dir / "profile.json")
        created = False
        if profile is None:
            profile = Profile()
            created = True
        # Apply Profile owner-intent fields with tighten-only validation
        ab = profile.autonomy_boundaries
        cu = profile.computer_usage

        # Session duration — tighten-only vs ceiling 45
        if payload.session_duration_minutes is not None:
            val = int(payload.session_duration_minutes)
            if val <= 0 or val > 45:
                raise HTTPException(status_code=400, detail="session_duration_minutes must be 1..45")
            # ceiling check — if profile value above ceiling, reject (not clamp)
            if val > 45:
                raise HTTPException(status_code=400, detail="session_duration_minutes above ceiling 45")
            ab.session_duration_minutes = val

        if payload.daily_action_limit is not None:
            val = int(payload.daily_action_limit)
            if val <= 0 or val > 1000:
                raise HTTPException(status_code=400, detail="daily_action_limit must be 1..1000")
            if val > 200:
                raise HTTPException(status_code=400, detail="daily_action_limit above ceiling 200")
            ab.daily_action_limit = val

        if payload.daily_llm_call_limit is not None:
            val = int(payload.daily_llm_call_limit)
            if val <= 0 or val > 1000:
                raise HTTPException(status_code=400, detail="daily_llm_call_limit must be 1..1000")
            if val > 150:
                raise HTTPException(status_code=400, detail="daily_llm_call_limit above ceiling 150")
            ab.daily_llm_call_limit = val

        if payload.allowed_hours is not None:
            ab.allowed_hours = str(payload.allowed_hours)

        if payload.allowlist is not None:
            # Validate domains
            from ..profile.validate import _is_valid_domain

            for site in payload.allowlist:
                if not _is_valid_domain(site):
                    raise HTTPException(status_code=400, detail=f"allowlist: invalid domain '{site}'")
            ab.allowed_sites = [s.strip().lower() for s in payload.allowlist]

        if payload.deny_zones is not None:
            # ADR-0003: deny-zones are owner-intent, stored in Profile (not Config). Config keeps preseed defaults immutable.
            ab.deny_zones = list(payload.deny_zones)

        # Idle threshold — single field seconds in Profile (ADR-0003), no precision loss
        if payload.idle_threshold_seconds is not None:
            val = int(payload.idle_threshold_seconds)
            if val < 60 or val > 7200:
                raise HTTPException(status_code=400, detail="idle_threshold_seconds must be 60..7200")
            cu.idle_threshold_seconds = val
            # Keep minutes in sync for back-compat display
            cu.idle_threshold_minutes = max(1, min(120, (val + 59) // 60))

        if payload.browser_consent is not None:
            bc = BrowserConsent(main_profile_granted=bool(payload.browser_consent), browser="chrome")
            ab.browser_consent = bc
            profile.browser_consent = bc

        # Config safety toggles
        if payload.readonly is not None:
            config.readonly = bool(payload.readonly)
        if payload.require_idle is not None:
            config.require_idle = bool(payload.require_idle)

        # Validate profile before saving (tighten-only)
        errs = validate_profile(profile)
        if errs:
            raise HTTPException(status_code=400, detail="; ".join(errs))

        # Save both
        save_profile(profile, data_dir / "profile.json")
        config.save()
        # Mirror browser consent
        try:
            if payload.browser_consent is not None:
                from ..browser_consent import record_consent

                record_consent(data_dir, bool(payload.browser_consent))
        except Exception:
            pass

        return {"ok": True, "profile": profile.model_dump(), "config": config.to_dict()}

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
        # Try to acquire lock
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
                raise HTTPException(status_code=409, detail=str(e))
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/v1/scheduler/stop")
    def api_scheduler_stop():
        data_dir = resolved_data_dir
        _scheduler_state["running"] = False
        _scheduler_state["pid"] = None
        _scheduler_state["started_at"] = None
        _stop_watch_worker()
        release_lock(data_dir)
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
        try:
            idle_app.request_emergency_stop(reason)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
        # Also set kv
        try:
            idle_app.memory.kv_set("last_stop_reason", reason)
        except Exception:
            pass
        # Mark active task as stopped if any
        try:
            st = idle_app.get_status()
            active = st.get("active_task")
            if active:
                tid = active.get("id")
                idle_app.memory.update_task_state(tid, "stopped")
                idle_app.memory.record_error(str(uuid.uuid4()), tid, f"emergency stop: {reason}")
        except Exception:
            pass
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
            {"id": "profile", "label": "Profile confirmed", "desc": "Conservative defaults — readonly, 45 min, 14 sites", "done": bool(profile and profile.confirmed), "cta": "Confirmed ✓" if profile and profile.confirmed else "Confirm defaults"},
            {"id": "provider", "label": "Provider key", "desc": "No LLM key — stub planner only" if not has_key else "Provider key configured", "done": has_key, "cta": "Add key" if not has_key else "Configured"},
            {"id": "permissions", "label": "macOS permissions", "desc": "Accessibility & Screen Recording granted" if perms_ok else "Grant Accessibility & Screen Recording", "done": perms_ok, "cta": "Granted" if perms_ok else "Check"},
            {"id": "browser", "label": "Browser consent", "desc": "Agent may use main Chrome — own tabs only" if browser_ok else "Grant browser consent", "done": browser_ok, "cta": "Granted" if browser_ok else "Grant"},
        ]
        done_count = sum(1 for c in checklist if c["done"])
        # Today's usage meters
        from ..accounting import get_today_count

        llm_today = get_today_count(data_dir)
        # Tasks
        tasks = idle_app.memory.list_tasks(limit=20)
        # Enrich tasks with result counts and ui_state
        tasks_enriched = []
        for t in tasks:
            tid = t["id"]
            # findings/urls counts
            try:
                findings = idle_app.memory.list_findings(task_id=tid)
                all_urls = idle_app.memory.list_urls(limit=500)
                task_urls = [u for u in all_urls if u.get("task_id") == tid]
                errors = idle_app.memory.list_errors(task_id=tid)
            except Exception:
                findings = []
                task_urls = []
                errors = []
            state = t.get("state", "unknown")
            ui_state = state
            if state == "waiting_for_idle":
                ui_state = "queued"
            elif state == "disabled":
                ui_state = "queued"
            # result string
            result_str = f"{len(findings)} findings · {len(task_urls)} urls" if state not in ("queued", "waiting_for_idle", "disabled") else "—"
            # failure reason for Retry
            failure_reason = ""
            if state == "failed" and errors:
                failure_reason = errors[0].get("message", "")[:80] if errors else ""
            tasks_enriched.append({
                **t,
                "ui_state": ui_state,
                "findings_count": len(findings),
                "urls_count": len(task_urls),
                "result_str": result_str,
                "failure_reason": failure_reason,
            })

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
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "config": config,
                "effective_idle_threshold": effective_idle,
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
        return templates.TemplateResponse(request, "partials/status_banner.html", {"honest": honest, "demo_mode": demo_mode})

    @app.get("/tasks", response_class=HTMLResponse)
    def ui_tasks(request: Request):
        # Simple tasks page
        if templates is None:
            return HTMLResponse("<html><body>Tasks</body></html>")
        data_dir = resolved_data_dir
        idle_app = _get_idle_cua(data_dir)
        tasks = idle_app.memory.list_tasks(limit=100)
        return templates.TemplateResponse(request, "tasks.html", {"tasks": tasks})

    @app.get("/history", response_class=HTMLResponse)
    def ui_history(request: Request):
        if templates is None:
            return HTMLResponse("<html><body>History</body></html>")
        idle_app = _get_idle_cua(resolved_data_dir)
        task_filter = request.query_params.get("task_id") or request.query_params.get("task")
        # Reuse single helper for API/UI filtering (DRY)
        hist = _fetch_history_filtered(idle_app, task_filter, limit=200)
        tasks = idle_app.memory.list_tasks(limit=100)
        return templates.TemplateResponse(request, "history.html", {"history": hist, "tasks": tasks, "selected_task": task_filter})

    @app.get("/reports", response_class=HTMLResponse)
    def ui_reports(request: Request):
        if templates is None:
            return HTMLResponse("<html><body>Reports</body></html>")
        idle_app = _get_idle_cua(resolved_data_dir)
        reports = idle_app.list_reports(limit=20)
        return templates.TemplateResponse(request, "reports.html", {"reports": reports})

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
        # Render markdown as html? Simple: wrap in <pre>
        import html as _html

        md_html = "<pre>" + _html.escape(rep.get("markdown","")) + "</pre>"
        return templates.TemplateResponse(request, "report_detail.html", {"task_id": task_id, "markdown": rep.get("markdown",""), "markdown_html": md_html})

    @app.get("/settings", response_class=HTMLResponse)
    def ui_settings(request: Request):
        if templates is None:
            return HTMLResponse("<html><body>Settings</body></html>")
        data_dir = resolved_data_dir
        config = IdleCuaConfig.load(data_dir)
        profile = load_profile(data_dir / "profile.json")
        return templates.TemplateResponse(request, "settings.html", {"config": config, "profile": profile})

    @app.get("/diagnostics", response_class=HTMLResponse)
    def ui_diagnostics(request: Request):
        if templates is None:
            return HTMLResponse("<html><body>Diagnostics</body></html>")
        data_dir = resolved_data_dir
        # reuse api diagnostics logic but render html
        # call api_diagnostics inner? Instead duplicate
        try:
            from ..profile.permissions import check_permissions

            perms = check_permissions()
            perms_data = [{"name": p.name, "granted": p.granted, "remediation": p.remediation} for p in perms]
        except Exception as e:
            perms_data = [{"error": str(e)}]
        profile = load_profile(data_dir / "profile.json")
        if profile is None:
            profile_valid = False
            profile_errors = ["No profile"]
        else:
            errs = validate_profile(profile)
            profile_valid = len(errs) == 0 and profile.confirmed
            profile_errors = errs
        lock = get_lock_info(data_dir)
        return templates.TemplateResponse(request, "diagnostics.html", {"perms": perms_data, "profile_valid": profile_valid, "profile_errors": profile_errors, "lock": lock, "watch_loop": _scheduler_state})

    return app
