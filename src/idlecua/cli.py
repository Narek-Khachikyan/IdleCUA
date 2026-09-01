from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from .app import IdleCua
from .cli_models import models_app
from .config import IdleCuaConfig

app = typer.Typer(
    name="idle-cua",
    help="IdleCUA — policy-gated idle-time computer-use agent.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

app.add_typer(models_app, name="models")

def _config_for_cli(data_dir: Optional[str]) -> IdleCuaConfig:
    if data_dir is not None:
        return IdleCuaConfig(data_dir=Path(data_dir).expanduser())
    return IdleCuaConfig()

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
    _print_plan(idle, p, "IdleCUA Plan (dry-run — no actions executed)", json_output)

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
    if not dry_run:
        console.print("[red]Non-dry-run execution is not implemented in the walking skeleton. Use --dry-run.[/red]")
        raise typer.Exit(code=2)
    config = _config_for_cli(data_dir)
    idle = IdleCua(config=config)
    p = idle.dry_run(task)
    _print_plan(idle, p, "IdleCUA run-once --dry-run (no actions executed)", json_output)

# For `python -m idlecua` convenience
def main() -> None:
    app()

if __name__ == "__main__":
    main()
