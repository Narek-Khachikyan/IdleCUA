from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.prompt import Prompt, Confirm

from .config import IdleCuaConfig, resolve_data_dir
from .profile.interview import run_interview, save_confirmed_profile, QUESTIONS
from .profile.store import load_profile, save_profile
from .profile.validate import validate_profile
from .profile.render import render_human_readable
from .profile.permissions import check_permissions, permissions_report_text
from .profile.models import Profile

profile_app = typer.Typer(help="Profile interview and management", no_args_is_help=True)
console = Console()


def _config_from_context(ctx: typer.Context | None = None) -> IdleCuaConfig:
    # Allow data-dir override via env or ctx obj
    data_dir = None
    if ctx is not None and ctx.obj is not None and isinstance(ctx.obj, dict):
        data_dir = ctx.obj.get("data_dir")
    return IdleCuaConfig(data_dir=data_dir)


@profile_app.command("interview")
def interview(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", help="Non-interactive: answer defaults and auto-confirm (for testing)"),
    data_dir: str | None = typer.Option(None, "--data-dir", help="Override data directory"),
) -> None:
    """Scripted questionnaire (works with no LLM). Saves only after explicit confirmation."""
    cfg = IdleCuaConfig(data_dir=data_dir or (ctx.obj.get("data_dir") if ctx.obj else None))
    # Interview
    if yes:
        # Non-interactive mode for tests: simulate hitting enter (use defaults as assumptions), auto-confirm
        def input_func(q):  # type: ignore
            return ""  # empty -> default via _parse_value, counted as assumption

        def confirm_func(_):  # type: ignore
            return True

        profile, facts, assumptions = run_interview(console=console, input_func=input_func, confirm_func=confirm_func)
    else:
        profile, facts, assumptions = run_interview(console=console)

    if save_confirmed_profile(profile, cfg.profile_path, console=console):
        # Also print human-readable rendering
        console.print("\n" + render_human_readable(profile))
        raise typer.Exit(0)
    else:
        raise typer.Exit(1)


@profile_app.command("show")
def show(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
    data_dir: str | None = typer.Option(None, "--data-dir", help="Override data directory"),
) -> None:
    """Show profile — human-readable rendering of the same machine-readable file."""
    cfg = IdleCuaConfig(data_dir=data_dir or (ctx.obj.get("data_dir") if ctx.obj else None))
    profile = load_profile(cfg.profile_path)
    if profile is None:
        console.print(f"[red]No profile found at {cfg.profile_path}. Run `idle-cua profile interview` first.[/red]")
        raise typer.Exit(1)
    if json_output:
        console.print_json(json.dumps(profile.to_dict(), indent=2))
    else:
        console.print(render_human_readable(profile))


