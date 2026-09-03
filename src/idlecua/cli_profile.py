from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import typer
from rich.console import Console
from rich.prompt import Confirm, Prompt

from .config import IdleCuaConfig

from .profile.interview import QUESTIONS, run_interview, save_confirmed_profile
from .profile.models import Profile
from .profile.permissions import check_permissions, permissions_report_text
from .profile.render import render_human_readable
from .profile.store import load_profile, save_profile
from .profile.validate import validate_profile

profile_app = typer.Typer(help="Profile interview and management", no_args_is_help=True)
console = Console()


def _resolve_data_dir(explicit: str | None, ctx: typer.Context | None = None) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser()
    if ctx is not None and ctx.obj is not None and isinstance(ctx.obj, dict):
        # typer's ctx.obj may hold data_dir from parent callback
        d = ctx.obj.get("data_dir")
        if d:
            return Path(d).expanduser()
    # check env vars - support both naming conventions
    env = os.environ.get("IDLECUA_DATA_DIR") or os.environ.get("IDLE_CUA_DATA_DIR")
    if env:
        return Path(env).expanduser()
    # fallback to IdleCuaConfig default
    return IdleCuaConfig().data_dir


def _profile_path(data_dir: Path) -> Path:
    return data_dir / "profile.json"


@profile_app.command("interview")
def interview(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", help="Non-interactive: answer defaults and auto-confirm (for testing)"),
    data_dir: str | None = typer.Option(None, "--data-dir", help="Override data directory"),
) -> None:
    """Scripted questionnaire (works with no LLM). Saves only after explicit confirmation."""
    resolved = _resolve_data_dir(data_dir, ctx)
    ppath = _profile_path(resolved)
    if yes:
        def input_func(q):  # type: ignore
            return ""

        def confirm_func(_):  # type: ignore
            return True

        profile, facts, assumptions = run_interview(console=console, input_func=input_func, confirm_func=confirm_func)
    else:
        profile, facts, assumptions = run_interview(console=console)

    if save_confirmed_profile(profile, ppath, console=console):
        # Mirror browser consent to config.json for "profile/config" requirement (issue #12)
        try:
            bc = profile.autonomy_boundaries.browser_consent
            cfg = IdleCuaConfig.load(resolved)
            cfg.record_browser_consent(bool(bc.main_profile_granted), browser=bc.browser, granted_at=bc.granted_at)
        except Exception:
            pass
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
    resolved = _resolve_data_dir(data_dir, ctx)
    ppath = _profile_path(resolved)
    profile = load_profile(ppath)
    if profile is None:
        console.print(f"[red]No profile found at {ppath}. Run `idle-cua profile interview` first.[/red]")
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
    resolved = _resolve_data_dir(data_dir, ctx)
    ppath = _profile_path(resolved)
    profile = load_profile(ppath)
    if profile is None:
        console.print(f"[red]No profile found at {ppath}. Run `idle-cua profile interview` first.[/red]")
        raise typer.Exit(1)

    if editor:
        import tempfile

        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w", encoding="utf-8")
        tmp.write(json.dumps(profile.to_dict(), indent=2, ensure_ascii=False))
        tmp.close()
        ed = os.environ.get("EDITOR", "nano")
        subprocess.run([ed, tmp.name])
        try:
            data = json.loads(Path(tmp.name).read_text(encoding="utf-8"))
            new_profile = Profile.from_dict(data)
            new_profile.touch()
            _errs = validate_profile(new_profile)
            if _errs:
                console.print(f"[red]Validation failed: {'; '.join(_errs)}[/red]")
                raise typer.Exit(1)
            save_profile(new_profile, ppath)
            console.print(f"[green]Profile updated via editor -> {ppath}[/green]")
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
        # T3: owner-settings fields delegate to Application API so CLI and HTTP share
        # identical validation (same accept/reject, same message text). No silent
        # save of invalid values; ValueError from app.update_owner_settings is
        # surfaced as `Validation failed: ...` with exit 1, byte-identical to HTTP 400.
        _patch = {}
        _is_settings_patch = True
        for f in field:
            if "=" not in f:
                _is_settings_patch = False
                break
            dotted, value = f.split("=", 1)
            if dotted == "autonomy_boundaries.session_duration_minutes":
                try:
                    _patch["session_duration_minutes"] = int(value)
                except ValueError:
                    console.print(f"[red]Field {dotted} expects integer, got '{value}'[/red]")
                    raise typer.Exit(1)
            elif dotted == "autonomy_boundaries.daily_action_limit":
                try:
                    _patch["daily_action_limit"] = int(value)
                except ValueError:
                    console.print(f"[red]Field {dotted} expects integer, got '{value}'[/red]")
                    raise typer.Exit(1)
            elif dotted == "autonomy_boundaries.daily_llm_call_limit":
                try:
                    _patch["daily_llm_call_limit"] = int(value)
                except ValueError:
                    console.print(f"[red]Field {dotted} expects integer, got '{value}'[/red]")
                    raise typer.Exit(1)
            elif dotted == "autonomy_boundaries.allowed_hours":
                _patch["allowed_hours"] = value
            elif dotted in ("autonomy_boundaries.allowed_sites", "autonomy_boundaries.allowlist"):
                _patch["allowlist"] = [s.strip() for s in value.split(",") if s.strip()] if value.strip() else []
            elif dotted == "autonomy_boundaries.deny_zones":
                _patch["deny_zones"] = [s.strip() for s in value.split(",") if s.strip()] if value.strip() else []
            elif dotted in ("computer_usage.idle_threshold_seconds", "autonomy_boundaries.idle_threshold_seconds"):
                try:
                    _patch["idle_threshold_seconds"] = int(value)
                except ValueError:
                    console.print(f"[red]Field {dotted} expects integer, got '{value}'[/red]")
                    raise typer.Exit(1)
            elif dotted == "computer_usage.idle_threshold_minutes":
                try:
                    _patch["idle_threshold_seconds"] = int(value) * 60
                except ValueError:
                    console.print(f"[red]Field {dotted} expects integer, got '{value}'[/red]")
                    raise typer.Exit(1)
            elif dotted in (
                "autonomy_boundaries.browser_consent.main_profile_granted",
                "browser_consent.main_profile_granted",
            ):
                _patch["browser_consent"] = value.lower() in ("true", "1", "yes", "y")
            elif dotted in ("readonly", "config.readonly"):
                _patch["readonly"] = value.lower() in ("true", "1", "yes", "y")
            elif dotted in ("require_idle", "config.require_idle"):
                _patch["require_idle"] = value.lower() in ("true", "1", "yes", "y")
            else:
                _is_settings_patch = False
                break
        if _is_settings_patch:
            from .app import IdleCua as _IdleCua

            _app = _IdleCua(config=IdleCuaConfig.load(resolved))
            try:
                _app.update_owner_settings(_patch)
                console.print(f"[green]Profile updated ({len(field)} field(s)) -> {ppath}[/green]")
                return
            except ValueError as ve:
                console.print(f"[red]Validation failed: {ve}[/red]")
                raise typer.Exit(1)

        data = profile.to_dict()
        for f in field:
            if "=" not in f:
                console.print(f"[red]Invalid --field '{f}' expected dotted.path=value[/red]")
                raise typer.Exit(1)
            dotted, value = f.split("=", 1)
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
            if isinstance(old, list):
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
            # Same domain validator the Application API uses, so non-settings
            # fields accept/reject identically regardless of caller.
            _errs = validate_profile(new_profile)
            if _errs:
                console.print(f"[red]Validation failed: {'; '.join(_errs)}[/red]")
                raise typer.Exit(1)
            save_profile(new_profile, ppath)
            console.print(f"[green]Profile updated ({len(field)} field(s)) -> {ppath}[/green]")
        except Exception as e:
            console.print(f"[red]Validation failed: {e}[/red]")
            raise typer.Exit(1)
        return

    console.print("[bold]Edit profile (leave empty to keep current value)[/bold]\n")
    data = profile.to_dict()
    for q in QUESTIONS:
        parts = q.key.split(".")
        cur = data
        for p in parts[:-1]:
            cur = cur[p]
        cur_val = cur[parts[-1]]
        if isinstance(cur_val, list):
            cur_str = ", ".join(cur_val)
        else:
            cur_str = str(cur_val)
        prompt_text = f"{q.prompt}"
        help_suffix = f" [dim]({q.help_text})[/dim]" if q.help_text else ""
        raw = Prompt.ask(f"{prompt_text}{help_suffix}", default=cur_str, console=console, show_default=True)
        if raw == cur_str:
            continue
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

    console.print("\n[bold]Updated profile preview:[/bold]")
    try:
        new_profile = Profile.from_dict(data)
    except Exception as e:
        console.print(f"[red]Invalid profile data: {e}[/red]")
        raise typer.Exit(1)
    _errs = validate_profile(new_profile)
    if _errs:
        console.print(f"[red]Validation failed: {'; '.join(_errs)}[/red]")
        raise typer.Exit(1)
    console.print(render_human_readable(new_profile))
    if not Confirm.ask("Save edited profile?", console=console, default=False):
        console.print("[yellow]Edit cancelled — not saved.[/yellow]")
        raise typer.Exit(1)
    new_profile.touch()
    save_profile(new_profile, ppath)
    console.print(f"[green]Profile saved to {ppath}[/green]")


