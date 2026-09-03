from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table
from rich.markdown import Markdown

from .app import IdleCua
from .cli_models import models_app
from .cli_profile import profile_app
from .config import IdleCuaConfig

app = typer.Typer(
    name="idle-cua",
    help="IdleCUA — policy-gated idle-time computer-use agent.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

app.add_typer(models_app, name="models")
app.add_typer(profile_app, name="profile")

def _config_for_cli(data_dir: Optional[str]) -> IdleCuaConfig:
    if data_dir is not None:
        return IdleCuaConfig(data_dir=Path(data_dir).expanduser())
    return IdleCuaConfig()

def _resolve_data_dir(data_dir: Optional[str]) -> Path:
    if data_dir is not None:
        return Path(data_dir).expanduser()
    env = os.environ.get("IDLECUA_DATA_DIR") or os.environ.get("IDLE_CUA_DATA_DIR")
    if env:
        return Path(env).expanduser()
    return IdleCuaConfig().data_dir


def is_loopback_host(host: str) -> bool:
    """Public seam for v1 bind validation — only loopback allowed per ADR-0005."""
    return host in ("127.0.0.1", "localhost", "::1")


def validate_bind_host(host: str) -> None:
    """Validate v1 bind host; rejects non-loopback with loud error per spec."""
    if not is_loopback_host(host):
        console.print(f"[red]Non-loopback bind '{host}' rejected in v1 — only 127.0.0.1 allowed[/red]")
        raise typer.Exit(1)


def _effective_idle_threshold_for_cli(data_dir: Path, config: IdleCuaConfig) -> int:
    """Thin caller per ADR-0002: delegate to the Application API single source."""
    try:
        idle = IdleCua(config=config)
        return int(idle.get_effective_idle_threshold())
    except Exception:
        return int(getattr(config, "idle_threshold_seconds", 600))

def _print_plan(idle: IdleCua, p, title: str, json_output: bool) -> None:
    if json_output:
        console.print_json(json.dumps(idle.plan_to_dict(p)))
        return
    table = Table(title=title, show_header=True)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("goal", p.goal)
    table.add_row("target", p.target)
    table.add_row("expected_actions", ", ".join(p.expected_actions))
    table.add_row("expected_result", p.expected_result)
    table.add_row("max_duration_minutes", str(p.max_duration_minutes))
    table.add_row("max_actions", str(p.max_actions))
    table.add_row("risk_level", p.risk_level.value)
    table.add_row("requires_confirmation", str(p.requires_confirmation))
    console.print(table)
    # Policy verdicts per plan item — layered: allowlist → deny-zone → action class
    verdicts = idle.get_plan_verdicts(p)
    vtable = Table(title="Policy verdicts (allowlist → deny-zone → action class)", show_header=True)
    vtable.add_column("Action", style="bold")
    vtable.add_column("Verdict")
    vtable.add_column("Reason")
    for v in verdicts:
        verdict = v["verdict"]
        style = "green" if verdict == "allowed" else "yellow" if verdict == "needs-confirmation" else "red"
        vtable.add_row(v["action"], f"[{style}]{verdict}[/{style}]", v["reason"])
    console.print(vtable)


def _check_profile_gate(data_dir: Optional[str]) -> tuple[Path, object]:
    """Thin caller per ADR-0002: decision lives in IdleCua; CLI only renders/maps."""
    _resolved = _resolve_data_dir(data_dir)
    _ppath = _resolved / "profile.json"
    try:
        from .config import IdleCuaConfig as _Cfg

        _idle = IdleCua(config=_Cfg(data_dir=_resolved))
        ok, reason = _idle.check_profile_confirmed()
    except Exception as e:
        ok, reason = False, str(e)
    if not ok:
        console.print(f"[red]Refused: {reason}[/red]")
        # Preserve the helpful hint for the missing-profile case (byte-identical guidance as before).
        if "No profile found" in str(reason):
            console.print("[dim]Hint: run `idle-cua profile interview` and confirm, or `idle-cua profile show` / `validate` to fix.[/dim]")
        raise typer.Exit(1)
    try:
        _profile = _idle.get_profile()
    except Exception:
        _profile = None
    return _ppath, _profile


@app.command()
def init(
    data_dir: Annotated[
        Optional[str],
        typer.Option("--data-dir", help="Data directory (default: ~/.idlecua or $IDLECUA_DATA_DIR)"),
    ] = None,
) -> None:
    """Create the data directory and default config."""
    config = _config_for_cli(data_dir)
    idle = IdleCua(config=config)
    created = idle.init_data_dir()
    # Ensure config file exists with defaults (idempotent)
    if not config.config_path.exists():
        config.save()
    console.print(f"[green]Initialized[/green] data dir: {created}")
    console.print(f"Config: {config.config_path}")

@app.command()
def plan(
    task: Annotated[str, typer.Argument(help="Task description")],
    data_dir: Annotated[
        Optional[str],
        typer.Option("--data-dir", help="Data directory"),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print raw JSON instead of table"),
    ] = False,
) -> None:
    """Print a bounded, typed plan for TASK (dry-run, executes nothing)."""
    if not task or not task.strip():
        console.print("[red]Task description must be non-empty[/red]")
        raise typer.Exit(code=2)
    config = _config_for_cli(data_dir)
    idle = IdleCua(config=config)
    # Handle LLM planner rejection/cap: plan() may raise PlanRejectedError, or fallback to stub on cap.
    # Cap usage is visible via status/report; we surface a hint here as well.
    try:
        p = idle.dry_run(task)
    except Exception as e:
        # Distinguish plan rejection (unconvertible LLM output never executed as free text)
        msg = str(e)
        if "rejected" in msg.lower() or "not convertible" in msg.lower() or "planrejected" in type(e).__name().lower():
            console.print(f"[red]Plan rejected: LLM output could not be converted to typed actions and was not executed: {e}[/red]")
            raise typer.Exit(code=1)
        console.print(f"[red]Planning failed: {e}[/red]")
        raise typer.Exit(code=1)
    _print_plan(idle, p, "IdleCUA Plan (dry-run — no actions executed)", json_output)
    # Cap visibility for dry-run path: show LLM usage
    try:
        from .accounting import get_today_count

        cnt = get_today_count(idle.config.data_dir)
        cap = idle.config.max_llm_calls_per_day
        if cnt >= cap:
            console.print(f"[yellow]LLM call cap reached: {cnt}/{cap} today — LLM planning fell back to deterministic stub (graceful). See `idle-cua status` / report for limits.[/yellow]")
        elif cnt >= cap * 0.9:
            console.print(f"[yellow]LLM calls today: {cnt}/{cap} (approaching cap)[/yellow]")
    except Exception:
        pass

@app.command(name="run-once")
def run_once(
    task: Annotated[str, typer.Argument(help="Task description")],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the plan and execute nothing"),
    ] = False,
    data_dir: Annotated[
        Optional[str],
        typer.Option("--data-dir", help="Data directory"),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print raw JSON instead of table"),
    ] = False,
    real_driver: Annotated[
        bool,
        typer.Option("--real-driver", help="Use real Cua driver (host primitives) instead of fake; requires cua-driver==0.23.2 and macOS permissions"),
    ] = False,
    interactive: Annotated[
        bool,
        typer.Option("--interactive", help="Enable interactive y/n confirmation for confirmation-required actions (shows action/target/payload)"),
    ] = False,
) -> None:
    """Run a task once (policy-gated, persists to SQLite, produces report)."""
    if not task or not task.strip():
        console.print("[red]Task description must be non-empty[/red]")
        raise typer.Exit(code=2)
    if not dry_run:
        _check_profile_gate(data_dir)
        config = _config_for_cli(data_dir)
        if real_driver:
            config.use_real_driver = True
        # When real driver is requested, pre-check permissions for actionable errors
        if real_driver or getattr(config, "use_real_driver", False):
            from .profile.permissions import check_permissions as _check_perms

            statuses = _check_perms()
            missing = [s for s in statuses if s.granted is False]
            unknown = [s for s in statuses if s.granted is None]
            if missing:
                for s in missing:
                    console.print(f"[red][MISSING] {s.name}: NOT granted[/red]")
                    console.print(f"  Remediation: {s.remediation}")
                console.print("[yellow]Refusing real-driver run until permissions granted. Fix with: System Settings → Privacy & Security → Accessibility / Screen Recording, then restart terminal.[/yellow]")
                console.print("[dim]Tip: run `idle-cua doctor` for full diagnostics. Smoke without real driver: omit --real-driver (uses FakeComputerDriver).[/dim]")
                raise typer.Exit(1)
            if unknown:
                console.print("[yellow]Permissions unknown (cua probe inconclusive) — attempting real driver anyway (may fail with actionable error).[/yellow]")

        # Inject real driver if requested (fail loudly, don't silently fallback)
        computer = None
        if real_driver or getattr(config, "use_real_driver", False):
            try:
                from .drivers.cua_driver import CuaComputerDriver

                computer = CuaComputerDriver(session="idlecua", data_dir=config.data_dir)
                console.print("[green]Using real Cua driver (cua-driver==0.23.2) — host primitives active.[/green]")
            except Exception as e:
                console.print(f"[red]Real Cua driver init failed: {e}[/red]")
                console.print("[dim]Remediation: `uv pip install cua-driver==0.23.2` and grant Accessibility + Screen Recording. See README `Cua driver install` section.[/dim]")
                raise typer.Exit(1)
            idle = IdleCua(config=config, computer=computer)
        else:
            idle = IdleCua(config=config)
        # Real execution via fake driver (persists, produces report)
        # Build interactive confirm callback with full disclosure when --interactive
        confirm_cb = None
        if interactive:
            from rich.prompt import Confirm as _CliConfirm
            def _cli_confirm(action):  # type: ignore
                console.print(f"[yellow]Confirmation required:[/yellow] {action.kind} → {action.target_url or '(local)'}")
                console.print(f"  Description: {action.description}")
                console.print(f"  Payload: {action.payload}")
                # Policy reason is available via app.check_action if needed, but action disclosure suffices
                console.print(f"  Consequences: this is a confirmation-required action (may change external state)")
                try:
                    return _CliConfirm.ask("Allow this action?", console=console, default=False)
                except Exception:
                    return False
            confirm_cb = _cli_confirm
        try:
            result = idle.run_once(task, dry_run=False, is_interactive=interactive, confirm_func=confirm_cb)
            # result is ExecutionResult
            if json_output:
                payload = {
                    "task_id": result.task_id,
                    "state": result.state.value if hasattr(result.state, "value") else str(result.state),
                    "plan": idle.plan_to_dict(result.plan),
                    "actions_executed": result.actions_executed,
                    "findings": result.findings,
                    "urls": result.urls,
                    "queries": result.queries,
                    "errors": result.errors,
                    "skipped_repeats": result.skipped_repeats,
                    "report_path": str(result.report_path) if result.report_path else None,
                    "limits": result.limits,
                }
                console.print_json(json.dumps(payload))
                return
            # Human readable: show report
            console.print(f"[green]Task completed:[/green] {result.state.value if hasattr(result.state,'value') else result.state}")
            console.print(f"Task ID: {result.task_id}")
            console.print(f"Actions executed: {result.actions_executed} / {result.plan.max_actions}")
            if result.findings:
                ftable = Table(title="Findings")
                ftable.add_column("Title")
                ftable.add_column("URL")
                for f in result.findings[:5]:
                    ftable.add_row(f.get("title",""), f.get("url",""))
                console.print(ftable)
            if result.skipped_repeats:
                console.print("[yellow]Skipped repeats:[/yellow]")
                for s in result.skipped_repeats:
                    console.print(f"  - {s.get('type')}: {s.get('value')} — {s.get('reason')}")
            if result.errors:
                console.print("[yellow]Errors:[/yellow]")
                for e in result.errors[:5]:
                    console.print(f"  - {e.get('message')}")
            console.print(f"Report: {result.report_path}")
            # Also print markdown preview
            try:
                md = Markdown(result.report_markdown[:4000])
                console.print(md)
            except Exception:
                console.print(result.report_markdown[:2000])
            # For CLI, exit 0 on completed, 1 on failed/paused, 2 on stopped? Keep 0 for now
            if str(result.state) == "failed":
                raise typer.Exit(1)
            return
        except typer.Exit:
            raise
        except Exception as e:
            console.print(f"[red]Execution failed: {e}[/red]")
            raise typer.Exit(1)
    # dry-run path
    config = _config_for_cli(data_dir)
    if real_driver:
        config.use_real_driver = True
        try:
            from .drivers.cua_driver import CuaComputerDriver

            computer = CuaComputerDriver(session="idlecua", data_dir=config.data_dir)
            idle = IdleCua(config=config, computer=computer)
        except Exception as e:
            console.print(f"[red]Real driver not available for dry-run: {e}[/red]")
            idle = IdleCua(config=config)
    else:
        idle = IdleCua(config=config)
    p = idle.dry_run(task)
    _print_plan(idle, p, "IdleCUA run-once --dry-run (no actions executed)", json_output)


