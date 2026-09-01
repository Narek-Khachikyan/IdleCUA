# CONTEXT.md - IdleCUA Domain Glossary

## Core Concepts

- **IdleCUA**: Local Python application that runs a policy-gated computer-use agent during idle periods.
- **Profile**: Confirmed user configuration covering characteristics, computer usage, and autonomy boundaries. Machine-readable JSON plus derived human rendering.
- **Confirmed / Unconfirmed**: Profile state gate. No autonomous action while unconfirmed.
- **PolicyEngine**: Layered gate: allowlist -> deny-zones -> action classes (auto-allowed / confirmation-required / forbidden).
- **ComputerDriver**: Small contract for host control (screenshot, a11y tree, mouse/keyboard, browser). One real impl over Cua, one fake for tests.
- **ModelProvider**: One OpenAI-compatible adapter (OpenRouter, OpenCode Go gateway).
- **IdleDetector**: macOS Quartz HID idle timer; synthetic input must not reset timer.
- **Task / Plan / Action**: Task description -> bounded typed plan -> typed actions executed step-by-step.
- **MemoryStore**: Single local SQLite database for tasks, actions, queries, URLs, findings.

## Agent States

`disabled, waiting_for_idle, planning, running, paused_by_user, paused_for_approval, completed, failed, stopped`

Valid transitions are enforced by the state model.
