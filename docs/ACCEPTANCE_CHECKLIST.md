# Acceptance Checklist — IdleCUA MVP (Issue #13)

Manual owner sweep across all 16 MVP criteria from spec #1. Covers automated-focused tests plus real-machine manual verification.

> **How to run:** Follow each criterion in order. For automated criteria, run the listed command; for manual, perform on the real Mac with the real driver/profile. Record result (`PASS`/`FAIL`/`SKIPPED` with note) directly in this file or via `python scripts/acceptance_sweep.py`.

Reference: `docs/adr/0001-mvp-scope.md`, `CONTEXT.md`, spec issue #1.

---

## Preconditions

- Python 3.13, `uv` installed.
- macOS with main Chrome profile logged into allowed sites (Hacker News, Google, etc.).
- Terminal granted Accessibility + Screen Recording (System Settings → Privacy & Security).
- `cua-driver==0.23.2` pinned (`uv pip install cua-driver==0.23.2`).

```bash
uv sync --group dev
uv run python -c "import cua_driver; print(cua_driver.__version__)"  # 0.23.2
uv run idle-cua doctor --help
```

---

## 1. Install — single package install

**Spec:** User story 1 — installable package (uv, src-layout, Typer+Rish CLI thin over Application API).

Steps:
```bash
uv sync --group dev
uv run idle-cua --help
uv run idle-cua doctor  # checks permissions + cua-driver probe
```
Expected:
- `idle-cua --help` lists commands: init, plan, run-once, doctor, status, history, report, kill/stop, start, pause, resume, models, profile, verify-secrets.
- `doctor` reports `cua-driver version: 0.23.2` and permission status (granted / remediation).

Result: ________  Notes: ________

---

## 2. Onboarding interview — profile created

**Spec:** User stories 2-4 (characteristics, computer usage, autonomy boundaries); scripted questionnaire works before any provider configured.

Steps:
```bash
uv run idle-cua init --data-dir ./data-accept
uv run idle-cua profile interview --yes  # non-interactive defaults for automation
# Interactive variant (owner-observed):
uv run idle-cua profile interview
# Answer prompts; summary must separate confirmed facts vs assumptions; explicit y/n
```
Expected:
- `profile interview --yes` creates `data-accept/profile.json` with `confirmed: true`.
- Interactive summary clearly labels **confirmed facts** (you provided) vs **assumptions** (defaults).
- `profile.json` is only written after explicit `y` confirmation.

Result: ________  Notes: ________

---

## 3. Profile confirmation gate — no autonomous action while unconfirmed

**Spec:** User stories 5-7 (view/edit/validate, gate blocks execution).

Steps:
```bash
uv run idle-cua profile show --data-dir ./data-accept
uv run idle-cua profile validate --data-dir ./data-accept
# Test gate: remove/rename profile and run
mv ./data-accept/profile.json ./data-accept/profile.json.bak
uv run idle-cua run-once --data-dir ./data-accept "research X"  # should refuse
mv ./data-accept/profile.json.bak ./data-accept/profile.json
uv run idle-cua profile edit --field autonomy_boundaries.allowed_sites="x.com, reddit.com" --data-dir ./data-accept
uv run idle-cua profile validate --data-dir ./data-accept
```
Expected:
- `profile show` renders same data as `profile.json` (no duplication).
- `profile validate` rejects invalid allowlist/limits.
- `run-once` while unconfirmed/missing prints `Refused: ... unconfirmed ... Autonomous runs are blocked` and exits 1.

Result: ________  Notes: ________

---

## 4. Provider connect — OpenAI-compatible ModelProvider + Keychain

**Spec:** User stories 8-9 (add/list/select/test/remove, keys in Keychain never in files).

