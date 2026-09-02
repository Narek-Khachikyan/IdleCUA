# ADR-0004: The serve process owns the watch loop; one scheduler per data dir

Date: 2026-09-01
Status: Accepted

## Context

`idle-cua start --watch` today runs the idle watch loop as a blocking loop inside the CLI process. With ADR-0002's long-lived serve process, two processes could each run a scheduler against the same data dir: duplicated sessions, racing SQLite writes, and two agents fighting for one Chrome profile.

## Decision

The serve process hosts the watch loop: the Local UI starts and stops watching through the HTTP API, and the synchronous `IdleScheduler` loop runs as a background worker inside the serve process. `idle-cua start --watch` remains a first-class headless mode. Exactly one scheduler may run per data dir, enforced by a scheduler lock in the data dir — a second start (CLI or server) fails fast with a clear "already running in process X" error instead of racing. Exactly one session may execute at a time per data dir: one-shot runs (`run-once` via CLI or API) are rejected while a session is in flight. Emergency stop works from either side: the UI hits the serve process directly; `idle-cua kill` keeps its current path.

Server-mode sessions run unattended: `confirmation_required` actions are always skipped-and-surfaced, interactive approval remains CLI-only (`--interactive`), and an approval queue arrives only with the v2 live monitor.

## Consequences

- The serve process is stateful: the dashboard shows live scheduler state from the process it lives in, not state reconstructed from SQLite.
- The scheduler lock must be crash-safe (stale-lock detection by PID liveness) so a killed serve process cannot wedge the data dir.
- The in-flight check likewise guards one-shot runs against a watch-loop session already executing.

## References

- ADR-0002 (serve process shape)
