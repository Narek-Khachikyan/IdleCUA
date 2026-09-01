# ADR-0001: Locked MVP scope — policy-gated idle-time computer-use agent

Date: 2026-09-01
Status: Accepted

## Context

IdleCUA is a local Python application (SDK + CLI) that runs a policy-gated computer-use agent on the owner's real Mac only while idle. The owner leaves the machine idle for long stretches while useful research goes undone; existing Cua tooling provides raw control primitives but no idle gating, return detection that ignores synthetic input, personalization, safety policy, limits, or reporting. The core risk is an unsupervised agent fighting the user for input, wandering outside allowed sites, or performing irreversible actions on the owner's main accounts.

Two rounds of grilling locked the MVP boundary. This ADR records the accepted scope so future work can be checked against it.

## Decision

Build the MVP as a **single-package, src-layout Python 3.13 application** managed with `uv`, with a thin `typer`+`rich` CLI that calls a public `Application` API. No business logic lives in the CLI.

### Locked scope

- **Onboarding interview** (scripted questionnaire, works before any provider is configured) produces a confirmed profile; no autonomous action runs before explicit profile confirmation. Profile is stored locally and editable/validatable outside onboarding.
- **Idle gating** via macOS Quartz HID hardware-event idle timer (not combined session state) so synthetic input never masks return. Gates: enabled, profile confirmed, schedule allows, idle ≥ 10 min, screen unlocked, machine healthy, limits not reached, task allowed by policy. On return: halt input, release held keys/buttons, transition to `paused_by_user`, wait for next idle. Resume only on next idle or explicit command.
- **PolicyEngine** evaluated per action in strict order: closed allowlist → deny-zones → action class. Preseeded allowlist: `x.com`, `reddit.com`, `youtube.com`, `github.com`, `news.ycombinator.com`, `arxiv.org`, `facebook.com`, `instagram.com`, `linkedin.com`, `tiktok.com`, `bsky.app`, `threads.net`, `mastodon.social`, `google.com`. Deny-zones inside allowed sites: DMs/chats, account/settings, password/2FA/billing, re-auth, notifications. Only the owner can mutate policy.
- **Plan → typed actions**: every session starts from a bounded structured plan (`goal`, link to profile, `target` site/app, `expected_actions`, `expected_result`, `max_duration`, `max_actions`, `risk_level`, confirmation needs) converted into a closed typed-action vocabulary. LLM never executes free-form plans. Unattended mode allows only `auto_allowed` actions; `confirmation_required` actions run only interactively after a prompt disclosing action/target/payload/consequences; `forbidden` actions are hard-blocked (payments, CAPTCHA bypass, automation masking, bulk engagement, private-data harvesting, etc.).
- **Contracts** are the only seams: `Application` (public SDK), `ComputerDriver` (one real Cua implementation, fake for tests), `ModelProvider` (one OpenAI-compatible HTTP adapter, fake for tests), plus `PolicyEngine`, `MemoryStore` (single local SQLite), `IdleDetector`, `Planner`.
- **Browser**: owner's main Chrome profile via driver attachment; agent opens/closes only its own tabs; never touches owner tabs/windows or browser chrome (logout/profile switching). Chromium-only is an accepted constraint. Trusted-input limits compensated by verify-by-reread. Pacing + per-site caps mitigate anti-bot risk on fragile sites (X/Instagram/LinkedIn/Facebook) with first-tier targets `X`, `Reddit`, `YouTube`.
- **Anti-repeat**: 7-day window on normalized queries, URL fingerprints, and plan fingerprints; no embeddings.
- **Limits**: session ≤ 45 min, ≤ 200 actions/session, ≤ 150 LLM calls/day; graceful completion with saved results.
- **Emergency stop**: single path for SIGINT/SIGTERM and CLI kill — cancel task, release input from journal, terminate only agent-started processes, persist stop reason, never wait for an LLM call.
- **Storage**: one local SQLite DB (tasks, actions, queries, URLs, findings, statuses, confirmations, errors, limit usage). Never stored: API keys/tokens/passwords/cookies/raw sensitive provider payloads. API keys in system credential store, never in logs/reports/repo.
- **Vision model** required; configured via OpenRouter or OpenCode Go gateway.

### Walking skeleton (issue #2)

The first vertical slice, deliverable without LLM or driver integration:

- Installable `uv` package, `idle-cua` CLI, README stub, `.env.example` without secrets.
- `idle-cua init` creates the data directory and default config; `idle-cua plan <task>` and `idle-cua run-once --dry-run <task>` use a deterministic stub planner to print a bounded typed plan and perform zero computer actions.
- Public `IdleCua`/`IdleCuaConfig` with `Task` and a plain validated state machine (`disabled`, `waiting_for_idle`, `planning`, `running`, `paused_by_user`, `paused_for_approval`, `completed`, `failed`, `stopped`).
- `ComputerDriver` and `ModelProvider` as small interfaces with fake implementations for tests.

### Explicitly out of scope for MVP

ChatGPT browser-login for model auth; local HTTP/MCP/REST/WebSocket/SSE/gRPC surfaces; GUI/Electron/Tauri; global hotkey listener; Lume VM isolation; second driver/browser adapter; additional native LLM providers; reuse of Cua's bundled agent loop; embeddings/vector DB/novelty scoring; plugin marketplace/DI/event sourcing/microservices/queues/multiple DBs; profile version history; multi-user/cloud sandboxes.

## Consequences

- Implementation order is vertical: Application → PolicyEngine → ComputerDriver (fake first) → Result/Report → onboarding → idle detection → SQLite → anti-repeat → ModelProvider → real Cua driver.
- Focused tests only, against `FakeComputerDriver`/`FakeModelProvider` via the public API; real-site/user-account testing is forbidden in automation. Acceptance-critical coverage: return-stop, emergency stop, write-block, repeat prevention, programmatic run. The rest is verified by a manual owner checklist.
- Cua is pinned to current stack (not legacy `computer` package); Quartz HID assumption gets a day-1 spike with an input-watchdog fallback.
- Future proposals that expand scope (GUI, hotkey, Lume, second driver, embeddings) require a new ADR and owner sign-off.

## References

- Issue #1 — IdleCUA MVP spec (canonical)
- Issue #2 — Walking skeleton acceptance criteria