@app.command()
def doctor(
    data_dir: Annotated[
        Optional[str],
        typer.Option("--data-dir", help="Data directory"),
    ] = None,
) -> None:
    """Run permission checks + profile validate for quick diagnostics."""
    from .profile.permissions import check_permissions as _check_perms
    from .profile.permissions import permissions_report_text as _perm_text
    from .profile.store import load_profile as _load_profile
    from .profile.validate import validate_profile as _validate

    console.print(_perm_text(_check_perms()))
    # Cua driver diagnostics
    console.print("")
    console.print("[bold]Cua Driver[/bold]")
    console.print("==============")
    try:
        import cua_driver  # type: ignore

        console.print(f"cua-driver version: {getattr(cua_driver, '__version__', 'unknown')} (pinned 0.23.2 expected)")
        try:
            status = cua_driver.current_mac_os_permission_status()
            acc = getattr(status, "accessibility", None)
            scr = getattr(status, "screen_recording", None)
            console.print(f"  Accessibility (cua probe): {'granted' if acc else 'NOT granted' if acc is False else 'unknown'}")
            console.print(f"  Screen Recording (cua probe): {'granted' if scr else 'NOT granted' if scr is False else 'unknown'}")
        except Exception as e:
            console.print(f"  Cua permission probe failed: {e}")
        # Try init driver (embedded runtime, no separate process required)
        try:
            drv = cua_driver.CuaDriver.create(None)
            console.print("  Driver init: [green]OK[/green] (embedded runtime)")
            # Quick screenshot probe (will fail if Screen Recording missing)
            import asyncio as _asyncio, json as _json

            async def _probe():
                try:
                    r = await drv.get_desktop_state(cua_driver.GetDesktopStateInput(session=None, screenshot_out_file=None))
                    console.print(f"  Screenshot probe: {'OK' if not getattr(r, 'is_error', False) else 'FAILED'} — {getattr(r, 'text', '')[:120]}")
                    # Also test list_windows style via call_tool
                    r2 = await drv.call_tool("get_accessibility_tree", "{}")
                    console.print(f"  Accessibility tree probe: {'OK' if not getattr(r2, 'is_error', False) else 'FAILED'}")
                except Exception as pe:
                    console.print(f"  Probe failed: {pe}")
                await drv.shutdown()

            _asyncio.run(_probe())
        except Exception as e:
            console.print(f"  Driver init: [red]FAILED[/red] {e}")
            console.print("  Remediation: `uv pip install cua-driver==0.23.2` and grant permissions. Docs: https://cua.ai/docs/how-to-guides/driver/install")
    except ImportError:
        console.print("cua-driver not installed. Install: `uv pip install cua-driver==0.23.2`")
        console.print("Docs: https://cua.ai/docs/how-to-guides/driver/install")
    _resolved = _resolve_data_dir(data_dir)
    _ppath = _resolved / "profile.json"
    _profile = _load_profile(_ppath)
    if _profile is None:
        console.print(f"[yellow]No profile at {_ppath} — run `idle-cua profile interview`[/yellow]")
    else:
        _errs = _validate(_profile)
        if _errs:
            console.print("[red]Profile validation errors:[/red]")
            for e in _errs:
                console.print(f"  - {e}")
        else:
            console.print("[green]Profile validation: OK[/green]")
        # Browser main-profile consent check (issue #12)
        try:
            bc = _profile.autonomy_boundaries.browser_consent
            if bc.main_profile_granted:
                console.print(f"[green]Browser main-profile consent: GRANTED[/green] ({bc.browser}, {bc.granted_at}, via {bc.grant_method})")
            else:
                console.print("[yellow]Browser main-profile consent: NOT granted[/yellow] (run `idle-cua profile grant-browser`; agent will not attach to main Chrome profile)")
        except Exception:
            pass
        # Config mirror check
        try:
            from .config import IdleCuaConfig as _Cfg

            _cfg = _Cfg.load(_resolved)
            if _cfg.browser_main_profile_granted:
                console.print(f"[green]Config browser consent: GRANTED[/green] ({_cfg.browser_main_profile_browser})")
            else:
                console.print("[dim]Config browser consent: not granted (mirrors profile)[/dim]")
        except Exception:
            pass
        # Driver existing-profile grant hint
        console.print("")
        console.print("[bold]Browser Driver Grant (existing-profile)[/bold]")
        console.print("[dim]Driver requires `cua-driver serve --grant existing-profile` (separate process) or embedded grant for main-profile attachment.[/dim]")
        console.print("[dim]When granted, `get_browser_state` binds to your running Chrome window via CDP.[/dim]")
        if _profile and hasattr(_profile.autonomy_boundaries, "browser_consent") and not _profile.autonomy_boundaries.browser_consent.main_profile_granted:
            console.print("[yellow]Profile browser consent not granted — grant first, then restart driver with grant.[/yellow]")
        else:
            console.print("[dim]If profile consent is granted but driver still refuses (browser_consent_required), restart driver with --grant existing-profile.[/dim]")

    # Secrets-absent verification (issue #13)
    console.print("")
    console.print("[bold]Secrets Scan[/bold]")
    console.print("==============")
    try:
        from .secrets_scan import format_report, scan_project

        _proj_root = Path(__file__).resolve().parents[2]
        # Detect repo root (where .git lives) — walk up from project file parents
        _repo_root = _proj_root
        for parent in [Path.cwd(), _resolved, _proj_root, _proj_root.parent]:
            if (parent / ".git").exists():
                _repo_root = parent
                break
            if (parent / "pyproject.toml").exists() and (parent / "src").exists():
                _repo_root = parent
        result = scan_project(project_root=_repo_root, data_dir=_resolved)
        console.print(format_report(result, verbose=False))
        if not result.ok:
            console.print("[red]Secrets scan FAILED — see findings above. Remediation: remove secrets from repo/data_dir; keys live only in Keychain/env.[/red]")
        else:
            console.print("[green]Secrets scan PASSED — no API keys/tokens/passwords in repo, reports, logs, or DB.[/green]")
    except Exception as e:
        console.print(f"[yellow]Secrets scan skipped: {e}[/yellow]")


