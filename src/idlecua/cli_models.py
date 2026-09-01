from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from .config import IdleCuaConfig
from .keychain import get_default_store
from .providers.config import PRESET_OPENCODE_GO, PRESET_OPENROUTER, ProviderConfig, ProviderStore
from .providers.openai_adapter import OpenAICompatibleProvider, VisionTestStatus

console = Console()
models_app = typer.Typer(
    name="models",
    help="Manage OpenAI-compatible model providers (api keys in system credential store).",
    no_args_is_help=True,
)


def _resolve_data_dir(data_dir: Optional[str]) -> Path:
    if data_dir is not None:
        return Path(data_dir).expanduser()
    # Use IdleCuaConfig default (env-aware)
    return IdleCuaConfig().data_dir


def _store(data_dir: Optional[str]) -> ProviderStore:
    base = _resolve_data_dir(data_dir)
    return ProviderStore.load(base)


def _keychain(data_dir: Optional[str]):
    base = _resolve_data_dir(data_dir)
    return get_default_store(base)


@models_app.command("add")
def models_add(
    name: Annotated[str, typer.Option("--name", help="Provider name (e.g. openrouter, opencode-go, my-openai)")],
    base_url: Annotated[str, typer.Option("--base-url", help="Base URL (e.g. https://openrouter.ai/api/v1)")],
    model: Annotated[str, typer.Option("--model", help="Model id (e.g. anthropic/claude-3.5-sonnet)")],
    api_key: Annotated[
        Optional[str],
        typer.Option(
            "--api-key",
            help="API key (prefer prompt/env; never written to file)",
            hide_input=True,
        ),
    ] = None,
    api_key_env: Annotated[
        Optional[str],
        typer.Option("--api-key-env", help="Env var holding the API key"),
    ] = None,
    data_dir: Annotated[
        Optional[str],
        typer.Option("--data-dir", help="Data directory"),
    ] = None,
) -> None:
    """Add or update a provider (key goes to system credential store, never to file)."""
    if not name or not name.strip():
        console.print("[red]--name is required[/red]")
        raise typer.Exit(code=2)
    if not base_url or not base_url.strip():
        console.print("[red]--base-url is required[/red]")
        raise typer.Exit(code=2)
    if not model or not model.strip():
        console.print("[red]--model is required[/red]")
        raise typer.Exit(code=2)

    # Resolve api_key from flags/env/file without prompting (non-interactive per spec)
    resolved_key: Optional[str] = None
    if api_key is not None:
        resolved_key = api_key
    elif api_key_env is not None:
        resolved_key = os.environ.get(api_key_env)
        if not resolved_key:
            console.print(f"[red]Env var {api_key_env} is not set or empty[/red]")
            raise typer.Exit(code=2)
    else:
        # also check generic envs for preset names
        # e.g., OPENROUTER_API_KEY, OPENCODE_GO_API_KEY
        env_candidates = [
            f"{name.upper().replace('-', '_')}_API_KEY",
            "OPENROUTER_API_KEY",
            "OPENCODE_GO_API_KEY",
            "OPENAI_API_KEY",
        ]
        for k in env_candidates:
            if os.environ.get(k):
                resolved_key = os.environ.get(k)
                break

    if not resolved_key or not resolved_key.strip():
        console.print(
            "[red]API key is required: pass --api-key <key> or --api-key-env <VAR> or set env var[/red]\n"
            "Examples:\n"
            "  idle-cua models add --name openrouter --base-url https://openrouter.ai/api/v1 --model anthropic/claude-3.5-sonnet --api-key $OPENROUTER_API_KEY\n"
            "  idle-cua models add --name opencode-go --base-url https://opencode.ai/zen/go/v1 --model anthropic/claude-3.5-sonnet --api-key-env OPENCODE_GO_API_KEY"
        )
        raise typer.Exit(code=2)

    # Validate config (will raise ValueError with message)
    try:
        cfg = ProviderConfig(name=name.strip(), base_url=base_url.strip(), model=model.strip())
    except ValueError as e:
        console.print(f"[red]Invalid provider config: {e}[/red]")
        raise typer.Exit(code=2)

    store = _store(data_dir)
    # Save non-secret config
    store.providers[cfg.name] = cfg
    if store.selected is None:
        store.selected = cfg.name
    store.save()

    # Save secret to credential store
    kc = _keychain(data_dir)
    try:
        kc.set(cfg.name, resolved_key.strip())
    except ValueError as e:
        console.print(f"[red]Invalid API key: {e}[/red]")
        raise typer.Exit(code=2)
    except Exception as e:
        console.print(f"[red]Failed to store API key in system credential store: {e}[/red]")
        raise typer.Exit(code=1)

    console.print(f"[green]Provider '{cfg.name}' saved[/green] (base_url={cfg.base_url}, model={cfg.model})")
    console.print(f"Key stored in system credential store (service 'idlecua', account 'provider:{cfg.name}')")
    if store.selected == cfg.name:
        console.print(f"[dim]Selected provider: {cfg.name}[/dim]")