Steps:
```bash
uv run idle-cua models add --name openrouter --base-url https://openrouter.ai/api/v1 --model anthropic/claude-3.5-sonnet --api-key $OPENROUTER_API_KEY --data-dir ./data-accept
uv run idle-cua models list --data-dir ./data-accept
uv run idle-cua models test --data-dir ./data-accept  # probes connectivity + vision capability
cat ./data-accept/providers.json  # should contain only name/base_url/model, no api_key
uv run idle-cua models remove openrouter --data-dir ./data-accept
# OpenCode Go gateway variant (strict payload — only model+messages)
uv run idle-cua models add --name opencode-go --base-url https://opencode.ai/zen/go/v1 --model anthropic/claude-3.5-sonnet --api-key-env OPENCODE_GO_API_KEY --data-dir ./data-accept
```
Expected:
- `models test` reports `ok` / `reachable-but-not-vision-capable` / `unreachable` with actionable message.
- `providers.json` contains `name`, `base_url`, `model` only; `api_key` is in Keychain (`idlecua` / `provider:<name>`), never in file/logs/reports.
- `models list --json` shows selected provider; removing works.

Result: ________  Notes: ________

---

## 5. Allowlist configuration — closed, preseeded, owner-only extension

**Spec:** User stories 20-21 (closed allowlist, deny-zones).

Steps:
```bash
cat ./data-accept/config.json | python3 -m json.tool | grep -A 20 allowlist
# Preseeded must include: x.com, reddit.com, youtube.com, github.com, news.ycombinator.com, arxiv.org, facebook.com, instagram.com, linkedin.com, tiktok.com, bsky.app, threads.net, mastodon.social, google.com
uv run idle-cua profile show --data-dir ./data-accept --json | python3 -m json.tool | grep -A 10 allowed_sites
# Try allowlist widening via policy engine (agent-originated must be rejected)
python3 - << 'PY'
from idlecua import IdleCua, IdleCuaConfig
from pathlib import Path
from idlecua.policy import TypedAction
app = IdleCua(config=IdleCuaConfig(data_dir=Path("./data-accept")))
try:
    app.policy.add_allowed_domain("evil.com")
    print("FAIL: allowlist widening not rejected")
except PermissionError as e:
    print(f"PASS: {e}")
PY
```
Expected:
- Preseeded allowlist present; only owner can extend via `profile edit` / `config.json` edit (agent call raises `PermissionError`).
- Deny-zones (`/messages`, `/inbox`, `/settings`, `/account`, `/password`, `/billing`, etc.) are hard-blocked even inside allowed domains.

Result: ________  Notes: ________

---

## 6. Dry-run — bounded typed plan, zero driver calls

**Spec:** User stories 10-11 (dry-run, bounded inspectable plan).

Steps:
```bash
uv run idle-cua plan "research recent AI papers on agents" --data-dir ./data-accept --json
uv run idle-cua run-once --dry-run "research recent AI papers on agents" --data-dir ./data-accept --json
python3 - << 'PY'
from pathlib import Path
from idlecua import IdleCua, IdleCuaConfig
from idlecua.contracts import FakeComputerDriver
driver = FakeComputerDriver()
app = IdleCua(config=IdleCuaConfig(data_dir=Path("./tmp-dry")), computer=driver)
plan = app.dry_run("research X on reddit")
print(f"actions={plan.expected_actions} target={plan.target} duration={plan.max_duration_minutes} max_actions={plan.max_actions}")
print(f"driver calls after dry-run: {len(driver.calls)} (expected 0)")
assert len(driver.calls)==0
PY
```
Expected:
- Plan contains: `goal`, `target` (one allowlisted domain), `expected_actions` (non-empty, closed vocabulary), `expected_result`, `max_duration_minutes` 1..45, `max_actions` 1..200, `risk_level`, `requires_confirmation`, plus per-action `action_verdicts` (allowlist → deny-zone → action class).
- `plan` and `run-once --dry-run` produce identical JSON.
- `driver.calls == 0` and `model.calls == 0` after dry-run.

Result: ________  Notes: ________

---

## 7. Real read-only task through Cua on main Chrome profile

**Spec:** User story 7 AC + issue #12 — browser on main Chrome profile, tab discipline, verify-by-reread, read-only first.