@app.command()
def status(
    data_dir: Annotated[
        Optional[str],
        typer.Option("--data-dir", help="Data directory"),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output JSON"),
    ] = False,
) -> None:
    """Show status: agent state, idle time, active task, last action, current site/app, limit usage, stop command."""
    from .profile.store import load_profile as _load_profile

    _resolved = _resolve_data_dir(data_dir)
    config = IdleCuaConfig(data_dir=_resolved)
    idle = IdleCua(config=config)
    # Watch loop observed data — CLI owns live state observation (never owned by app)
    try:
        from .server.lock import get_lock_info as _get_lock_info_cli

        _lock_info_cli = _get_lock_info_cli(_resolved)
        _watch_loop_cli = {
            "running": False,
            "pid": _lock_info_cli.get("pid") if _lock_info_cli else None,
            "started_at": None,
            "lock": _lock_info_cli,
        }
    except Exception:
        _watch_loop_cli = None
    st = idle.get_status_enriched(watch_loop=_watch_loop_cli)

    if json_output:
        # Versioned contract (same keys as GET /api/v1/status) plus legacy idle_time for CLI compat
        payload = {
            "agent_state": str(st.get("agent_state")),
            "idle_time": st.get("idle_time"),
            "idle_seconds": st.get("idle_seconds"),
            "idle_threshold_seconds": st.get("idle_threshold_seconds"),
            "screen_locked": st.get("screen_locked"),
            "watch_loop": st.get("watch_loop"),
            "demo_mode": st.get("demo_mode"),
            "honest_status": st.get("honest_status"),
            "limits": st.get("limits"),
            "daily_usage": st.get("daily_usage"),
            "today_usage": st.get("today_usage"),
            "last_report": st.get("last_report"),
            "active_task": st.get("active_task"),
            "last_action": st.get("last_action"),
            "current_site": st.get("current_site"),
            "stop_command": st.get("stop_command"),
            # legacy flat limits for backward compat
            "llm_calls_today": st.get("llm_calls_today"),
            "max_llm_calls_per_day": st.get("max_llm_calls_per_day"),
            "max_actions": st.get("max_actions"),
            "max_duration_minutes": st.get("max_duration_minutes"),
        }
        console.print_json(json.dumps(payload))
        return

    # Rich table status panel
    _ppath = _resolved / "profile.json"
    _profile = _load_profile(_ppath)
    if _profile is None:
        console.print(f"[yellow]No profile at {_ppath}[/yellow]")
        console.print("Profile: missing — run `idle-cua profile interview`")
    elif not _profile.confirmed:
        console.print(f"[yellow]Profile at {_ppath} is unconfirmed[/yellow]")
        console.print("Profile: unconfirmed — autonomous runs blocked")
    else:
        from .profile.validate import validate_profile as _validate

        _errs = _validate(_profile)
        if _errs:
            console.print(f"[red]Profile at {_ppath} invalid: {'; '.join(_errs)}[/red]")
        else:
            console.print(f"[green]Profile at {_ppath} is confirmed and valid[/green]")

    console.print(f"Data dir: {_resolved}")
    # Browser consent status line
    if _profile is not None:
        try:
            bc = _profile.autonomy_boundaries.browser_consent
            if bc.main_profile_granted:
                console.print(f"[green]Browser main-profile consent: GRANTED ({bc.browser})[/green]")
            else:
                console.print("[yellow]Browser main-profile consent: NOT granted[/yellow] — run `idle-cua profile grant-browser`")
        except Exception:
            pass
    # Demo badge — identical to server's honest_status chip/banners (stub never presented as LLM work)
    honest_cli = st.get("honest_status") or {}
    if st.get("demo_mode"):
        console.print(f"[black on yellow] DEMO [/] {honest_cli.get('chip_text','Limited mode')} — {honest_cli.get('banner_text','Limited mode — stub planner · LLM off')}")
        console.print("[dim]Stub planner active — LLM off; outputs are stub, not LLM work.[/dim]")
    table = Table(title="IdleCUA Status", show_header=True)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Agent state", str(st.get("agent_state")))
    table.add_row("Idle time", str(st.get("idle_time")))
    table.add_row("Screen locked", str(st.get("screen_locked")))
    active = st.get("active_task")
    if active:
        table.add_row("Active task", f"{active.get('id','')[:8]} — {active.get('description','')} [{active.get('state','')}]")
    else:
        table.add_row("Active task", "(none)")
    last = st.get("last_action")
    if last:
        table.add_row("Last action", f"{last.get('kind','')} → {last.get('target_url','') or '(local)'} [{last.get('status','')}]")
    else:
        table.add_row("Last action", "(none)")
    table.add_row("Current site/app", str(st.get("current_site") or "(none)"))
    table.add_row("LLM calls today", f"{st.get('llm_calls_today',0)} / {st.get('max_llm_calls_per_day',150)}")
    table.add_row("Max actions/session", str(st.get("max_actions")))
    table.add_row("Max duration", f"{st.get('max_duration_minutes')} min")
    table.add_row("Stop command", str(st.get("stop_command")))
    console.print(table)


