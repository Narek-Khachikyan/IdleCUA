# ADR-0003: One authority per setting — Profile owns owner-intent, Config owns safety

Date: 2026-09-01
Status: Accepted

## Context

ADR-0002 surfaces settings in the Local UI, but today the same owner-facing limits live in several places: `config.json` (`max_duration_minutes`, `max_actions`, `max_llm_calls_per_day`, `idle_threshold_seconds`, `allowlist`, `deny_zones`), `profile.json` (`autonomy_boundaries.session_duration_minutes`, `daily_action_limit`, `daily_llm_call_limit`, `allowed_hours`, `allowed_sites`, `computer_usage.idle_threshold_minutes`), and constants in `policy.py`. The idle threshold even exists in two units (seconds in config, minutes in profile), and the executor reconciles by taking `min()`. UI writes into multiple stores would guarantee drift.

## Decision

Every UI-editable setting gets exactly one authoritative home, split by meaning:

- **Profile — owner intent**: session duration, daily action/LLM limits, allowed hours, allowlist, deny-zones, idle threshold, browser consent. The Local UI Settings page reads and writes only these Profile fields. The Profile is the only mutation path for policy content (owner-only, per ADR-0001).
- **Config — safety and machine level**: `readonly`, `require_idle`, hard ceilings (45 min / 200 actions / 150 LLM calls per day), `data_dir`, dev flags. The UI may toggle `readonly`/`require_idle` and display ceilings, but never edits ceilings or machine fields; those stay CLI/env.
- **Reconciliation is tighten-only**: effective limit = min(Profile value, Config ceiling). A Profile value above a ceiling is rejected at validation time, never silently clamped at runtime.
- **The idle-threshold duplication is removed**: one field, one unit (seconds), one home (the Profile). Config's threshold becomes a default for new profiles, not a live input. Code constants in `policy.py` become immutable preseed defaults.

## Consequences

- Existing installs get a one-time prefill: unset Profile fields inherit current Config values when Settings is first opened, shown for review; a confirmed Profile is never silently overwritten.
- `IdleCuaConfig` shrinks to safety/machine fields; its limit fields become ceilings. Profile validation gains ceiling checks.
- Per-invocation CLI flags (e.g. `start --idle-threshold`) keep precedence for that invocation only.

## References

- ADR-0002 (settings surfaced in the Local UI)
