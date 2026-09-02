# ADR-0002: Local Web UI and HTTP API

Date: 2026-09-01
Status: Accepted (supersedes the GUI / local-server exclusion in ADR-0001)

## Context

ADR-0001 explicitly excluded GUI and local HTTP/MCP/REST surfaces to keep the MVP skeleton minimal, and required a new ADR plus owner sign-off for any such expansion. The owner now wants day-to-day control — composing tasks, previewing plans, reading reports, tuning settings, stopping the agent — without memorizing CLI flags, and wants the surface to be scriptable.

## Decision

Add one local control surface: **`idle-cua serve`** starts a single long-lived process bound to `127.0.0.1` that (a) exposes a versioned JSON **HTTP API** and (b) serves the **Local UI** as static assets from the same process. The CLI, the HTTP API, and the Local UI are all thin callers of the Application API (`IdleCua`); no business logic moves out of it. The CLI remains a first-class client.

- **Localhost-only, single owner.** No auth token or TLS in v1; remote/network access requires a new ADR.
- **The Local UI is zero-friction and UI-first.** Conservative preseeded defaults, one-click confirmation of the default Profile, a guided getting-started checklist (profile confirm, provider key, macOS permissions, browser consent); nothing requires reading docs or touching the CLI first. The terminal remains a fully capable second client, but daily use never requires it — everything the owner needs day-to-day is visible and doable in the Local UI. Dev flags, interactive confirmations (`--interactive`), and user-return simulation (`pause`/`resume`) stay CLI-only as non-daily tools.
- **`idle-cua serve` auto-initializes** the data dir with defaults (idempotent, like `init`) and opens the browser on the Local UI; `--no-open` disables auto-open.
- **Demo mode is explicit.** With no provider key configured, sessions run on the deterministic stub planner; every surface badges this as Demo mode rather than presenting stub output as LLM work.
- **Provider API keys stay only in the system Keychain.** The Local UI shows masked values with a test button, never the secret.
- **v1 Local UI scope**: dashboard (status/idle/limits), getting-started checklist, task composer with dry-run plan preview, history/reports browser, settings, diagnostics (permissions, driver probe, profile validity, secrets scan), emergency stop. Live session streaming, the full profile editor, and an approval queue are v2.
- **Settings surfaced in the UI**: providers, session/action/LLM limits, idle threshold, `readonly`, `require_idle`, allowlist, deny-zones, allowed hours, browser consent. Dev flags (`use_real_driver`, `use_real_idle`, `data_dir`) stay CLI/env-only.
- **MCP server and multi-user auth remain out of scope.**

## Consequences

- "Session" keeps its ADR-0001 meaning (one bounded agent run). A UI login timeout, if ever needed, gets a separate term ("UI session"), never "session".
- Scheduler ownership is decided in ADR-0004; the authoritative write target for UI-editable settings in ADR-0003. The implementation stack and API contract are decided in ADR-0005.
- Secrets scan must extend to anything the HTTP API could leak (masked key endpoints, OpenAPI schemas, error responses).

## References

- ADR-0001 (locked MVP scope; superseded exclusion)