@app.command()
def history(
    data_dir: Annotated[
        Optional[str],
        typer.Option("--data-dir", help="Data directory"),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output JSON"),
    ] = False,
    limit: Annotated[
        int,
        typer.Option("--limit", help="Max entries"),
    ] = 50,
) -> None:
    """List queries and visited URLs from SQLite history."""
    _resolved = _resolve_data_dir(data_dir)
    config = IdleCuaConfig(data_dir=_resolved)
    idle = IdleCua(config=config)
    hist = idle.get_history(limit=limit)
    if json_output:
        console.print_json(json.dumps(hist))
        return

    q_table = Table(title="Queries (recent)", show_header=True)
    q_table.add_column("Query")
    q_table.add_column("Normalized")
    q_table.add_column("When")
    for q in hist.get("queries", [])[:limit]:
        q_table.add_row(q.get("query",""), q.get("normalized",""), q.get("created_at","")[:19])
    console.print(q_table)

    u_table = Table(title="Visited URLs (recent)", show_header=True)
    u_table.add_column("URL")
    u_table.add_column("Fingerprint")
    u_table.add_column("When")
    for u in hist.get("urls", [])[:limit]:
        u_table.add_row(u.get("url",""), (u.get("fingerprint","")[:8]), u.get("created_at","")[:19])
    console.print(u_table)

    t_table = Table(title="Tasks (recent)", show_header=True)
    t_table.add_column("ID")
    t_table.add_column("Description")
    t_table.add_column("State")
    for t in hist.get("tasks", [])[:limit]:
        t_table.add_row(t.get("id","")[:8], t.get("description","")[:40], t.get("state",""))
    console.print(t_table)


