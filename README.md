# IdleCUA

Policy-gated idle-time computer-use agent for macOS — autonomous research, feeds, and link saving while your Mac is idle. Built on Cua primitives with idle scheduling, policy gating, and read-only defaults.

> **Slice 2 — ModelProvider:** single OpenAI-compatible chat+vision adapter over HTTP with strict request shape (gateway-safe), system Keychain for API keys, `models` CLI, daily LLM-call accounting, and `FakeModelProvider` still available for tests.

## Install

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --group dev
# or: pip install -e ".[dev]"
```

Verify the CLI:

```bash
uv run idle-cua --help
```

## Quickstart

```bash
# Create data dir and default config (default: ~/.idlecua)
uv run idle-cua init
uv run idle-cua init --data-dir ./data

# Deterministic dry-run plan — prints a bounded typed plan, executes nothing
uv run idle-cua plan "research recent AI papers on agents"
uv run idle-cua run-once --dry-run "research recent AI papers on agents"

# Configure an OpenAI-compatible provider (key goes to macOS Keychain, never to file)
uv run idle-cua models add --name openrouter \
  --base-url https://openrouter.ai/api/v1 \
  --model anthropic/claude-3.5-sonnet \
  --api-key $OPENROUTER_API_KEY

# OpenCode Go gateway (strict gateway — only standard OpenAI fields are sent)
uv run idle-cua models add --name opencode-go \
  --base-url https://opencode.ai/zen/go/v1 \
  --model anthropic/claude-3.5-sonnet \
  --api-key-env OPENCODE_GO_API_KEY

# Any other OpenAI-compatible endpoint works the same way
uv run idle-cua models add --name my-openai \
  --base-url https://api.openai.com/v1 \
  --model gpt-4o \
  --api-key $OPENAI_API_KEY

uv run idle-cua models list
uv run idle-cua models select openrouter
uv run idle-cua models test            # verifies connectivity + vision capability
uv run idle-cua models test --name opencode-go --json
uv run idle-cua models remove my-openai
```

Python API (same planner, no CLI):

```python
from idlecua import IdleCua, IdleCuaConfig
from idlecua.contracts import FakeComputerDriver, FakeModelProvider

config = IdleCuaConfig(data_dir="./data")
driver = FakeComputerDriver()
app = IdleCua(config=config, computer=driver, model_provider=FakeModelProvider())

task = app.create_task("research recent AI papers on agents")
plan = app.dry_run(task.description)   # or app.plan(...)
print(plan)
assert len(driver.calls) == 0  # dry-run never touches the driver

# State machine is validated:
task.transition_to("planning")  # OK
task.transition_to("running")   # illegal from disabled -> raises ValueError
```

### Model provider (OpenAI-compatible)

```python
from pathlib import Path
from idlecua import IdleCua, IdleCuaConfig

# Uses the selected provider from `models add` / system Keychain automatically
app = IdleCua(config=IdleCuaConfig(data_dir=Path.home() / ".idlecua"))
print(type(app.model_provider).__name__)  # OpenAICompatibleProvider when configured, else FakeModelProvider

# The fake still works for tests — inject it explicitly
app_fake = IdleCua(config=IdleCuaConfig(data_dir="./tmp"), model_provider=FakeModelProvider(response="hi"))
assert app_fake.model_provider.complete("hello") == "hi"

# Direct adapter (for embedding / scripts)
from idlecua.providers.config import ProviderConfig
from idlecua.providers.openai_adapter import OpenAICompatibleProvider

cfg = ProviderConfig(name="openrouter", base_url="https://openrouter.ai/api/v1", model="anthropic/claude-3.5-sonnet")
provider = OpenAICompatibleProvider(config=cfg, api_key="sk-...", data_dir=Path("./data"))
print(provider.complete("Hello, vision model!"))
# provider.chat([{"role":"user","content":[{"type":"text","text":"What's in this image?"},
#                                             {"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}]}])

# Daily accounting hook (for the 150-call cap)
from idlecua.accounting import get_today_count, record_llm_call
record_llm_call("./data")  # called automatically on each real LLM call
print("today:", get_today_count("./data"))
```

## Project layout

```
src/idlecua/          # single src-layout package
  cli.py              # Typer CLI (thin caller of the Application API)
  cli_models.py       # `models` subcommands (add/list/select/test/remove)
  app.py              # IdleCua / IdleCuaConfig (public Application API)
  planner.py          # deterministic stub planner
  accounting.py       # daily LLM call accounting hook (llm_usage.json)
  keychain.py         # system credential store (Keychain) — keys never in files
  models/             # Task, Plan, AgentState
  contracts/          # ComputerDriver, ModelProvider + fakes
  providers/
    config.py         # ProviderConfig + ProviderStore (providers.json, no secrets)
    openai_adapter.py # OpenAI-compatible chat+vision adapter (strict payload)
  docs/adr/           # Architecture Decision Records
  CONTEXT.md          # domain glossary (single-context layout)
```

Key safety: API keys live only in the macOS Keychain (service `idlecua`, account `provider:<name>`); `providers.json` stores only `name`, `base_url`, `model`. Nothing sensitive is written to config files, logs, reports, or the repo. `models test` reports `unreachable` vs `reachable-but-not-vision-capable` vs `ok`; vision is probed with a 1×1 PNG. Strict request shape (only `model` + `messages`) keeps the OpenCode Go gateway (`https://opencode.ai/zen/go/v1`) happy.

See `CONTEXT.md` and `docs/adr/0001-mvp-scope.md` for domain language and locked MVP scope.

## Configuration

Copy `.env.example` to `.env` and set only what you need. Secrets (API keys) are never stored in the config file — use the system credential store or environment variables.

Provider presets:

| Preset | Base URL | Example model | Env var for key |
|--------|----------|---------------|-----------------|
| OpenRouter | `https://openrouter.ai/api/v1` | `anthropic/claude-3.5-sonnet` | `OPENROUTER_API_KEY` |
| OpenCode Go | `https://opencode.ai/zen/go/v1` | `anthropic/claude-3.5-sonnet` | `OPENCODE_GO_API_KEY` |

Any other OpenAI-compatible endpoint works identically — pass its `base_url` and `model`.

## Development

```bash
uv run pytest -q
uv run ruff check src
```

## License

MIT