Steps:
```bash
# Ensure profile confirmed + browser consent granted
uv run idle-cua profile interview --yes --data-dir ./data-accept
uv run idle-cua profile grant-browser --browser chrome --data-dir ./data-accept
uv run idle-cua profile browser-status --data-dir ./data-accept
uv run idle-cua doctor --data-dir ./data-accept  # checks profile consent + driver grant + permissions

# Real smoke — host primitive (no network, fully reversible):
uv run idle-cua run-once --real-driver --data-dir ./data-accept "launch Calculator and verify its window is visible" --json

# Real browsing — read-only, owner-observed (Hacker News or Google):
uv run idle-cua run-once --real-driver --data-dir ./data-accept "Research Hacker News top stories and save links with short notes" --json
# Check only agent tabs were closed, owner tabs untouched:
python3 - << 'PY'
from pathlib import Path
from idlecua import IdleCua, IdleCuaConfig
from idlecua.drivers.cua_driver import CuaComputerDriver
cfg = IdleCuaConfig(data_dir=Path("./data-accept"))
driver = CuaComputerDriver(session="idlecua-verify", data_dir=cfg.data_dir)
print("agent_tabs after:", driver.get_agent_tabs())
print("journal:", driver.journal.log[-10:])
PY
uv run idle-cua report --data-dir ./data-accept
uv run idle-cua history --data-dir ./data-accept
```
Expected:
- Real driver init succeeds with embedded runtime; permission failures surface with `Remediation:` message and task fails (persisted to report, not silent).
- Browser task completes with `agent_tabs` == 0 after (agent tabs closed), owner tabs untouched; `close_all_agent_tabs` logged.
- Every significant browser action verified by `verify_browser_state` / `get_browser_state` reread; failures retried or recorded as `verify failed` in SQLite + report.
- Allowlist + deny-zones enforced: no navigation outside allowed domains, no deny-zone pages.

Result: ________  Notes: ________

---

## 8. Idle auto-start — hardware HID timer, synthetic never masks

**Spec:** User stories 14-15 — Spec AC 8 (manual): a task auto-starts after the idle period on the real machine.

Steps:
```bash
# Show Quartz HID detector probe (synthetic never masks)
python3 - << 'PY'
from idlecua.idle import QuartzIdleDetector, FakeIdleDetector
qd = QuartzIdleDetector()
print(f"Quartz use_quartz={qd._use_quartz} idle={qd.seconds_since_last_input():.1f}s screen_locked={qd.is_screen_locked()}")
fd = FakeIdleDetector(idle_seconds=1000, locked=False)
print(f"Fake idle={fd.seconds_since_last_input()} can_run(600)={fd.can_run(600)}")
PY

# Manual auto-start test (owner-observed):
# 1. Set idle threshold short for test (e.g., 30s) via config or --idle-threshold
uv run idle-cua status --data-dir ./data-accept
# 2. Leave machine idle ≥ threshold (do not move mouse/type), wait for HID timer
uv run idle-cua start --watch --once --idle-threshold 30 --poll-interval 2 --data-dir ./data-accept "research recent AI papers and save links" --json
# Alternative: direct API
python3 - << 'PY'
from pathlib import Path
from idlecua import IdleCua, IdleCuaConfig
from idlecua.idle import QuartzIdleDetector
cfg = IdleCuaConfig(data_dir=Path("./data-accept"), idle_threshold_seconds=30)
app = IdleCua(config=cfg, idle_detector=QuartzIdleDetector())
app.wait_for_idle(poll_interval=2, timeout=120)
print("Idle detected — would auto-start session here")
PY
```
Expected:
- `QuartzIdleDetector` uses `CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateHIDSystemState, kCGAnyInputEventType)` so synthetic `click`/`type_text` from `CuaComputerDriver` never resets the HID timer (documented spike; watchdog fallback if assumption fails).
- After idle ≥ threshold (default 10 min, test override 30s), a task auto-starts without manual trigger; `start --watch` polls HID every `--poll-interval` and respects screen-lock gate (locked → not start).
- Gates before any work: enabled, profile confirmed, schedule allows, idle ≥ threshold, screen unlocked, limits not reached, task allowed by policy.