@app.command()
def report(
    task_id: Annotated[
        Optional[str],
        typer.Argument(help="Task ID (default: most recent)"),
    ] = None,
    data_dir: Annotated[
        Optional[str],
        typer.Option("--data-dir", help="Data directory"),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output JSON"),
    ] = False,
) -> None:
    """Show Markdown session report (tasks, queries, findings, skipped repeats, errors, limits)."""
    _resolved = _resolve_data_dir(data_dir)
    config = IdleCuaConfig(data_dir=_resolved)
    idle = IdleCua(config=config)
    # Determine task_id
    target_id = task_id
    if not target_id:
        tasks = idle.memory.list_tasks(limit=1)
        if not tasks:
            console.print("[yellow]No tasks found — no reports yet[/yellow]")
            raise typer.Exit(1)
        target_id = tasks[0]["id"]
    rep = idle.get_report(target_id)
    if not rep:
        console.print(f"[yellow]No report for task {target_id}[/yellow]")
        raise typer.Exit(1)
    md = rep.get("markdown","")
    if json_output:
        console.print_json(json.dumps({"task_id": target_id, "markdown": md}))
        return
    console.print(Markdown(md))
    # Also show path
    rpath = _resolved / "reports" / f"{target_id}.md"
    console.print(f"[dim]Report file: {rpath}[/dim]")
    # Daily report path if exists
    daily = _resolved / "reports" / f"{__import__('datetime').datetime.now(__import__('datetime').timezone.utc).date().isoformat()}.md"
    if daily.exists() and daily != rpath:
        console.print(f"[dim]Daily report: {daily}[/dim]")


@app.command(name="verify-secrets")
def verify_secrets(
    data_dir: Annotated[
        Optional[str],
        typer.Option("--data-dir", help="Data directory"),
    ] = None,
    project_root: Annotated[
        Optional[str],
        typer.Option("--project-root", help="Repo root to scan (default: auto-detect)"),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output JSON"),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", help="Show skipped files"),
    ] = False,
) -> None:
    """Verify no secrets in repo, reports, logs, or SQLite (api keys/tokens/passwords)."""
    from .secrets_scan import format_report, scan_project

    _resolved = _resolve_data_dir(data_dir)
    _root = Path(project_root).expanduser().resolve() if project_root else None
    if _root is None:
        # Auto-detect repo root: walk up from CWD and this file's parents
        for cand in [Path.cwd(), Path(__file__).resolve().parents[2], Path(__file__).resolve().parents[3]]:
            if (cand / ".git").exists() or ((cand / "pyproject.toml").exists() and (cand / "src").exists()):
                _root = cand
                break
        if _root is None:
            _root = Path.cwd()
    result = scan_project(project_root=_root, data_dir=_resolved)
    if json_output:
        payload = {
            "ok": result.ok,
            "findings": [{"source": f.source, "pattern": f.pattern, "snippet": f.snippet} for f in result.findings],
            "scanned_files": result.scanned_files,
            "scanned_db_tables": result.scanned_db_tables,
            "skipped": result.skipped[:20],
        }
        console.print_json(json.dumps(payload))
    else:
        console.print(format_report(result, verbose=verbose))
    if not result.ok:
        raise typer.Exit(1)


@app.command()
def kill(
    reason: Annotated[
        Optional[str],
        typer.Option("--reason", help="Stop reason"),
    ] = "CLI kill",
    data_dir: Annotated[
        Optional[str],
        typer.Option("--data-dir", help="Data directory"),
    ] = None,
) -> None:
    """Emergency stop — LLM-independent: cancel task, release input, terminate agent processes."""
    _resolved = _resolve_data_dir(data_dir)
    config = IdleCuaConfig(data_dir=_resolved)
    idle = IdleCua(config=config)
    idle.request_emergency_stop(reason or "CLI kill")
    console.print(f"[red]Emergency stop requested:[/red] {reason}")
    # Also try to mark active task as stopped
    st = idle.get_status()
    active = st.get("active_task")
    if active:
        tid = active.get("id")
        # Directly update state to stopped via memory (emergency path)
        try:
            idle.memory.update_task_state(tid, "stopped")
            idle.memory.record_error(__import__("uuid").uuid4().hex, tid, f"emergency stop: {reason}")
            idle.memory.kv_set("last_stop_reason", reason or "CLI kill")
        except Exception:
            pass
        console.print(f"[dim]Task {tid[:8]} marked stopped[/dim]")
    console.print("[green]Input released, agent processes terminated (if any). Reason saved.[/green]")


# Alias `stop` to `kill` for ergonomics
@app.command(name="stop")
def stop_cmd(
    reason: Annotated[
        Optional[str],
        typer.Option("--reason", help="Stop reason"),
    ] = "CLI stop",
    data_dir: Annotated[
        Optional[str],
        typer.Option("--data-dir", help="Data directory"),
    ] = None,
) -> None:
    """Emergency stop (alias for kill) — LLM-independent."""
    kill(reason=reason, data_dir=data_dir)


