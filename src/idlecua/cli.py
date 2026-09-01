from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

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
    p = idle.dry_run(task)

    if json_output:
        console.print_json(json.dumps(idle.plan_to_dict(p)))
        return

    table = Table(title="IdleCUA Plan (dry-run — no actions executed)", show_header=True)
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
) -> None:
    """Run a task once. In the walking skeleton only --dry-run is supported."""
    if not task or not task.strip():
        console.print("[red]Task description must be non-empty[/red]")
        raise typer.Exit(code=2)
    # Hard gate: no autonomous action while profile is unconfirmed (except dry-run)
    if not dry_run:
        # Use same data_dir resolution as profile
        from pathlib import Path as _Path

        import os as _os

        from .profile.store import load_profile as _load_profile
        from .profile.validate import validate_profile as _validate_profile

        # resolve data_dir like profile does
        _resolved = _Path(data_dir).expanduser() if data_dir is not None else None
        if _resolved is None:
            _env = _os.environ.get("IDLECUA_DATA_DIR") or _os.environ.get("IDLE_CUA_DATA_DIR")
            if _env:
                _resolved = _Path(_env).expanduser()
            else:
                _resolved = IdleCuaConfig().data_dir
        _ppath = _resolved / "profile.json"
        _profile = _load_profile(_ppath)
        if _profile is None:
            console.print(f"[red]Refused: No profile found at {_ppath}. Run `idle-cua profile interview` and confirm.[/red]")
            console.print("[dim]Hint: run `idle-cua profile interview` and confirm, or `idle-cua profile show` / `validate` to fix.[/dim]")
            raise typer.Exit(1)
        if not _profile.confirmed:
            console.print(f"[red]Refused: Profile at {_ppath} is unconfirmed. Complete `idle-cua profile interview` and confirm, or `idle-cua profile show` to inspect. Autonomous runs are blocked until the profile is confirmed.[/red]")
            raise typer.Exit(1)
        _errs = _validate_profile(_profile)
        if _errs:
            console.print(f"[red]Refused: Profile at {_ppath} is confirmed but invalid: {'; '.join(_errs)}. Run `idle-cua profile validate`.[/red]")
            raise typer.Exit(1)
        console.print("[red]Non-dry-run execution is not implemented in the walking skeleton. Use --dry-run.[/red]")
        raise typer.Exit(code=2)
    # Reuse plan logic (dry-run)
    config = _config_for_cli(data_dir)
    idle = IdleCua(config=config)
    p = idle.dry_run(task)

    if json_output:
        console.print_json(json.dumps(idle.plan_to_dict(p)))
        return

    table = Table(title="IdleCUA run-once --dry-run (no actions executed)", show_header=True)
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
    # also validate profile if exists
    # resolve data_dir
    import os as _os
    from pathlib import Path as _Path

    _resolved = _Path(data_dir).expanduser() if data_dir is not None else None
    if _resolved is None:
        _env = _os.environ.get("IDLECUA_DATA_DIR") or _os.environ.get("IDLE_CUA_DATA_DIR")
        if _env:
            _resolved = _Path(_env).expanduser()
        else:
            _resolved = IdleCuaConfig().data_dir
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


@app.command()
def status(
    data_dir: Annotated[
        Optional[str],
        typer.Option("--data-dir", help="Data directory"),
    ] = None,
) -> None:
    """Show status (stub)."""
    from .profile.store import load_profile as _load_profile

    # resolve data_dir
    import os as _os
    from pathlib import Path as _Path

    _resolved = _Path(data_dir).expanduser() if data_dir is not None else None
    if _resolved is None:
        _env = _os.environ.get("IDLECUA_DATA_DIR") or _os.environ.get("IDLE_CUA_DATA_DIR")
        if _env:
            _resolved = _Path(_env).expanduser()
        else:
            _resolved = IdleCuaConfig().data_dir
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
    console.print("State: disabled (stub)")

# For `python -m idlecua` convenience
def main() -> None:
    app()

if __name__ == "__main__":
    main()