Result: ________  Notes: ________

---

## 9. Stop on user return — halt input, paused_by_user, auto-resume only at next idle

**Spec:** User stories 15-17 — input halt + paused_by_user + saved state, no next step, auto-resume only at next idle.

Steps:
```bash
# Simulated via FakeIdleDetector hardware_input() (automated):
python3 - << 'PY'
from pathlib import Path
import tempfile
from idlecua import IdleCua, IdleCuaConfig
from idlecua.contracts import FakeComputerDriver
from idlecua.idle import FakeIdleDetector
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    det = FakeIdleDetector(idle_seconds=1000, locked=False)
    driver = FakeComputerDriver()
    # Profile gate: init + interview
    cfg = IdleCuaConfig(data_dir=td)
    app = IdleCua(config=cfg, computer=driver, idle_detector=det)
    from idlecua.profile.interview import run_interview
    run_interview(td / "profile.json", td, non_interactive=True, assume_yes=True)
    # Advance fake idle then simulate return mid-task: set idle to 0
    # This is exercised by TaskExecutor._check_user_return test; manual:
    print("Before: idle", det.seconds_since_last_input())
    det.hardware_input()
    print("After hardware_input idle", det.seconds_since_last_input(), "can_run", det.can_run(600))
    print("Expected: executor halts input, releases held keys/buttons via journal, transitions to paused_by_user")
PY

# Real machine: start a browsing task, then move mouse / press key during execution
uv run idle-cua start --watch --idle-threshold 30 --poll-interval 2 --data-dir ./data-accept "Research Hacker News top stories and save links" --json
# While running, move mouse / type — agent should halt immediately, release held inputs, show paused_by_user
uv run idle-cua status --data-dir ./data-accept --json  # should show paused_by_user
uv run idle-cua resume --data-dir ./data-accept  # only at next idle
```
Expected:
- On hardware input (real mouse/keyboard, HID), agent halts input synchronously (`release_all_inputs`), transitions task to `paused_by_user`, saves state, starts no next step.
- `status` shows `paused_by_user`; `resume` refuses until next idle period (`can_run` fails while not idle).
- Auto-resume only at next idle (spec AC 9) — not immediately while user present.

Result: ________  Notes: ________

---

## 10. Emergency stop — kill/SIGINT/SIGTERM, LLM-independent

**Spec:** User story 18 — hard off-switch via CLI and signals, LLM-independent.

Steps:
```bash
# CLI kill:
uv run idle-cua kill --data-dir ./data-accept --reason "manual test kill"
uv run idle-cua status --data-dir ./data-accept --json | python3 -m json.tool | grep -A 5 stop

# SIGINT (Ctrl-C) during watch:
uv run idle-cua start --watch --idle-threshold 30 --poll-interval 2 --data-dir ./data-accept "research X" --json &
sleep 5
kill -SIGINT $!
# Verify: held inputs released, task stopped, reason saved, no further driver calls
python3 - << 'PY'
from pathlib import Path
from idlecua import IdleCua, IdleCuaConfig
from idlecua.contracts import FakeComputerDriver
from idlecua.executor import request_emergency_stop, is_emergency_stop_requested
import tempfile, time
driver = FakeComputerDriver()
driver.hold_for_test("Shift","left")
print("held before:", driver.held_keys, driver.held_buttons)
request_emergency_stop("test SIGINT")
print("emergency flag:", is_emergency_stop_requested())
driver.release_all_inputs()
print("held after:", driver.held_keys, driver.held_buttons)
PY
```
Expected:
- `kill`, `stop` (alias), `SIGINT`, `SIGTERM` all route through same path: cancel active task → `stopped`, release held keys/buttons via `InputJournal` synchronously, terminate only agent-started processes (owner tabs untouched), persist `last_stop_reason`, never wait for LLM call to complete.
- Focused test: emergency stop mid-task → task stopped, journal drained, zero further driver calls, works while fake LLM call in flight.