@app.command()
def start(
    task: Annotated[
        Optional[str],
        typer.Argument(help="Optional task description to run when idle"),
    ] = None,
    data_dir: Annotated[
        Optional[str],
        typer.Option("--data-dir", help="Data directory"),
    ] = None,
    watch: Annotated[
        bool,
        typer.Option("--watch", help="Poll for idle and auto-start sessions in a loop (Watch loop mode)"),
    ] = False,
    once: Annotated[
        bool,
        typer.Option("--once", help="With --watch, run at most one session then exit"),
    ] = False,
    poll_interval: Annotated[
        float,
        typer.Option("--poll-interval", help="Seconds between idle polls (HID check)"),
    ] = 5.0,
    idle_threshold: Annotated[
        Optional[int],
        typer.Option("--idle-threshold", help="Idle seconds required (overrides config)"),
    ] = None,
    timeout: Annotated[
        Optional[float],
        typer.Option("--timeout", help="Max seconds to wait for idle (0 = forever)"),
    ] = None,
    real_idle: Annotated[
        bool,
        typer.Option("--real-idle", help="Force Quartz HID detector even without env"),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON for reports/status instead of rich tables"),
    ] = False,
) -> None:
    """Start idle watching — waits for hardware idle (HID, synthetic never masks), then runs a bounded session.

    End-to-end: idle auto-start → plan → policy → driver execution → SQLite history → daily Markdown report → graceful stop on return/limits/emergency stop.

    - Without --watch: checks gates once; if idle runs the task immediately, else waits up to --timeout (or exits if no task).
    - With --watch: polls HID every --poll-interval, auto-starts after idle period, handles stop-on-return (paused_by_user), limits, and emergency stop (Ctrl-C / kill). Auto-resume only at next idle.
    """
    import time

    _resolved = _resolve_data_dir(data_dir)
    config = IdleCuaConfig(data_dir=_resolved)
    if idle_threshold is not None:
        config.idle_threshold_seconds = int(idle_threshold)
    if real_idle:
        config.use_real_idle_detector = True

    # Resolve detector: real HID when requested/available, else fake (tests / safe default)
    detector = None
    if config.use_real_idle_detector:
        try:
            from .idle import QuartzIdleDetector

            detector = QuartzIdleDetector()
            console.print("[dim]Using Quartz HID idle detector (synthetic never masks return)[/dim]")
        except Exception as e:
            console.print(f"[yellow]Real idle detector unavailable ({e}) — falling back to FakeIdleDetector[/yellow]")
            from .idle import FakeIdleDetector

            detector = FakeIdleDetector(idle_seconds=1000, locked=False)
    else:
        from .idle import FakeIdleDetector

        # In CLI we default to fake with high idle so --watch can be demonstrated without real idle;
        # real machine runs with IDLECUA_USE_REAL_IDLE=1 or --real-idle and true HID values.
        detector = None  # let IdleCua create its default fake (1000s idle, unlocked)

    # Resolve task description: explicit TASK > profile-derived default
    effective_task = task
    if not effective_task and (watch or timeout):
        # Derive from profile interests/projects when no explicit task
        try:
            from .profile.store import load_profile

            p = load_profile(_resolved / "profile.json")
            if p is not None and bool(getattr(p, "confirmed", False)):
                interests = getattr(getattr(p, "user_characteristics", None), "interests", []) or []
                projects = getattr(getattr(p, "user_characteristics", None), "projects", []) or []
                hint = (interests[:1] or projects[:1] or ["recent AI papers on agents"]) [0]
                effective_task = f"research {hint} and save links with short notes"
                console.print(f"[dim]No TASK given — derived from profile: {effective_task!r}[/dim]")
            else:
                effective_task = "research recent AI papers on agents and save links with short notes"
                console.print(f"[dim]No TASK given and no confirmed profile — using default: {effective_task!r}[/dim]")
        except Exception:
            effective_task = "research recent AI papers on agents and save links with short notes"

    idle = IdleCua(config=config, idle_detector=detector) if detector is not None else IdleCua(config=config)
    # Ensure executor uses same detector
    idle.idle_detector = detector if detector is not None else idle.idle_detector

    # Install LLM-independent emergency stop handlers (SIGINT/SIGTERM)
    try:
        from .executor import install_signal_handlers

        install_signal_handlers()
    except Exception:
        pass

    # Profile gate (hard)
    _check_profile_gate(data_dir)

    # If watch mode: autonomous loop (wait-for-idle → session → wait again)
    if watch or (effective_task and timeout is not None):
        thr = int(idle_threshold) if idle_threshold is not None else idle.get_effective_idle_threshold()
        # ADR-0004: exactly one scheduler per data dir enforced by lock — also for CLI Watch loop
        _watch_lock_info = None
        try:
            from .server.lock import acquire_lock as _acquire_wlock, release_lock as _release_wlock

            _watch_lock_info = _acquire_wlock(_resolved)
            console.print(f"[dim]Scheduler lock acquired for Watch loop: pid {_watch_lock_info['pid']} data_dir={_resolved}[/dim]")
        except RuntimeError as _le:
            if "already running" in str(_le):
                console.print(f"[red]Failed to start Watch loop: {_le}[/red]")
                console.print(f"[dim]Data dir: {_resolved}[/dim]")
                console.print("[dim]If the process crashed, the stale lock was cleaned — retry. If still running, stop the other process first (idle-cua kill or kill PID).[/dim]")
                raise typer.Exit(1)
            raise
        console.print(f"[bold]Idle watch:[/bold] waiting for idle ≥ {thr}s (poll {poll_interval}s) — synthetic never masks HID — Ctrl-C to stop")
        console.print(f"Task: {effective_task}")
        console.print(f"Screen locked check: hard gate — agent will not run while locked")
        # Build scheduler wired to this app's memory/detector/config
        from .scheduler import IdleScheduler

        scheduler = IdleScheduler(config=config, idle_detector=idle.idle_detector, memory=idle.memory)

        def _on_event(kind: str, payload) -> None:
            if kind == "waiting_for_idle":
                console.print(f"[dim]Waiting for idle ≥ {payload['threshold']}s...[/dim]")
            elif kind == "idle_tick":
                gate = payload["gate"]
                # Throttle verbose
                if payload["tick"] % max(1, int(5 / max(0.5, poll_interval))) == 0:
                    console.print(f"[dim]idle poll {payload['tick']}: {gate.reason} ({gate.gate})[/dim]")
            elif kind == "idle_detected":
                console.print("[green]Idle detected — all gates pass — starting session (plan → policy → driver → history → report)[/green]")
            elif kind == "schedule_blocked":
                console.print(f"[yellow]Schedule blocked: {payload}[/yellow]")
            elif kind == "limits_reached":
                console.print(f"[yellow]Limits reached: {payload} — stopping watch[/yellow]")
            elif kind == "session_completed":
                r = payload
                state = r.state.value if hasattr(r.state, "value") else str(r.state)
                console.print(f"[green]Session completed:[/green] {state} — actions {r.actions_executed} — report {r.report_path}")
                if r.skipped_repeats:
                    console.print(f"[dim]Skipped repeats: {len(r.skipped_repeats)}[/dim]")
                if r.report_markdown and not json_output:
                    try:
                        # Daily report preview
                        console.print(Markdown(r.report_markdown[:3000]))
                    except Exception:
                        console.print(r.report_markdown[:1500])
            elif kind == "paused_by_user":
                console.print("[yellow]Paused by user (hardware return) — input halted, task paused_by_user, will auto-resume at next idle[/yellow]")
            elif kind == "session_error":
                console.print(f"[red]Session error: {payload}[/red]")
            elif kind == "emergency_stop":
                console.print("[red]Emergency stop requested — exiting watch loop[/red]")
            elif kind == "wait_timeout":
                console.print("[yellow]Wait for idle timed out[/yellow]")

        # Determine timeout_per_wait
        tmo = None if timeout is None or float(timeout) == 0 else float(timeout)
        # If once implied by non-watch single session, set once=True
        effective_once = once or (not watch and effective_task is not None)
        try:
            results = scheduler.run_loop(
                effective_task,
                poll_interval=poll_interval,
                idle_threshold_override=thr,
                max_sessions=1 if effective_once else None,
                once=effective_once,
                timeout_per_wait=tmo,
                on_event=_on_event,
            )
        except KeyboardInterrupt:
            console.print("[yellow]Interrupted (SIGINT) — watch stopped[/yellow]")
            raise typer.Exit(0)
        except RuntimeError as e:
            console.print(f"[red]Watch failed: {e}[/red]")
            raise typer.Exit(1)
        finally:
            try:
                from .server.lock import release_lock as _release_wlock2

                _release_wlock2(_resolved)
                console.print("[dim]Scheduler lock released for Watch loop[/dim]")
            except Exception:
                pass

        if not results:
            console.print("[yellow]No session started (idle not reached, timeout, or gate blocked). See `idle-cua status` and `idle-cua doctor`.[/yellow]")
            if effective_once:
                raise typer.Exit(1)
            return
        # Emit JSON if requested
        if json_output and results:
            last = results[-1]
            payload = {
                "sessions": len(results),
                "last_task_id": last.task_id,
                "last_state": last.state.value if hasattr(last.state, "value") else str(last.state),
                "last_report": str(last.report_path) if last.report_path else None,
                "last_report_markdown": last.report_markdown[:8000],
            }
            console.print_json(json.dumps(payload))
        return

    # Non-watch path (legacy single check): show status and run if idle, else wait briefly if task given
    st = idle.get_status()
    _thr_override = int(idle_threshold) if idle_threshold is not None else None
    _eff_thr = idle.get_effective_idle_threshold() if _thr_override is None else _thr_override
    console.print(f"Agent state: {st.get('agent_state')}")
    console.print(f"Idle: {st.get('idle_time')} (threshold {_eff_thr}s)")
    console.print(f"Screen locked: {st.get('screen_locked')}")
    if st.get("screen_locked"):
        console.print("[yellow]Screen is locked — agent will not run until unlocked[/yellow]")
        raise typer.Exit(1)
    # Use effective_task fallback if task is None but derived exists
    run_task = effective_task or task
    # If not idle but task given and timeout allows waiting, wait once
    if run_task and float(st.get("idle_seconds", 0)) < _eff_thr:
        if timeout is not None and float(timeout) > 0:
            console.print(f"[yellow]Not idle yet — waiting up to {timeout}s for idle ≥ {_eff_thr}s[/yellow]")
            ok = idle.wait_for_idle(poll_interval=poll_interval, timeout=float(timeout), threshold_override=_thr_override)
            if not ok:
                console.print(f"[yellow]Still not idle after {timeout}s — not starting[/yellow]")
                raise typer.Exit(1)
        else:
            console.print(f"[yellow]Not idle yet — need {_eff_thr}s, have {st.get('idle_seconds'):.1f}s[/yellow]")
            if not run_task:
                console.print("[dim]Use `idle-cua run-once \"task\"` to run immediately, or `idle-cua start --watch` to auto-start when idle.[/dim]")
                return
            # For backward compat with tests: if FakeIdleDetector default is 1000s, we are idle; if not, we still run when task explicitly given?
            # Respect threshold strictly when no timeout: refuse unless idle
            if float(st.get("idle_seconds", 0)) < _eff_thr:
                console.print("[yellow]Refusing to run — not idle (use --timeout or --watch to wait, or `run-once` to bypass idle gate)[/yellow]")
                raise typer.Exit(1)
    if run_task:
        console.print(f"[green]Running task:[/green] {run_task}")
        # Use idle-gated session (waits not needed, we already checked)
        result = idle.run_idle_session(run_task, poll_interval=poll_interval, wait=False)
        console.print(f"[green]Completed:[/green] {result.state.value if hasattr(result.state,'value') else result.state}")
        console.print(f"Report: {result.report_path}")
        # Show daily report path
        daily = _resolved / "reports" / f"{__import__('datetime').datetime.now(__import__('datetime').timezone.utc).date().isoformat()}.md"
        if daily.exists():
            console.print(f"Daily report: {daily}")
    else:
        console.print("[dim]Idle watch would run here in Watch loop mode (MVP: use `idle-cua start --watch \"task\"` or `idle-cua run-once`).[/dim]")