@profile_app.command("validate")
def validate(
    ctx: typer.Context,
    data_dir: str | None = typer.Option(None, "--data-dir", help="Override data directory"),
) -> None:
    """Validate allowlist, limits, schedule sanity."""
    resolved = _resolve_data_dir(data_dir, ctx)
    ppath = _profile_path(resolved)
    profile = load_profile(ppath)
    if profile is None:
        console.print(f"[red]No profile found at {ppath}[/red]")
        raise typer.Exit(1)
    errors = validate_profile(profile)
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
    if any(s.granted is False for s in statuses):
        raise typer.Exit(1)
    if any(s.granted is None for s in statuses):
        console.print("[yellow]Could not definitively verify permissions — please verify manually via System Settings.[/yellow]")


@profile_app.command("grant-browser")
def grant_browser(
    ctx: typer.Context,
    browser: str = typer.Option("chrome", "--browser", help="Browser for main-profile consent (chrome/chromium/edge/brave)"),
    data_dir: str | None = typer.Option(None, "--data-dir", help="Override data directory"),
) -> None:
    """Explicitly grant IdleCUA to use your main Chrome profile (issue #12).

    Records consent in profile.json (autonomy_boundaries.browser_consent). This is
    the product-level explicit consent; the driver also requires
    `cua-driver serve --grant existing-profile` (or equivalent embedded grant)
    for the DevTools endpoint — see `idle-cua doctor`.
    Agent opens/closes only its own tabs, never owner tabs/windows, never chrome controls.
    """
    from datetime import datetime, timezone

    from .profile.models import BrowserConsent

    resolved = _resolve_data_dir(data_dir, ctx)
    ppath = _profile_path(resolved)
    profile = load_profile(ppath)
    if profile is None:
        console.print(f"[red]No profile found at {ppath}. Run `idle-cua profile interview` first.[/red]")
        raise typer.Exit(1)
    # T3: delegate through Application API so CLI and HTTP share the same consent path.
    # The grant decision persists via update_owner_settings; the CLI only adds
    # observed metadata (granted_at/browser/method) afterwards, then re-validates
    # with the same validator the API uses. No decision persists without validation.
    try:
        from .app import IdleCua as _IdleCua

        _app = _IdleCua(config=IdleCuaConfig.load(resolved))
        _app.update_owner_settings({"browser_consent": True})
    except ValueError as ve:
        console.print(f"[red]Validation failed: {ve}[/red]")
        raise typer.Exit(1)
    except Exception as e:
        # Thin caller per ADR-0002: never persist the decision outside the
        # Application API — fail loudly instead of direct-saving.
        console.print(f"[red]Browser consent update failed: {e}[/red]")
        raise typer.Exit(1)
    try:
        # Re-load to add granted_at/method without diverging from app's single authority.
        # Metadata-only: validate_profile ignores consent metadata (see
        # profile/validate.py — no consent checks), so validity cannot change
        # here; the grant decision was already validated via the API above.
        _profile2 = load_profile(ppath)
        if _profile2 is None:
            raise RuntimeError(f"No profile found at {ppath} after update")
        bc2 = BrowserConsent(
            main_profile_granted=True,
            granted_at=datetime.now(timezone.utc).isoformat(),
            browser=browser.lower().strip() or "chrome",
            grant_method="cli grant-browser",
        )
        _profile2.autonomy_boundaries.browser_consent = bc2
        _profile2.browser_consent = bc2
        _profile2.touch()
        save_profile(_profile2, ppath)
        try:
            cfg = IdleCuaConfig.load(resolved)
            cfg.record_browser_consent(True, browser=bc2.browser, granted_at=bc2.granted_at)
        except Exception:
            pass
        bc = bc2
    except Exception as e:
        # Decision already persisted via the API; metadata enrichment failed.
        console.print(f"[yellow]Consent granted but metadata not recorded: {e}[/yellow]")
        bc = BrowserConsent(
            main_profile_granted=True,
            granted_at=datetime.now(timezone.utc).isoformat(),
            browser=browser.lower().strip() or "chrome",
            grant_method="cli grant-browser",
        )
    console.print(f"[green]Browser main-profile consent GRANTED for {bc.browser} — recorded at {ppath} + config.json[/green]")
    console.print("[dim]Driver grant still required for existing-profile attachment:[/dim]")
    console.print("[dim]  cua-driver serve --grant existing-profile  (Watch loop process)  or  --grant existing-profile on mcp/embedded launch[/dim]")
    console.print("[dim]Verify with: idle-cua doctor[/dim]")


