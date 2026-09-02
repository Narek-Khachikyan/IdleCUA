# IdleCUA

Policy-gated idle-time computer-use agent for macOS — autonomous research, feeds, and link saving while your Mac is idle. Built on Cua primitives with idle scheduling, policy gating, and read-only defaults.

> **Slice 2 — ModelProvider:** single OpenAI-compatible chat+vision adapter over HTTP with strict request shape (gateway-safe), system Keychain for API keys, `models` CLI, daily LLM-call accounting, and `FakeModelProvider` still available for tests.
> **Slice 3 — ComputerDriver:** real host primitives over `cua-driver==0.23.2` (pinned) with macOS permission docs, input journal, and verified owner smoke via `run-once --real-driver`.

## Install

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --group dev
# or: pip install -e ".[dev]"
```

Cua driver (pinned):

```bash
uv pip install cua-driver==0.23.2
# verify
uv run python -c "import cua_driver; print(cua_driver.__version__)"
```

See **Cua Driver — Host Primitives** below for macOS Accessibility and Screen Recording steps (required before any `--real-driver` smoke).

Verify the CLI:

```bash
uv run idle-cua --help
uv run idle-cua doctor  # checks permissions + cua-driver probe
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
### Profile onboarding (required before autonomous runs)

```bash
# Scripted questionnaire — works with no LLM configured, saves only after explicit confirmation
uv run idle-cua profile interview
uv run idle-cua profile interview --yes  # non-interactive defaults (for tests)

# View / edit / validate
uv run idle-cua profile show
uv run idle-cua profile show --json
uv run idle-cua profile edit --field autonomy_boundaries.allowed_sites="x.com, reddit.com"
uv run idle-cua profile validate   # rejects invalid allowlist / limits / schedule
uv run idle-cua profile check-permissions  # macOS Accessibility + Screen Recording with remediation
uv run idle-cua doctor  # permission checks + profile validate + cua-driver probe (pinned 0.23.2)
```

### Cua Driver — Host Primitives (issue #11)

The real `ComputerDriver` is `CuaComputerDriver` over `cua-driver==0.23.2` (pinned in `pyproject.toml`). The fake driver remains the automated-test surface — no automated tests touch real third-party sites.

**Install (pinned):**

```bash
uv pip install cua-driver==0.23.2
# or: uv sync --group dev  # already pins cua-driver
uv run python -c "import cua_driver; print(cua_driver.__version__)"  # 0.23.2
# Docs: https://cua.ai/docs/how-to-guides/driver/install  and  https://cua.ai/docs/reference/cua-driver/mcp-tools
# The embedded runtime (CuaDriver.create()) needs no separate daemon; the `cua-driver` binary is bundled in the wheel.
```

**macOS permissions (required before `--real-driver`):**

1. System Settings → Privacy & Security → **Accessibility**: enable your terminal (Terminal, iTerm, VS Code, or whichever runs `idle-cua`).
2. System Settings → Privacy & Security → **Screen Recording**: enable the same terminal/app.
3. Restart the terminal/app after granting.
4. Verify:

```bash
uv run idle-cua doctor
uv run idle-cua profile check-permissions
uv run python -c "import cua_driver; print(cua_driver.current_mac_os_permission_status())"
# Expected: MacOsPermissionStatus(accessibility=True, screen_recording=True)
```

If either is `False`, the real driver will refuse with an actionable error (task fails, error persists to report, no silent fallback).

**Smoke on the real machine (owner-verified, trivial read-only):**

```bash
# Ensure profile is confirmed first
uv run idle-cua profile interview --yes
# Alternatively reuse an existing profile: idle-cua profile show

# Trivial read-only smoke: launch Calculator (host primitive) and read its window state.
# No browser, no network — host-only, fully reversible.
uv run idle-cua run-once --real-driver "launch Calculator and verify its window is visible" --json

# Other read-only smoke examples:
uv run idle-cua run-once --real-driver "open TextEdit and verify window state" --json
uv run idle-cua run-once --real-driver "take a screenshot and read the accessibility tree" --json

# Or via Python API:
```

```python
from pathlib import Path
from idlecua import IdleCua, IdleCuaConfig
from idlecua.drivers.cua_driver import CuaComputerDriver

config = IdleCuaConfig(data_dir=Path("./data"))
driver = CuaComputerDriver(session="idlecua-smoke", data_dir=config.data_dir)
app = IdleCua(config=config, computer=driver)
result = app.run_once("launch Calculator and verify window", dry_run=False)
print(result.state, result.report_path)
print(app.computer.get_accessibility_tree().keys())
print(app.computer.get_window_state().keys())
# Emergency-stop journal check:
print(driver.journal.log)
driver.release_all_inputs()  # releases held keys/buttons synchronously
```

**Contract guarantees:**