@profile_app.command("edit")
def edit(
    ctx: typer.Context,
    field: list[str] = typer.Option(None, "--field", help="Field to set as dotted.path=value (repeatable)"),
    editor: bool = typer.Option(False, "--editor", help="Open in $EDITOR"),
    data_dir: str | None = typer.Option(None, "--data-dir", help="Override data directory"),
) -> None:
    """Edit profile. Without args, interactively edit starting from stored values."""
    cfg = IdleCuaConfig(data_dir=data_dir or (ctx.obj.get("data_dir") if ctx.obj else None))
    profile = load_profile(cfg.profile_path)
    if profile is None:
        console.print(f"[red]No profile found at {cfg.profile_path}. Run `idle-cua profile interview` first.[/red]")
        raise typer.Exit(1)

    if editor:
        # Open JSON in editor
        import tempfile

        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w", encoding="utf-8")
        tmp.write(json.dumps(profile.to_dict(), indent=2, ensure_ascii=False))
        tmp.close()
        ed = os.environ.get("EDITOR", "nano")
        subprocess.run([ed, tmp.name])
        try:
            data = json.loads(Path(tmp.name).read_text(encoding="utf-8"))
            new_profile = Profile.from_dict(data)
            # Preserve confirmed? Allow editing; if they edit, keep confirmed status but re-validate
            new_profile.touch()
            save_profile(new_profile, cfg.profile_path)
            console.print(f"[green]Profile updated via editor -> {cfg.profile_path}[/green]")
        except Exception as e:
            console.print(f"[red]Failed to save edited profile: {e}[/red]")
            raise typer.Exit(1)
        finally:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass
        return

    if field:
        # Parse dotted.path=value
        data = profile.to_dict()
        for f in field:
            if "=" not in f:
                console.print(f"[red]Invalid --field '{f}' expected dotted.path=value[/red]")
                raise typer.Exit(1)
            dotted, value = f.split("=", 1)
            # Navigate and set with type inference
            parts = dotted.split(".")
            cur = data
            for p in parts[:-1]:
                if p not in cur:
                    console.print(f"[red]Unknown field path '{dotted}'[/red]")
                    raise typer.Exit(1)
                cur = cur[p]
            leaf = parts[-1]
            if leaf not in cur:
                console.print(f"[red]Unknown field '{dotted}'[/red]")
                raise typer.Exit(1)
            old = cur[leaf]
            # Coerce type based on old
            if isinstance(old, list):
                # comma-separated -> list
                new_val = [s.strip() for s in value.split(",") if s.strip()] if value.strip() else []
            elif isinstance(old, int):
                try:
                    new_val = int(value)
                except ValueError:
                    console.print(f"[red]Field {dotted} expects integer, got '{value}'[/red]")
                    raise typer.Exit(1)
            elif isinstance(old, bool):
                new_val = value.lower() in ("true", "1", "yes", "y")
            else:
                new_val = value
            cur[leaf] = new_val
        try:
            new_profile = Profile.from_dict(data)
            new_profile.touch()
            save_profile(new_profile, cfg.profile_path)
            console.print(f"[green]Profile updated ({len(field)} field(s)) -> {cfg.profile_path}[/green]")
        except Exception as e:
            console.print(f"[red]Validation failed: {e}[/red]")
            raise typer.Exit(1)
        return

    # Interactive: pre-fill with existing values
    console.print("[bold]Edit profile (leave empty to keep current value)[/bold]\n")
    data = profile.to_dict()
    for q in QUESTIONS:
        # get current value
        parts = q.key.split(".")
        cur = data
        for p in parts[:-1]:
            cur = cur[p]
        cur_val = cur[parts[-1]]
        if isinstance(cur_val, list):
            cur_str = ", ".join(cur_val)
        else:
            cur_str = str(cur_val)
        # Prompt with current as default
        prompt_text = f"{q.prompt}"
        help_suffix = f" [dim]({q.help_text})[/dim]" if q.help_text else ""
        # Use current value as default display
        raw = Prompt.ask(f"{prompt_text}{help_suffix}", default=cur_str, console=console, show_default=True)
        if raw == cur_str:
            # unchanged -> treat as keeping fact (no assumption)
            continue
        # parse
        if q.is_list:
            parsed = [s.strip() for s in raw.split(",") if s.strip()] if raw.strip() else []
        elif q.is_int:
            try:
                parsed = int(raw) if raw.strip() else cur_val  # type: ignore
            except ValueError:
                console.print(f"[yellow]Invalid integer '{raw}', keeping {cur_val}[/yellow]")
                continue
        else:
            parsed = raw
        cur[parts[-1]] = parsed

    # Confirm before saving edits
    console.print("\n[bold]Updated profile preview:[/bold]")
    try:
        new_profile = Profile.from_dict(data)
    except Exception as e:
        console.print(f"[red]Invalid profile data: {e}[/red]")
        raise typer.Exit(1)
    console.print(render_human_readable(new_profile))
    if not Confirm.ask("Save edited profile?", console=console, default=False):
        console.print("[yellow]Edit cancelled — not saved.[/yellow]")
        raise typer.Exit(1)
    new_profile.touch()
    save_profile(new_profile, cfg.profile_path)
    console.print(f"[green]Profile saved to {cfg.profile_path}[/green]")


@profile_app.command("validate")
def validate(
    ctx: typer.Context,
    data_dir: str | None = typer.Option(None, "--data-dir", help="Override data directory"),
) -> None:
    """Validate allowlist, limits, schedule sanity."""
    cfg = IdleCuaConfig(data_dir=data_dir or (ctx.obj.get("data_dir") if ctx.obj else None))
    profile = load_profile(cfg.profile_path)
    if profile is None:
        console.print(f"[red]No profile found at {cfg.profile_path}[/red]")
        raise typer.Exit(1)
    errors = validate_profile(profile)
    # Also include permission checks as warnings, not hard errors? Issue says validate rejects invalid allowlist or limits
    if errors:
        console.print("[red]Profile validation failed:[/red]")
        for e in errors:
            console.print(f"  - {e}")
        raise typer.Exit(1)
    console.print("[green]Profile is valid.[/green]")
    console.print(f"Confirmed: {profile.confirmed}")
    console.print(f"Allowlist: {', '.join(profile.autonomy_boundaries.allowed_sites)}")
    console.print(f"Session: {profile.autonomy_boundaries.session_duration_minutes} min, {profile.computer_usage.idle_threshold_minutes} min idle threshold")
    console.print(f"Allowed hours: {profile.autonomy_boundaries.allowed_hours}")


@profile_app.command("check-permissions")
def check_permissions_cmd(
    ctx: typer.Context,
    data_dir: str | None = typer.Option(None, "--data-dir", help="Override data directory"),
) -> None:
    """Report missing macOS grants (Accessibility, Screen Recording) with remediation."""
    statuses = check_permissions()
    text = permissions_report_text(statuses)
    console.print(text)
    # Exit code 1 if any is definitively missing (granted == False)
    if any(s.granted is False for s in statuses):
        raise typer.Exit(1)
    # If unknown, we still warn but exit 0; user should verify manually.
    # However to make acceptance visible, print reminder
    if any(s.granted is None for s in statuses):
        console.print("[yellow]Could not definitively verify permissions — please verify manually via System Settings.[/yellow]")