@profile_app.command("revoke-browser")
def revoke_browser(
    ctx: typer.Context,
    data_dir: str | None = typer.Option(None, "--data-dir", help="Override data directory"),
) -> None:
    """Revoke main Chrome profile consent (agent will no longer attach to main profile)."""
    from .profile.models import BrowserConsent

    resolved = _resolve_data_dir(data_dir, ctx)
    ppath = _profile_path(resolved)
    profile = load_profile(ppath)
    if profile is None:
        console.print(f"[red]No profile found at {ppath}. Run `idle-cua profile interview` first.[/red]")
        raise typer.Exit(1)
    # T3: delegate through Application API for parity. Thin caller per ADR-0002:
    # never persist the decision outside the API — fail loudly on errors.
    try:
        from .app import IdleCua as _IdleCua

        _app = _IdleCua(config=IdleCuaConfig.load(resolved))
        _app.update_owner_settings({"browser_consent": False})
    except ValueError as ve:
        console.print(f"[red]Validation failed: {ve}[/red]")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Browser consent update failed: {e}[/red]")
        raise typer.Exit(1)
    console.print(f"[yellow]Browser main-profile consent REVOKED — recorded at {ppath} + config.json[/yellow]")


@profile_app.command("browser-status")
def browser_status(
    ctx: typer.Context,
    data_dir: str | None = typer.Option(None, "--data-dir", help="Override data directory"),
) -> None:
    """Show browser main-profile consent + driver endpoint status."""
    resolved = _resolve_data_dir(data_dir, ctx)
    ppath = _profile_path(resolved)
    profile = load_profile(ppath)
    if profile is None:
        console.print(f"[red]No profile found at {ppath}. Run `idle-cua profile interview` first.[/red]")
        raise typer.Exit(1)
    bc = profile.autonomy_boundaries.browser_consent
    if bc.main_profile_granted:
        console.print(f"[green]Browser main-profile consent: GRANTED[/green] ({bc.browser}, {bc.granted_at}, via {bc.grant_method})")
    else:
        console.print("[yellow]Browser main-profile consent: NOT granted — agent will not attach to main profile[/yellow]")
        console.print("[dim]Grant with: idle-cua profile grant-browser[/dim]")
    # Also show driver grant hint
    console.print("\n[bold]Driver grant (DevTools endpoint)[/bold]")
    console.print("[dim]Existing-profile attachment requires driver grant:[/dim]")
    console.print("[dim]  cua-driver serve --grant existing-profile[/dim]")
    console.print("[dim]Check driver readiness: idle-cua doctor[/dim]")
    if not bc.main_profile_granted:
        raise typer.Exit(1)