@models_app.command("list")
def models_list(
    data_dir: Annotated[
        Optional[str],
        typer.Option("--data-dir", help="Data directory"),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output JSON"),
    ] = False,
) -> None:
    """List configured providers (keys never shown)."""
    store = _store(data_dir)
    if json_output:
        payload = {
            "selected": store.selected,
            "providers": {n: c.to_dict() for n, c in store.providers.items()},
        }
        console.print_json(json.dumps(payload))
        return

    if not store.providers:
        console.print("[yellow]No providers configured[/yellow]")
        console.print("Add one with:")
        console.print("  idle-cua models add --name openrouter --base-url https://openrouter.ai/api/v1 --model anthropic/claude-3.5-sonnet --api-key $OPENROUTER_API_KEY")
        console.print("  idle-cua models add --name opencode-go --base-url https://opencode.ai/zen/go/v1 --model anthropic/claude-3.5-sonnet --api-key-env OPENCODE_GO_API_KEY")
        console.print("\nPresets:")
        console.print(f"  OpenRouter: base_url={PRESET_OPENROUTER.base_url} model={PRESET_OPENROUTER.model}")
        console.print(f"  OpenCode Go: base_url={PRESET_OPENCODE_GO.base_url} model={PRESET_OPENCODE_GO.model}")
        console.print("Any other OpenAI-compatible endpoint works the same way — pass its base URL and model.")
        return

    table = Table(title="Model providers (keys in credential store, never in file)")
    table.add_column("Name", style="bold")
    table.add_column("Base URL")
    table.add_column("Model")
    table.add_column("Selected")
    for name, cfg in sorted(store.providers.items()):
        sel = "★" if store.is_selected(name) else ""
        table.add_row(name, cfg.base_url, cfg.model, sel)
    console.print(table)
    console.print(f"[dim]Selected: {store.selected or 'none'} — change with 'idle-cua models select <name>'[/dim]")


@models_app.command("select")
def models_select(
    name: Annotated[str, typer.Argument(help="Provider name to select")],
    data_dir: Annotated[
        Optional[str],
        typer.Option("--data-dir", help="Data directory"),
    ] = None,
) -> None:
    """Select the active provider for the application."""
    store = _store(data_dir)
    if name not in store.providers:
        console.print(f"[red]Provider '{name}' not found[/red]")
        available = ", ".join(sorted(store.providers)) or "none"
        console.print(f"Available: {available}")
        raise typer.Exit(code=2)
    store.select(name)
    console.print(f"[green]Selected provider: {name}[/green]")