Result: ________  Notes: ________

---

## 11. Markdown daily report — tasks, queries, findings, skipped repeats, errors, limits

**Spec:** User story 31 — Markdown report after each session.

Steps:
```bash
uv run idle-cua report --data-dir ./data-accept
uv run idle-cua report --data-dir ./data-accept --json | python3 -m json.tool | head -100
ls -la ./data-accept/reports/
cat ./data-accept/reports/$(date -u +%F).md | head -200
python3 - << 'PY'
from pathlib import Path
cfg = Path("./data-accept")
daily = cfg / "reports" / f"{__import__('datetime').datetime.now(__import__('datetime').timezone.utc).date().isoformat()}.md"
print("Daily exists:", daily.exists())
if daily.exists():
    print(daily.read_text()[:2000])
PY
```
Expected:
- Per-task file `reports/<task_id>.md` plus daily file `reports/YYYY-MM-DD.md` (appended, deduplicated by task_id).
- Report sections present: Tasks done (goal/target/result/risk), Queries used (normalized), Best findings with links, Relevance to profile, Skipped repeats (7-day window), Errors, Unfinished actions, Actions executed, URLs visited, Limit usage (actions/duration/LLM calls, stopped_due_to_limit).
- Graceful limit reporting (`stopped_due_to_limit`) when caps hit.

Result: ________  Notes: ________

---

## 12. History — queries and visited URLs, auditability

**Spec:** User story 32 — CLI-accessible history of queries and visited URLs.

Steps:
```bash
uv run idle-cua history --data-dir ./data-accept
uv run idle-cua history --data-dir ./data-accept --json | python3 -m json.tool | head -200
python3 - << 'PY'
from pathlib import Path
from idlecua import IdleCua, IdleCuaConfig
cfg = IdleCuaConfig(data_dir=Path("./data-accept"))
app = IdleCua(config=cfg)
h = app.get_history(limit=10)
print(f"queries={len(h['queries'])} urls={len(h['urls'])} tasks={len(h['tasks'])}")
for q in h['queries'][:3]:
    print(f"  query: {q['query']!r} normalized={q['normalized']!r}")
PY
sqlite3 ./data-accept/memory.db "SELECT COUNT(*) FROM queries; SELECT COUNT(*) FROM urls; SELECT COUNT(*) FROM findings;"
```
Expected:
- `history` lists recent queries (normalized) and URLs (fingerprinted) from SQLite.
- DB tables `tasks`, `actions`, `queries`, `urls`, `findings`, `errors`, `reports`, `kv` populated; machine-readable via `get_history(limit)`.

Result: ________  Notes: ________

---

## 13. Repeat avoidance — 7-day normalized query/URL/plan dedupe

**Spec:** User story 13 — 7-day window suppresses exact duplicate normalized queries, URL fingerprints, plan fingerprints; no embeddings.

Steps:
```bash
python3 - << 'PY'
from pathlib import Path, tempfile
from idlecua import IdleCua, IdleCuaConfig
from idlecua.contracts import FakeComputerDriver
from idlecua.dedup import normalize_query, url_fingerprint, plan_fingerprint
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    # Setup confirmed profile
    from idlecua.profile.interview import run_interview
    run_interview(td / "profile.json", td, non_interactive=True, assume_yes=True)
    app = IdleCua(config=IdleCuaConfig(data_dir=td), computer=FakeComputerDriver())
    # First run
    r1 = app.run_once("research AI agents on x.com", dry_run=False)
    print("run1 queries:", len(r1.queries), "urls:", len(r1.urls), "skipped:", r1.skipped_repeats)
    # Second run with same description — should skip repeat
    r2 = app.run_once("research AI agents on x.com", dry_run=False)
    print("run2 skipped:", r2.skipped_repeats)
    print("report contains Skipped repeats:", "Skipped repeats" in r2.report_markdown)
    assert any(s["type"]=="query" for s in r2.skipped_repeats) or any(s["type"]=="plan" for s in r2.skipped_repeats), "repeat not detected"
    print("PASS: repeat avoidance works")
PY
uv run idle-cua history --data-dir ./data-accept --json | python3 -m json.tool | grep -E "normalized|fingerprint" | head -20
uv run idle-cua report --data-dir ./data-accept | grep -A 10 "Skipped repeats"
```
Expected:
- First run records normalized query/URL/plan fingerprint in SQLite.
- Second run with same exact (normalized) query/URL/plan within 7 days is skipped; `skipped_repeats` list populated with type/value/reason and surfaced in report's `## Skipped repeats` section and as errors in SQLite.
- Fresh/new queries/URLs are allowed.