@app.command()
def pause(
    data_dir: Annotated[
        Optional[str],
        typer.Option("--data-dir", help="Data directory"),
    ] = None,
) -> None:
    """Pause active task (user-return simulation)."""
    _resolved = _resolve_data_dir(data_dir)
    config = IdleCuaConfig(data_dir=_resolved)
    idle = IdleCua(config=config)
    st = idle.get_status()
    active = st.get("active_task")
    if not active:
        console.print("[yellow]No active task to pause[/yellow]")
        raise typer.Exit(1)
    tid = active["id"]
    # Simulate user return: release input and transition to paused_by_user
    try:
        if hasattr(idle.computer, "release_all_inputs"):
            idle.computer.release_all_inputs()
    except Exception:
        pass
    idle.memory.update_task_state(tid, "paused_by_user")
    idle.memory.record_error(__import__("uuid").uuid4().hex, tid, "paused_by_user: manual pause")
    console.print(f"[yellow]Task {tid[:8]} paused (paused_by_user)[/yellow]")


@app.command()
def resume(
    data_dir: Annotated[
        Optional[str],
        typer.Option("--data-dir", help="Data directory"),
    ] = None,
) -> None:
    """Resume paused task at next idle period."""
    _resolved = _resolve_data_dir(data_dir)
    config = IdleCuaConfig(data_dir=_resolved)
    idle = IdleCua(config=config)
    st = idle.get_status()
    active = st.get("active_task")
    if not active:
        console.print("[yellow]No task to resume[/yellow]")
        raise typer.Exit(1)
    if active.get("state") != "paused_by_user":
        console.print(f"[yellow]Task {active.get('id','')[:8]} is not paused (state={active.get('state')})[/yellow]")
        raise typer.Exit(1)
    # Check readiness via the Application API (T1: decision inside, CLI only renders).
    ok, reason = idle.can_start()
    if not ok:
        console.print(f"[yellow]Cannot resume yet: {reason}[/yellow]")
        raise typer.Exit(1)
    idle.memory.update_task_state(active["id"], "waiting_for_idle")
    console.print(f"[green]Task {active['id'][:8]} resumed → waiting_for_idle[/green]")

