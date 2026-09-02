# ADR-0005: Serve stack — FastAPI + Jinja2/htmx, /api/v1 as a first-class contract

Date: 2026-09-01
Status: Accepted

## Context

ADR-0002 fixed the shape (one serve process, HTTP API + Local UI) but not the stack or contract discipline. The owner wants the API itself as a daily surface (scripts, shortcuts, other tools), and the repo is Python/uv-only with proportionality rules in CODING_STANDARDS.md.

## Decision

- **API**: FastAPI + uvicorn, JSON, mounted under `/api/v1`, with the OpenAPI schema and interactive docs exposed. `/api/v1` is a stable, first-class contract: breaking changes only via `/api/v2`. Default bind `127.0.0.1:8000`, configurable with `--port`; never a non-loopback bind in v1.
- **Local UI**: server-rendered Jinja2 templates + htmx (vendored static asset); no Node toolchain, no SPA build. The dashboard refreshes via htmx polling (3–5 s); live session streaming (v2) will use SSE. If the UI outgrows this, a separate ADR introduces a SPA. UI language is English, consistent with CLI output and reports.
- **Tests**: the API is tested through httpx `TestClient` against the existing fakes (`FakeComputerDriver`, `FakeModelProvider`, `FakeIdleDetector`); the UI gets route smoke tests only. No browser automation, no real sites in automation — ADR-0001's testing rule extends to the new layer.

## Consequences

- New runtime deps: `fastapi`, `uvicorn`, `jinja2`. All server code calls the same Application API as the CLI; no business logic in the server layer.
- Provider-key endpoints accept a key on write and return only a masked value (last characters) on read; secrets never appear in API responses, logs, or the OpenAPI schema. The existing secrets scan covers the new surface.

## v1 endpoint map (accepted)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/status` | agent state, idle seconds, watch-loop state, daily limit usage, last report |
| GET/POST | `/api/v1/tasks` | list / create task (goal description) |
| POST | `/api/v1/tasks/{id}/plan` | dry-run Plan preview, zero actions |
| POST | `/api/v1/tasks/{id}/run` | run-once now (idle-gated; refused while a session is in flight) |
| GET | `/api/v1/tasks/{id}` | task detail + execution result |
| GET | `/api/v1/history` | actions/queries/urls/findings/errors, filterable by task |
| GET | `/api/v1/reports`, `/api/v1/reports/{task_id}` | Markdown report bodies |
| GET | `/api/v1/diagnostics` | doctor checks: macOS permissions, driver probe, profile validity, secrets scan |
| GET/PATCH | `/api/v1/settings` | Profile owner-intent fields + `readonly`/`require_idle` toggles; ceilings read-only |
| GET/POST/DELETE | `/api/v1/providers` (+ `/{name}/test`, `/{name}/select`) | provider CRUD; key accepted on write, masked on read |
| POST | `/api/v1/scheduler/start`, `/api/v1/scheduler/stop` | watch-loop control (server mode) |
| POST | `/api/v1/emergency-stop` | emergency stop |

`providers/{name}/test` performs one minimal real `ModelProvider` call and reports ok/error.

## References

- ADR-0002 (shape), ADR-0001 (testing rule)