- `screenshot()` → PNG bytes via `get_desktop_state` (true screen pixels, `is_error` checked).
- `get_accessibility_tree()` / `get_window_state()` → structured elements via `get_accessibility_tree` + `get_window_state(pid, window_id)` (AX tree, bounds, z-order).
- `click(x,y)`, `type_text(text)`, `press(key)`, `hotkey(keys)`, `scroll(dx,dy)` → typed input via `click` / `type_text` / `press_key` / `hotkey` / `scroll` with `scope="desktop"` and `InputJournal` recording.
- `open_app(name)` → `launch_app(bundle_id)` (backgrounded, no foreground steal).
- `release_all_inputs()` → journal `release_all()` (emergency-stop path, LLM-independent).
- Driver failures (missing wheel, permissions, daemon) surface as task failures with actionable `Remediation:` messages and are persisted to `reports/` and `errors` table — never silent.

### Browser surface on main Chrome profile (issue #12)

The browser surface runs on the **owner's main Chrome profile** — your logged-in sessions stay available — but is **scoped strictly to agent-owned tabs**.

- **Explicit consent** recorded in `profile.json` (`autonomy_boundaries.browser_consent`) *and* mirrored to `config.json` (`browser_main_profile_granted`). Interview asks `Grant IdleCUA to use your main Chrome profile?` and CLI provides `profile grant-browser` / `revoke-browser` / `browser-status`.
- **Driver-level grant** separate: `cua-driver serve --grant existing-profile` (or embedded `CuaDriver.create_configured` with authorization) must also be given for the DevTools endpoint. Without it the driver refuses with `browser_consent_required` and remediation (`idle-cua doctor` checks both).
- **Tab discipline:** the agent tracks `agent_tabs` vs `owner_tabs`. `open_browser_tab` / `open_url` adds to `agent_tabs`; `close_tab(tab_id)` refuses owner tabs; `close_all_agent_tabs()` at task end closes only agent tabs. `FakeComputerDriver` simulates this for tests (`add_owner_tab_for_test`, `is_agent_tab`, `get_agent_tabs`).
- **Semantic refs + synthetic fallback:** navigation/click/type prefer `get_browser_state` (`semantic_v2`) refs: `browser_navigate(target_id, tab_id, url)`, `browser_click(ref, tab_id, input_route="trusted")` with automatic fallback to `dom_event` (synthetic `el.click()`) when trusted background input is unavailable on macOS. Every significant action is **verified by re-reading** UI state (`verify_browser_state` / `get_browser_state` after the action); failures retry once or are recorded as errors in SQLite + report.
- **Policy gating during real browsing:** every navigation is evaluated by `PolicyEngine` (closed allowlist → deny-zones → action class) before the driver call. `news.ycombinator.com`, `google.com`, `github.com`, `x.com`, `reddit.com` etc. are allowlisted; deny-zones (`/messages`, `/settings`, `/account`, …) are hard-blocked. The driver also double-checks `navigate` targets as defense. `planner` maps `hacker`/`HN` → `news.ycombinator.com`, `google`/`search` → `google.com`, etc., and the stub now distinguishes `open app` host tasks from browsing tasks.
- **First real read-only browsing task (owner-observed, read-only, reversible):**

```bash
# 1. Ensure profile is confirmed and browser consent granted
uv run idle-cua profile interview --yes   # now asks for browser consent and mirrors to config.json
# or explicitly:
uv run idle-cua profile grant-browser --browser chrome
uv run idle-cua profile browser-status
uv run idle-cua doctor  # checks profile consent + driver grant + permissions

# 2. Ensure Chrome is running (your main profile) and driver has grant
#    If you run the driver manually:
#    cua-driver serve --grant existing-profile
#    For the embedded Python driver, the same grant must be supplied via your launcher
#    (python `CuaDriver.create_configured` with authorization). Without it, the driver
#    fails with `browser_consent_required` and remediation.

# 3. Run a safe read-only browsing task (Hacker News or Google search → open results → save links + notes)
uv run idle-cua run-once --real-driver "Research Hacker News top stories and save links with short notes" --json
uv run idle-cua run-once --real-driver "Google search for recent AI agents papers and open top results" --json

# Check report + history + that only agent tabs were closed
uv run idle-cua report
uv run idle-cua history
uv run idle-cua profile browser-status
```

Python API for the same browsing task:

```python
from pathlib import Path
from idlecua import IdleCua, IdleCuaConfig
from idlecua.drivers.cua_driver import CuaComputerDriver

cfg = IdleCuaConfig(data_dir=Path("./data-browsing"))
# Profile must be confirmed and browser_consent granted in that data_dir
driver = CuaComputerDriver(session="idlecua-browsing", data_dir=cfg.data_dir)
app = IdleCua(config=cfg, computer=driver)
# The driver will call browser_prepare(existing_profile) using your running Chrome window
result = app.run_once("Research Hacker News top stories and save links with short notes", dry_run=False)
print(result.state, result.report_path)
print("agent tabs after (should be 0):", driver.get_agent_tabs())
print("owner tabs untouched:", driver.get_browser_state())  # or driver.journal.log for verify-by-reread entries
print(result.report_markdown[:2000])
```

