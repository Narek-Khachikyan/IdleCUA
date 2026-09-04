# ADR-0006: One deep module owns the Task lifecycle

Date: 2026-09-04
Status: Accepted

## Context

Task and Session lifecycle decisions are currently spread across `IdleCua`, `TaskExecutor`, `IdleScheduler`, the HTTP API, and the serve Watch loop. Those callers independently reconstruct Tasks, mutate `AgentState`, inspect storage, enforce active-Session guards, interpret queue-time decisions, and persist stop or failure outcomes. This leaks lifecycle implementation across seams, permits different decisions for the same Session, and conflicts with ADR-0002 and ADR-0005's requirement that the HTTP API remain a thin caller of the Application API.

## Decision

Introduce one internal deep `TaskLifecycle` module in `task_lifecycle.py`. Its interface has two entry points: `handle(command)` for mutation and `inspect(query)` for observation. `IdleCua` remains the public Application API; the CLI, HTTP API, and Watch loop remain adapters and never mutate lifecycle storage directly.

`handle` accepts a closed set of typed commands: `Enqueue`, `Start`, `Cancel`, and `EmergencyStop`. `Start` carries an explicit `unattended` or `interactive` mode and also resumes a paused Task. `inspect` accepts typed `GetTask`, `ListTasks`, and `GetActive` queries. Expected failures return an immutable structured outcome with a stable category, Task identity, `AgentState`, safe message, retryability, and failed gate where applicable; raw dependency errors do not cross the seam.

Ad-hoc Plan preview remains planning behavior behind the Application API because it creates no Task and changes no lifecycle state. A real Plan is created on the first Start, persisted before its first Action, and then remains immutable.

## Invariants

- One Task has exactly one Session and one final Report.
- An enqueued Task starts in `waiting_for_idle`; `disabled` applies to the Agent, not a Task.
- `completed`, `failed`, and `stopped` are terminal. A later attempt creates a new Task.
- Resume uses the persisted Plan and continues after the last confirmed Action. Session limits are cumulative; waiting and paused time do not consume the duration limit.
- Only `TaskLifecycle` changes Task state, owns the per-data-dir active-Session lease, and persists critical checkpoints. A failed critical write prevents the next Action.
- An Action is persisted as started before dispatch. Completion and cursor advance are persisted after confirmation. An interruption between dispatch and confirmation produces `outcome_unknown`, fails the Task, and is never retried automatically.
- The Watch loop resumes the oldest paused Task before starting the oldest queued Task; each group is FIFO.
- Queue-time skip applies to every Action of a selected type. Queue-time approval is rejected in v1. Unattended Sessions skip `confirmation_required` Actions; only interactive CLI execution can request approval.
- Cancellation affects one queued or paused Task. Emergency stop is idempotent, disables the Watch loop, stops the active Task when present, releases held input, and stops only Agent-started processes. Both end in `stopped` but retain distinct structured causes.
- A Report must be persisted before a Task becomes `completed`.

## Internal modules and seams

The existing `TaskExecutor` seam is replaced by an internal `ActionRunner` module that dispatches and verifies one prepared Action and returns a typed outcome. `TaskLifecycle` owns planning flow, transitions, Plan progress, limits, persistence, and Report completion.

`IdleScheduler` retains only polling, timing, Watch loop control, and the process lock required by ADR-0004. It sends an idle-triggered Start command and does not select Tasks, inspect SQLite, evaluate lifecycle gates, or construct `IdleCua`.

`MemoryStore` remains the single local SQLite adapter. Existing real and fake `ComputerDriver`, `ModelProvider`, and `IdleDetector` adapters remain the test seams; no new database, event bus, or remote interface is introduced.

## Persistence migration

Use `PRAGMA user_version` and a transactional additive migration. Extend the Task record with Plan progress, cumulative active duration, queue-time skipped Action types, last outcome, and stop or failure cause. Add one singleton active-Session lease table containing Task identity, PID, and acquisition time; a lease is stale only when its PID no longer exists.

During migration, legacy `disabled` Tasks become `stopped` with cause `legacy_not_queued`; stale `planning` or `running` Tasks become `failed` with cause `interrupted_unknown`. Existing queued, paused, and terminal Tasks retain their meaning. Migration failure rolls back and prevents Agent execution; existing rows are never destructively rebuilt.

## Consequences

The Application API changes deliberately: `create_task()` returns a Task in `waiting_for_idle`, with no compatibility path back to Task-level `disabled`. The versioned HTTP API retains its paths, status codes, and response shapes; non-empty queue-time approvals return a safe client error.

This ADR supersedes only ADR-0001's walking-skeleton semantics that a new Task begins in `disabled` and that a terminal Task may be recycled for a later cycle. ADR-0001's safety contracts and the Agent-level meaning of `disabled` remain in force.

Tests move to the `TaskLifecycle` interface with temporary SQLite and the existing fake adapters. Application API tests verify delegation, HTTP tests verify only stable outcome mapping, and critical end-to-end flows remain. Tests of removed private helpers, duplicate gate paths, and reactivation of terminal Tasks are replaced rather than layered onto the new test surface.

Implementation proceeds as replace-only vertical slices: persistence and inspection; enqueue and cancellation; start, checkpoints, and resume; Emergency stop; caller migration; then deletion of the old state mutations and guards. No feature flag or dual lifecycle authority is introduced.

## References

- ADR-0001 — locked MVP scope and safety contracts
- ADR-0003 — one authority per setting
- ADR-0004 — one Watch loop and one active Session per data dir
- ADR-0005 — stable HTTP API contract and thin callers