@models_app.command("remove")
def models_remove(
    name: Annotated[str, typer.Argument(help="Provider name to remove")],
    data_dir: Annotated[
        Optional[str],
        typer.Option("--data-dir", help="Data directory"),
    ] = None,
) -> None:
    """Remove a provider and delete its key from the credential store."""
    store = _store(data_dir)
    if name not in store.providers:
        console.print(f"[red]Provider '{name}' not found[/red]")
        raise typer.Exit(code=2)
    # Remove config first
    try:
        store.remove(name)
    except KeyError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2)
    # Remove key from credential store (best effort)
    kc = _keychain(data_dir)
    try:
        kc.delete(name)
    except Exception as e:
        console.print(f"[yellow]Warning: failed to delete key from credential store: {e}[/yellow]")
    console.print(f"[green]Removed provider '{name}'[/green]")
    if store.selected:
        console.print(f"[dim]Now selected: {store.selected}[/dim]")
    else:
        console.print("[dim]No provider selected[/dim]")


@models_app.command("test")
def models_test(
    name: Annotated[
        Optional[str],
        typer.Option("--name", help="Provider name to test (default: selected)"),
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
    """Test connectivity and report vision capability.

    Distinguishes:
    - unreachable (network/auth/base_url failure)
    - reachable-but-not-vision-capable (model rejects image input — computer use requires vision)
    - ok (reachable + vision-capable)
    """
    store = _store(data_dir)
    target_name = name or store.selected
    if not target_name:
        console.print("[red]No provider selected and --name not given[/red]")
        console.print("Add a provider first, then select it:")
        console.print("  idle-cua models add --name openrouter --base-url https://openrouter.ai/api/v1 --model anthropic/claude-3.5-sonnet --api-key $KEY")
        raise typer.Exit(code=2)
    if target_name not in store.providers:
        console.print(f"[red]Provider '{target_name}' not found[/red]")
        raise typer.Exit(code=2)
    cfg = store.get(target_name)
    kc = _keychain(data_dir)
    api_key = kc.get(target_name)
    if not api_key:
        # Also check env fallbacks for presets
        env_map = {
            "openrouter": "OPENROUTER_API_KEY",
            "opencode-go": "OPENCODE_GO_API_KEY",
        }
        env_var = env_map.get(target_name) or f"{target_name.upper().replace('-', '_')}_API_KEY"
        api_key = os.environ.get(env_var) or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        msg = f"No API key found for provider '{target_name}' in system credential store (service 'idlecua', account 'provider:{target_name}')"
        if json_output:
            console.print_json(json.dumps({"name": target_name, "status": "unreachable", "message": msg}))
        else:
            console.print(f"[red]{msg}[/red]")
            console.print("Add the key with: idle-cua models add --name {} --base-url {} --model {} --api-key <key>".format(cfg.name, cfg.base_url, cfg.model))
        raise typer.Exit(code=2)

    base_path = _resolve_data_dir(data_dir)
    provider = OpenAICompatibleProvider(config=cfg, api_key=api_key, data_dir=base_path)

    status, message = provider.test_vision()

    if json_output:
        console.print_json(json.dumps({"name": target_name, "status": status.value, "message": message}))
        if status == VisionTestStatus.unreachable:
            raise typer.Exit(code=1)
        if status == VisionTestStatus.not_vision:
            raise typer.Exit(code=3)
        raise typer.Exit(code=0)

    if status == VisionTestStatus.ok:
        console.print(f"[green]OK[/green] — {message} ({target_name}: {cfg.base_url}, model={cfg.model})")
        raise typer.Exit(code=0)
    if status == VisionTestStatus.not_vision:
        console.print(f"[yellow]Reachable but NOT vision-capable[/yellow] — {message}")
        console.print(f"Provider '{target_name}' ({cfg.model}) is reachable but the model does not accept image input.")
        console.print("Computer use requires a vision-capable model. Switch to a vision model (e.g. anthropic/claude-3.5-sonnet, gpt-4o).")
        raise typer.Exit(code=3)
    # unreachable
    console.print(f"[red]Unreachable[/red] — {message}")
    console.print(f"Check base_url ({cfg.base_url}), model ({cfg.model}), API key, and network.")
    raise typer.Exit(code=1)