**Verification details for #12:**

- `verify_browser_state(expected_url_contains)` is called after every `open_browser_tab` / `browser_navigate` / `browser_click` / `browser_type`; on mismatch it retries once then records `verify failed` in `errors` and the report.
- `scroll` and `read_ui`/`extract` also re-read `get_browser_state` (semantic_v2) when available, else `get_accessibility_tree`.
- At task end `close_all_agent_tabs()` is called automatically; `FakeComputerDriver` proves `owner_tabs` stay untouched via `close_tab_refused_owner`.

The interview covers user characteristics (occupation, projects, goals, interests, technologies, material types/depth, languages, unwanted topics), computer usage (schedule, idle periods, overnight/screen-lock habits, monitors, common apps/sites, return signals, idle threshold), and autonomy boundaries (allowed sites/apps, action classes, results location, session duration, daily limits, allowed hours). The summary clearly separates **confirmed facts** (you provided) from **assumptions** (defaults), and `profile.json` is only written after explicit `y` confirmation. Machine-readable `profile.json` and human rendering (`profile show`) are the same data — no duplication. Autonomous `run-once` is hard-gated while the profile is unconfirmed.

### Full autonomous idle session + daily report (issue #13)

End-to-end on the real machine: **idle auto-start → plan → policy → real driver → SQLite history → daily Markdown report → graceful stop on return/limits/emergency stop.**

```bash
# 1. Autonomous watch — polls HID hardware timer (synthetic never masks return), respects screen-lock gate
uv run idle-cua start --watch --once --idle-threshold 60 --poll-interval 5 \
  --data-dir ~/.idlecua "research latest AI agent papers on arxiv and save links" --json
#   --watch loop handles: wait-for-idle → session → wait again; auto-resume only at next idle
#   --once for single session; omit to loop forever (Ctrl-C / idle-cua kill to stop)

# Immediate session without idle wait (still gated by profile + policy):
uv run idle-cua start --idle-threshold 60 --data-dir ~/.idlecua "research AI papers"

# Check status (agent state, idle time, active task, last action, site, limits, stop command):
uv run idle-cua status --data-dir ~/.idlecua --json
# Pause/resume (user-return simulation):
uv run idle-cua pause --data-dir ~/.idlecua
uv run idle-cua resume --data-dir ~/.idlecua

# History + reports:
uv run idle-cua history --data-dir ~/.idlecua --json
uv run idle-cua report --data-dir ~/.idlecua       # latest
uv run idle-cua report <task_id> --data-dir ~/.idlecua --json
ls ~/.idlecua/reports/              # per-task <task_id>.md + daily YYYY-MM-DD.md
cat ~/.idlecua/reports/$(date -u +%F).md  # daily Markdown report (all sections)

# Secrets-absent verification (repo + reports + DB + logs):
uv run idle-cua verify-secrets --data-dir ~/.idlecua --verbose
uv run idle-cua doctor --data-dir ~/.idlecua  # also runs secrets scan + permissions + driver probe

# Acceptance sweep (16 criteria — automated + manual):
uv run python scripts/acceptance_sweep.py --data-dir ~/.idlecua --verbose
cat docs/ACCEPTANCE_CHECKLIST.md        # manual checklist for real-machine sweep
```

**Scheduler guarantees:**
- `QuartzIdleDetector` uses `kCGEventSourceStateHIDSystemState` (not combined) so the agent's synthetic `click`/`type_text` never resets the HID idle clock; watchdog fallback if quartz unavailable.
- Gates before any work: idle ≥ threshold (default 10 min, test override via `--idle-threshold`), screen unlocked, allowed hours (default 24/7), limits not reached, task allowed by policy, profile confirmed.
- On user return (hardware input detected): immediate `release_all_inputs()`, no next step, task → `paused_by_user`, saved state; auto-resume only at next idle period.
- Limits enforced gracefully: ≤45 min / ≤200 actions per session / ≤150 LLM calls/day — stop reason saved, report produced, no data loss.
- `verify-secrets` scans repo, `reports/*.md`, logs, and SQLite for `sk-*`, `api_key`, `ghp_*`, etc. (allowlisting placeholders), ensuring keys live only in Keychain/env; `providers.json` stores only `name`/`base_url`/`model`.

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
  drivers/
    cua_driver.py     # CuaComputerDriver over cua-driver==0.23.2 (host primitives, InputJournal)
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