@app.command()
def serve(
    data_dir: Annotated[
        Optional[str],
        typer.Option("--data-dir", help="Data directory (default: ~/.idlecua or $IDLECUA_DATA_DIR)"),
    ] = None,
    port: Annotated[
        int,
        typer.Option("--port", help="Port to bind (default: 8000)"),
    ] = 8000,
    no_open: Annotated[
        bool,
        typer.Option("--no-open", help="Do not open browser automatically"),
    ] = False,
) -> None:
    """Start the Local UI + HTTP API server (binds to 127.0.0.1 only)."""
    import webbrowser
    import signal

    _resolved = _resolve_data_dir(data_dir)
    # Auto-init idempotently like `init`
    _resolved.mkdir(parents=True, exist_ok=True)
    config = IdleCuaConfig(data_dir=_resolved)
    idle = IdleCua(config=config)
    created = idle.init_data_dir()
    if not config.config_path.exists():
        config.save()
    # Also ensure memory db init (already via IdleCua)
    _ = idle.memory

    # Validate port
    if port <= 0 or port > 65535:
        console.print(f"[red]Invalid --port {port}: must be 1..65535[/red]")
        raise typer.Exit(1)

    # v1 only supports loopback; explicit validation path for spec compliance and tests
    host = "127.0.0.1"
    validate_bind_host(host)

    # Scheduler lock — one per data dir, crash-safe via PID liveness
    try:
        from .server.lock import acquire_lock, release_lock

        lock_info = acquire_lock(_resolved)
        console.print(f"[dim]Scheduler lock acquired: pid {lock_info['pid']} data_dir={_resolved}[/dim]")
    except RuntimeError as e:
        if "already running" in str(e):
            console.print(f"[red]Failed to start serve: {e}[/red]")
            console.print(f"[dim]Data dir: {_resolved}[/dim]")
            console.print("[dim]If the process crashed, the stale lock was cleaned — retry. If still running, stop the other process first (idle-cua kill or kill PID).[/dim]")
            raise typer.Exit(1)
        raise

    # Also handle stale lock cleanup on signals
    def _release_on_exit(signum=None, frame=None):
        try:
            release_lock(_resolved)
        except Exception:
            pass
        # Re-raise for uvicorn? Just exit gracefully
        if signum is not None:
            console.print(f"\n[yellow]Received signal {signum} — releasing lock and exiting[/yellow]")
            raise typer.Exit(0)

    try:
        signal.signal(signal.SIGINT, _release_on_exit)
        signal.signal(signal.SIGTERM, _release_on_exit)
    except Exception:
        pass

    # Create app
    try:
        from .server.app import create_app
    except Exception as e:
        console.print(f"[red]Failed to create server app: {e}[/red]")
        try:
            release_lock(_resolved)
        except Exception:
            pass
        raise typer.Exit(1)

    fastapi_app = create_app(data_dir=_resolved)

    url = f"http://{host}:{port}/"
    console.print(f"[green]IdleCUA serve starting[/green] on {url} (data_dir={_resolved})")
    console.print(f"[dim]API docs: {url}api/docs  •  OpenAPI: {url}api/openapi.json[/dim]")
    if not no_open:
        try:
            # Delay slightly so server is listening before opening?
            # Use webbrowser open (best effort)
            webbrowser.open(url)
            console.print(f"[dim]Opened browser at {url} (use --no-open to disable)[/dim]")
        except Exception as e:
            console.print(f"[yellow]Could not open browser: {e} — visit {url} manually[/yellow]")
    else:
        console.print(f"[dim]Browser auto-open disabled (--no-open) — visit {url}[/dim]")

    # Run uvicorn — thin caller, no business logic
    try:
        import uvicorn

        # uvicorn handles its own signal handling; ensure lock released after
        uvicorn.run(fastapi_app, host=host, port=port, log_level="info")
    except Exception as e:
        console.print(f"[red]Server failed: {e}[/red]")
        raise typer.Exit(1)
    finally:
        try:
            release_lock(_resolved)
            console.print("[dim]Scheduler lock released[/dim]")
        except Exception:
            pass


# For `python -m idlecua` convenience
def main() -> None:
    app()

if __name__ == "__main__":
    main()
