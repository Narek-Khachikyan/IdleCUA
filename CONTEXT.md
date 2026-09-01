# CONTEXT — IdleCUA

Single-context glossary for the IdleCUA domain. Use these terms verbatim in code, issues, and docs; avoid synonyms not listed here.

## Core concepts

- **Agent**: the autonomous loop that executes typed actions through the `ComputerDriver` under policy gates. Never acts while the owner is present.
- **Application API**: the public Python surface (`IdleCua`, `IdleCuaConfig`). CLI and any embedding call through it; no business logic lives in the CLI.
- **Task**: a user-described goal (e.g., "research X") that becomes a bounded `Plan` and then a sequence of typed `Action`s. Has an `AgentState`.
- **Plan**: bounded, inspectable structure produced by the Planner before any execution. Fields: `goal`, `target`, `expected_actions`, `max_duration_minutes`, `max_actions`, `risk_level`, `requires_confirmation`. Deterministic in the walking skeleton; LLM-backed later.
- **Action**: a single typed computer operation (e.g., `navigate`, `search`, `scroll`, `open_link`, `read_extract`, `save_note`, `open_app`, `close_tab`). Vocabulary is closed and grows only on demonstrated need.
- **AgentState**: lifecycle of a task: `disabled`, `waiting_for_idle`, `planning`, `running`, `paused_by_user`, `paused_for_approval`, `completed`, `failed`, `stopped`. Validated transitions; illegal transitions are rejected.
- **PolicyEngine**: layer that checks every planned action in order: closed site allowlist → deny-zones inside allowed sites → action class (`auto_allowed` / `confirmation_required` / `forbidden`). Only the owner can mutate policy; the agent never self-expands it.
- **ComputerDriver**: minimal seam over Cua on the real host (screenshots, accessibility tree, mouse/keyboard, browser via CDP with semantic refs, window/app control). Real implementation uses the owner's main Chrome profile; tests use `FakeComputerDriver`.
- **ModelProvider**: OpenAI-compatible chat+vision adapter (OpenRouter `https://openrouter.ai/api/v1`, OpenCode Go gateway `https://opencode.ai/zen/go/v1`, or any compatible endpoint — strict `model`+`messages` payload, no non-standard fields). Tests use `FakeModelProvider`. API keys live in the credential store or env, never in logs/reports/repo.
- **ProviderConfig / ProviderStore**: non-secret config (`name`, `base_url`, `model` in `providers.json`); secret `api_key` only in system Keychain (`idlecua` / `provider:<name>`).
- **LLM accounting**: daily counter in `llm_usage.json` incremented on each real `ModelProvider` call — hook for the 150-call cap.
- **IdleDetector**: macOS Quartz HID hardware-event idle timer. Synthetic input from the driver must never mask the owner's return.
- **Deny-zone**: sensitive area inside an allowed site that is always blocked (DMs/chats, account/settings, password/2FA/billing, re-auth, notifications).
- **Allowlist**: closed set of sites the agent may visit. Preseeded with `x.com`, `reddit.com`, `youtube.com`, `github.com`, `news.ycombinator.com`, `arxiv.org`, `facebook.com`, `instagram.com`, `linkedin.com`, `tiktok.com`, `bsky.app`, `threads.net`, `mastodon.social`, `google.com`; extendable only by the owner.
- **Anti-repeat**: 7-day window that suppresses exact duplicate normalized queries, processed URLs, and plan fingerprints.
- **Session / Report**: one bounded run (≤45 min, ≤200 actions, ≤150 LLM calls/day) that ends with a persisted Markdown report and rows in local SQLite.

## States

`disabled` — agent is off; no idle watching.
`waiting_for_idle` — enabled, watching idle gates.
`planning` — building a bounded plan.
`running` — executing typed actions.
`paused_by_user` — owner returned; input halted, held keys/buttons released.
`paused_for_approval` — awaiting interactive y/n for a confirmation-required action.
`completed` — task finished successfully.
`failed` — task failed (planning or execution error).
`stopped` — emergency stop / cancellation (SIGINT/SIGTERM/CLI kill), LLM-independent.

## Out of scope terms (MVP)

GUI, MCP/REST server, global hotkey, Lume VM isolation, second driver/browser adapter, embeddings/vector store, plugin marketplace.
