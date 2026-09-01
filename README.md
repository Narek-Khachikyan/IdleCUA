# IdleCUA

Policy-gated idle-time computer-use agent for macOS.

## Install

```bash
uv sync
uv run idle-cua --help
```

## Quick start

```bash
idle-cua init
idle-cua profile interview
idle-cua profile show
idle-cua profile validate
idle-cua plan "research recent AI papers"
idle-cua run-once --dry-run "research recent AI papers"
```

## Profile

Machine-readable profile at `<data-dir>/profile.json` with human-readable rendering via `profile show`.
Permissions: `profile check-permissions`.

## Docs

- `CONTEXT.md` - domain glossary
- `docs/ADR-0001.md` - locked MVP scope