Result: ________  Notes: ________

---

## 14. Public API — programmatic usage without CLI

**Spec:** User stories 34-35 — `IdleCua` / `IdleCuaConfig`, `ComputerDriver`/`ModelProvider` contracts with fakes, status/history accessors.

Steps:
```bash
python3 - << 'PY'
from pathlib import Path, tempfile
from idlecua import IdleCua, IdleCuaConfig
from idlecua.contracts import FakeComputerDriver, FakeModelProvider
with tempfile.TemporaryDirectory() as td:
    cfg = IdleCuaConfig(data_dir=Path(td))
    driver = FakeComputerDriver()
    app = IdleCua(config=cfg, computer=driver, model_provider=FakeModelProvider(response="hi"))
    # Profile required for run_task
    from idlecua.profile.interview import run_interview
    run_interview(Path(td) / "profile.json", Path(td), non_interactive=True, assume_yes=True)
    task = app.create_task("research recent AI papers on agents")
    plan = app.dry_run(task.description)
    print("plan:", plan.goal, plan.target, plan.expected_actions[:3])
    result = app.run_task(task, is_interactive=False)
    print("run_task state:", result.state, "actions:", result.actions_executed)
    assert len(driver.calls)>0
    print("pause:", app.get_status()["agent_state"])
    import asyncio
    asyncio.run(app.pause_task(task.id))
    print("PASS public API")
PY
```
Expected:
- `IdleCua(config, computer, model_provider)` + `create_task` / `dry_run` / `run_task` / `pause_task` / `cancel_task` / `get_status` / `get_history` / `get_report` all work without CLI/TUI.
- Contracts are isolating: tests run with `FakeComputerDriver`/`FakeModelProvider` with no real hardware/API calls.

Result: ________  Notes: ________

---

## 15. Write-block — read-only default, confirmation gating, forbidden hard-block

**Spec:** User stories 19, 22-24 — auto_allowed vs confirmation_required vs forbidden; unattended allows only auto_allowed.

