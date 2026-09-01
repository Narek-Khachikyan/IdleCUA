from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

from .config import IdleCuaConfig
from .planner import stub_plan, render_plan
from .application import Application, ProfileNotConfirmedError
from .profile.permissions import check_permissions, permissions_report_text
from .cli_profile import profile_app

app = typer.Typer(help="IdleCUA - policy-gated idle-time computer-use agent", no_args_is_help=False)
app.add_typer(profile_app, name="profile")
console = Console()


def _resolve_config(data_dir: str | None) -> IdleCuaConfig:
    return IdleCuaConfig(data_dir=data_dir)


@app.callback()
def main_callback(
    ctx: typer.Context,
    data_dir: Annotated[Optional[str], typer.Option("--data-dir", help="Override data directory")] = None,
) -> None:
    ctx.obj = {"data_dir": data_dir}


@app.command()
def init(
    ctx: typer.Context,
    data_dir: str | None = typer.Option(None, "--data-dir", help="Override data directory"),
) -> None:
    """Create the data directory and default config."""
    cfg = IdleCuaConfig(data_dir=data_dir or (ctx.obj.get("data_dir") if ctx.obj else None))
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    # Write default config.json if not exists
    if not cfg.config_path.exists():
        default_cfg = {
            "version": 1,
            "data_dir": str(cfg.data_dir),
            "readonly": True,
            "idle_threshold_minutes": 10,
            "session_max_minutes": 45,
            "session_max_actions": 200,
            "daily_llm_call_limit": 150,
            "allowlist": cfg.allowlist,
        }
        cfg.config_path.write_text(json.dumps(default_cfg, indent=2) + "\n", encoding="utf-8")
        console.print(f"[green]Created config at {cfg.config_path}[/green]")
    else:
        console.print(f"[dim]Config already exists at {cfg.config_path}[/dim]")
    console.print(f"[green]Data directory ready at {cfg.data_dir}[/green]")


@app.command()
def plan(
    ctx: typer.Context,
    task: str = typer.Argument(..., help="Task description"),
    data_dir: str | None = typer.Option(None, "--data-dir", help="Override data directory"),
) -> None:
    """Print a typed, bounded plan (goal, target, expected actions, max duration, max actions, risk level) and execute nothing."""
    cfg = IdleCuaConfig(data_dir=data_dir or (ctx.obj.get("data_dir") if ctx.obj else None))
    # Ensure data dir exists? Not required for plan, but we check profile gate? For plan, we allow even unconfirmed (dry-run audit).
    p = stub_plan(task)
    console.print(render_plan(p))
    # Explicitly state no actions executed
    console.print("\n[dim]No actions executed (plan only).[/dim]")


@app.command(name="run-once")
def run_once(
    ctx: typer.Context,
    task: str = typer.Argument(..., help="Task description"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print plan without performing any action"),
    data_dir: str | None = typer.Option(None, "--data-dir", help="Override data directory"),
) -> None:
    """Run a task once. With --dry-run, prints plan and executes nothing. Otherwise hard-gated by profile confirmation."""
    cfg = IdleCuaConfig(data_dir=data_dir or (ctx.obj.get("data_dir") if ctx.obj else None))

    p = stub_plan(task)
    if dry_run:
        console.print(render_plan(p))
        console.print("\n[dim]Dry-run — no actions executed.[/dim]")
        return

    # Hard gate: refuse autonomous runs while profile unconfirmed
    app_obj = Application(config=cfg)
    try:
        # This will raise if not confirmed/valid
        plan_result = asyncio.run(app_obj.run_task(task, dry_run=False))
        console.print(render_plan(plan_result))
        console.print("\n[green]Task completed (stub execution — no real actions).[/green]")
    except ProfileNotConfirmedError as e:
        console.print(f"[red]Refused: {e}[/red]")
        console.print("[dim]Hint: run `idle-cua profile interview` and confirm, or `idle-cua profile show` / `validate` to fix.[/dim]")
        raise typer.Exit(1)


@app.command()
def status(
    ctx: typer.Context,
    data_dir: str | None = typer.Option(None, "--data-dir", help="Override data directory"),
) -> None:
    """Show status (stub)."""
    cfg = IdleCuaConfig(data_dir=data_dir or (ctx.obj.get("data_dir") if ctx.obj else None))
    app_obj = Application(config=cfg)
    confirmed, msg = app_obj.check_profile_confirmed()
    console.print(f"Data dir: {cfg.data_dir}")
    console.print(f"Profile: {msg}")
    console.print(f"State: {app_obj.state_machine.state}")


@app.command()
def doctor(
    ctx: typer.Context,
    data_dir: str | None = typer.Option(None, "--data-dir", help="Override data directory"),
) -> None:
    """Run permission checks + profile validate for quick diagnostics."""
    cfg = IdleCuaConfig(data_dir=data_dir or (ctx.obj.get("data_dir") if ctx.obj else None))
    console.print(permissions_report_text(check_permissions()))
    # Also validate profile if exists
    from .profile.store import load_profile
    from .profile.validate import validate_profile

    profile = load_profile(cfg.profile_path)
    if profile is None:
        console.print(f"[yellow]No profile at {cfg.profile_path} — run `idle-cua profile interview`[/yellow]")
    else:
        errors = validate_profile(profile)
        if errors:
            console.print("[red]Profile validation errors:[/red]")
            for e in errors:
                console.print(f"  - {e}")
        else:
            console.print("[green]Profile validation: OK[/green]")