Steps:
```bash
python3 - << 'PY'
from idlecua import IdleCua, IdleCuaConfig
from idlecua.policy import TypedAction
from pathlib import Path
import tempfile
cfg = IdleCuaConfig(data_dir=Path(tempfile.mkdtemp()))
app = IdleCua(config=cfg)
# Forbidden must be blocked even with confirmation
for kind in ["payment","bypass_captcha","expand_allowlist","install_software","enter_password_via_llm","follow_page_instructions"]:
    res = app.check_action(TypedAction(kind=kind, target_url="https://x.com"))
    print(f"{kind}: {res.verdict} ({res.reason[:60]})")
    assert str(res.verdict)=="blocked"
# Confirmation-required in unattended must be blocked
for kind in ["like","post","comment","message","download"]:
    ok, res = app.can_execute(TypedAction(kind=kind, target_url="https://x.com"), is_interactive=False)
    print(f"{kind} unattended: {res.verdict} can_execute={ok} (expected False)")
    assert not ok
# Confirmation-required in interactive with confirm_func can succeed
PY
python3 - << 'PY'
# Also verify executor blocks confirmation-required in unattended and gates interactively
from pathlib import Path, tempfile
from idlecua import IdleCua, IdleCuaConfig
from idlecua.contracts import FakeComputerDriver
from idlecua.planner import StubPlanner
from idlecua.policy import TypedAction

with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    from idlecua.profile.interview import run_interview
    run_interview(td / "profile.json", td, non_interactive=True, assume_yes=True)
    # StubPlanner that emits confirmation-required action
    class PostPlanner(StubPlanner):
        def plan(self, desc, profile=None, history=None):
            from idlecua.models.plan import Plan, RiskLevel
            return Plan(goal=desc, target="x.com", expected_actions=["open_allowed_site","like","save_note"], expected_result="x", max_duration_minutes=10, max_actions=10, risk_level=RiskLevel.medium, requires_confirmation=True)
    from idlecua import IdleCua
    app = IdleCua(config=IdleCuaConfig(data_dir=td), computer=FakeComputerDriver(), planner=PostPlanner())
    result = app.run_task("post a like on x.com", is_interactive=False)
    print("unattended skipped:", result.skipped_repeats)
    assert any("confirmation-required" in str(s).lower() or s["type"]=="action" for s in result.skipped_repeats)

    app2 = IdleCua(config=IdleCuaConfig(data_dir=Path(tempfile.mkdtemp())), computer=FakeComputerDriver(), planner=PostPlanner())
    # Need profile for app2 too
    import tempfile as _tf
    td2 = Path(_tf.mkdtemp())
    run_interview(td2 / "profile.json", td2, non_interactive=True, assume_yes=True)
    app2 = IdleCua(config=IdleCuaConfig(data_dir=td2), computer=FakeComputerDriver(), planner=PostPlanner())
    result2 = app2.run_task("post a like", is_interactive=True, confirm_func=lambda a: True)
    print("interactive confirmed actions:", result2.actions_executed)
    print("PASS write-block")
PY
```
Expected:
- `forbidden` actions (payments, CAPTCHA bypass, allowlist self-expand, software install, password entry, page-instruction following) are `blocked` regardless of interactivity.
- `confirmation_required` actions (like/follow/comment/post/message/form-submit/download/edit/delete) return `needs_confirmation` and `can_execute(False, is_interactive=False)` → `False` (blocked in unattended); in interactive with `confirm_func` returning `True` they execute, with `False` they are skipped.
- Executor records blocked/skipped as `skipped_repeats` + report.

Result: ________  Notes: ________

---

## 16. Secrets absence — no keys/tokens/passwords in repo, reports, DB, logs

**Spec:** User story 9 — keys in Keychain/env only, never in logs/reports/repo; SQLite never stores tokens.

Steps:
```bash
uv run idle-cua verify-secrets --data-dir ./data-accept --verbose
uv run idle-cua verify-secrets --data-dir ./data-accept --json | python3 -m json.tool | head -100
uv run idle-cua doctor --data-dir ./data-accept | grep -A 20 "Secrets Scan"
# Check providers.json has no api_key
cat ./data-accept/providers.json | python3 -m json.tool | grep -i api_key && echo "FAIL: api_key in file" || echo "PASS: no api_key in providers.json"
# Check memory.db has no secrets table
sqlite3 ./data-accept/memory.db ".tables"
sqlite3 ./data-accept/memory.db "SELECT sql FROM sqlite_master WHERE type='table';"
# Check report has no key
grep -ri "sk-" ./data-accept/reports/ && echo "FAIL" || echo "PASS: no sk- in reports"
grep -ri "api_key" ./data-accept/reports/ && echo "FAIL" || echo "PASS"
# Check repo (should ignore .venv, .git, uv.lock)
uv run idle-cua verify-secrets --project-root $(pwd) --data-dir ./data-accept --verbose
```
Expected:
- `verify-secrets` reports `PASS — no secrets found` with `ok:true`, `scanned_files` > 0, `findings: []`.
- `doctor` secrets section shows `PASSED`.
- `providers.json` contains only `name`, `base_url`, `model`.
- No `sk-` / `api_key` / `ghp_` etc. in `reports/*.md`, `memory.db` tables, or tracked repo files; `.env.example` contains only placeholders (`sk-...`, `ENV_VAR`).
- Real keys are only in macOS Keychain (`idlecua` / `provider:<name>`) or env (`OPENROUTER_API_KEY`, `OPENCODE_GO_API_KEY`), never committed.

Result: ________  Notes: ________

---

## End-to-End Real Session (Owner-Verified, Issue #13 Acceptance)

**Spec AC:** Spec AC 8 (manual): a task auto-starts after the idle period on the real machine; daily Markdown report is produced; history shows queries/URLs; next run avoids repeats; secrets absent verified.

Steps (real machine, all gates — use `./data-accept` or `~/.idlecua`):
```bash
uv run idle-cua init --data-dir ~/.idlecua
uv run idle-cua profile interview --data-dir ~/.idlecua  # confirm
uv run idle-cua profile grant-browser --browser chrome --data-dir ~/.idlecua
uv run idle-cua models add --name opencode-go --base-url https://opencode.ai/zen/go/v1 --model anthropic/claude-3.5-sonnet --api-key-env OPENCODE_GO_API_KEY --data-dir ~/.idlecua
uv run idle-cua doctor --data-dir ~/.idlecua
uv run idle-cua plan "research latest AI agent papers on arxiv" --data-dir ~/.idlecua --json
# Leave machine idle ≥ threshold (default 10 min, or --idle-threshold 60 for test), then watch auto-start
uv run idle-cua start --watch --once --idle-threshold 60 --poll-interval 5 --data-dir ~/.idlecua "research latest AI agent papers on arxiv and save links with short notes" --json
# After completion:
uv run idle-cua report --data-dir ~/.idlecua
uv run idle-cua history --data-dir ~/.idlecua --json | python3 -m json.tool | head -100
ls -la ~/.idlecua/reports/
cat ~/.idlecua/reports/$(date -u +%F).md | head -300  # daily report
# Next run with same query should be skipped
uv run idle-cua run-once --data-dir ~/.idlecua "research latest AI agent papers on arxiv" --json | python3 -c "import json,sys; d=json.load(sys.stdin); print('skipped_repeats', d.get('skipped_repeats'))"
uv run idle-cua verify-secrets --data-dir ~/.idlecua --verbose
```

Expected:
- After idle period, task auto-starts without manual trigger; plan → policy (allowlist → deny-zone → action class) → real driver (main Chrome profile, agent tabs only, verify-by-reread) → SQLite (queries/URLs/findings) → daily Markdown report (all sections) → graceful stop on return/limits/emergency stop.
- Daily report file `reports/YYYY-MM-DD.md` exists and contains the session; history shows queries/URLs; next exact repeat is avoided (skipped_repeats non-empty, reported).
- `verify-secrets` passes after real session.
- Tab discipline: `agent_tabs` closed after, owner tabs untouched.

Result: ________  Notes: ________

---

## Recording Results on Issue #13

After executing all 16 criteria:

1. Update this file with `PASS`/`FAIL` per criterion and notes (especially for manual real-machine steps with screenshots/timestamps).
2. For automated sweep, run:
   ```bash
   python scripts/acceptance_sweep.py --data-dir ./data-accept --json | tee acceptance_result.json
   python scripts/acceptance_sweep.py --data-dir ./data-accept --verbose | tee docs/acceptance_result.md
   ```
3. Comment or attach `acceptance_result.json` to GitHub issue #13:
   ```bash
   gh issue comment 13 --body "$(cat docs/acceptance_result.md)"
   # Or upload artifact and link
   ```
4. If any criterion is `FAIL`, fix only within MVP scope; beyond-scope items are recorded in `## Beyond Scope` below, not silently expanded.

### Beyond Scope (record, do not expand)

List anything observed during sweep that is beyond MVP scope (GUI, hotkey, Lume, second driver, embeddings, etc.):

- ________

---

*Generated for IdleCUA #13 — Full autonomous idle session + daily report + acceptance sweep. Fix only what the sweep exposes within scope.*

