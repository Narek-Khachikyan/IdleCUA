from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def generate_markdown_report(
    task: dict,
    plan: dict | Any,
    queries: list[dict],
    urls: list[dict],
    findings: list[dict],
    errors: list[dict],
    actions: list[dict],
    skipped_repeats: list[dict],
    profile: Any | None = None,
    limits: dict | None = None,
) -> str:
    """Generate Markdown session report per spec.

    Includes: tasks done, queries used, best findings with links, relevance to profile,
    skipped repeats, errors, unfinished actions, limit usage.
    """
    # Normalize plan dict
    if hasattr(plan, "goal"):
        plan_dict = {
            "goal": plan.goal,
            "target": getattr(plan, "target", ""),
            "expected_actions": getattr(plan, "expected_actions", []),
            "expected_result": getattr(plan, "expected_result", ""),
            "max_duration_minutes": getattr(plan, "max_duration_minutes", 0),
            "max_actions": getattr(plan, "max_actions", 0),
            "risk_level": getattr(plan, "risk_level", "low").value if hasattr(getattr(plan, "risk_level", "low"), "value") else str(getattr(plan, "risk_level", "low")),
        }
    else:
        plan_dict = dict(plan) if isinstance(plan, dict) else {}

    now = datetime.now(timezone.utc).isoformat()
    lines: list[str] = []
    lines.append(f"# IdleCUA Session Report — {task.get('id','')[:8]}")
    lines.append("")
    lines.append(f"_Generated: {now} — Task: {task.get('description','')} — State: {task.get('state','')} _")
    lines.append("")

    # Tasks done
    lines.append("## Tasks done")
    lines.append(f"- Goal: {plan_dict.get('goal','')}")
    lines.append(f"- Target: {plan_dict.get('target','')}")
    lines.append(f"- Expected result: {plan_dict.get('expected_result','')}")
    lines.append(f"- Risk level: {plan_dict.get('risk_level','')}")
    lines.append(f"- Actions planned: {', '.join(plan_dict.get('expected_actions',[]))}")
    lines.append("")

    # Queries
    lines.append("## Queries used")
    if queries:
        for q in queries:
            lines.append(f"- `{q.get('query','')}` (normalized: `{q.get('normalized','')}`)")
    else:
        lines.append("- (none)")
    lines.append("")

    # Best findings with links
    lines.append("## Best findings")
    if findings:
        for f in findings:
            lines.append(f"- **{f.get('title','')}** — [{f.get('url','')}]({f.get('url','')})")
            if f.get("summary"):
                lines.append(f"  - {f.get('summary','')}")
            if f.get("relevance"):
                lines.append(f"  - Relevance: {f.get('relevance','')}")
    else:
        lines.append("- (no findings extracted — read-only exploration)")
        # synthesize at least one finding from urls if none
        if urls:
            for u in urls[:3]:
                lines.append(f"- [{u.get('url','')}]({u.get('url','')}) — visited")
    lines.append("")

    # Relevance to profile
    lines.append("## Relevance to profile")
    if profile is not None:
        try:
            interests = getattr(profile.user_characteristics, "interests", []) or []
            projects = getattr(profile.user_characteristics, "projects", []) or []
            lines.append(f"- Interests: {', '.join(interests) if interests else '(none configured)'}")
            lines.append(f"- Projects: {', '.join(projects) if projects else '(none configured)'}")
            lines.append(f"- Goal relevance: findings relate to `{plan_dict.get('goal','')}` within allowed sites `{', '.join(getattr(profile.autonomy_boundaries, 'allowed_sites', []))}`")
        except Exception:
            lines.append(f"- Profile interests/projects linked to goal: `{plan_dict.get('goal','')}`")
    else:
        lines.append(f"- No profile confirmed; findings are generic for goal `{plan_dict.get('goal','')}`")
    lines.append("")

    # Skipped repeats
    lines.append("## Skipped repeats (7-day window)")
    if skipped_repeats:
        for s in skipped_repeats:
            lines.append(f"- {s.get('type','')}: `{s.get('value','')}` — {s.get('reason','')}")
    else:
        lines.append("- (none — all queries/URLs/plans were fresh)")
    lines.append("")

    # Errors
    lines.append("## Errors")
    if errors:
        for e in errors:
            lines.append(f"- {e.get('message','')}")
    else:
        lines.append("- (none)")
    lines.append("")

    # Unfinished actions
    lines.append("## Unfinished actions")
    # Compare planned vs actually performed
    planned = set(plan_dict.get("expected_actions", []))
    performed_kinds = [a.get("kind","") for a in actions]
    performed_set = set(performed_kinds)
    unfinished = planned - performed_set
    if unfinished:
        for u in sorted(unfinished):
            lines.append(f"- {u} — not executed (limit, policy, or interruption)")
    else:
        if len(performed_kinds) < len(planned):
            lines.append(f"- Planned {len(planned)} actions, performed {len(performed_kinds)}; see actions below")
        else:
            lines.append("- (none — all planned actions completed)")
    # Also list any action errors
    failed_actions = [a for a in actions if a.get("status") == "failed" or a.get("status") == "blocked"]
    if failed_actions:
        lines.append("- Failed/blocked actions:")
        for fa in failed_actions:
            lines.append(f"  - {fa.get('kind','')} ({fa.get('verdict','')}): {fa.get('error','')}")
    lines.append("")

    # Actions detail
    lines.append("## Actions executed")
    if actions:
        for a in actions:
            lines.append(f"- {a.get('kind','')} → {a.get('target_url','') or '(local)'} [{a.get('verdict','')}/{a.get('status','')}]")
    else:
        lines.append("- (none)")
    lines.append("")

    # URLs visited
    lines.append("## URLs visited")
    if urls:
        for u in urls:
            lines.append(f"- {u.get('url','')} (fp: {u.get('fingerprint','')[:8]})")
    else:
        lines.append("- (none)")
    lines.append("")

    # Limit usage
    lines.append("## Limit usage")
    if limits:
        lines.append(f"- Actions: {limits.get('actions_used',0)} / {limits.get('max_actions',200)} per session")
        lines.append(f"- Duration: {limits.get('duration_minutes',0)} / {limits.get('max_duration_minutes',45)} min")
        lines.append(f"- LLM calls today: {limits.get('llm_calls_today',0)} / {limits.get('max_llm_calls_per_day',150)}")
        if limits.get("stopped_due_to_limit"):
            lines.append(f"- Stopped due to limit: {limits.get('stopped_due_to_limit')}")
    else:
        lines.append("- (limits not tracked)")
    lines.append("")

    lines.append("---")
    lines.append("_IdleCUA — policy-gated idle-time agent (read-only default, main Chrome profile, verified by re-read)._")

    return "\n".join(lines)


def save_report_to_file(markdown: str, data_dir: Path, task_id: str) -> Path:
    reports_dir = Path(data_dir).expanduser() / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    # Daily file + per-task file
    today = datetime.now(timezone.utc).date().isoformat()
    daily_path = reports_dir / f"{today}.md"
    per_task_path = reports_dir / f"{task_id}.md"
    per_task_path.write_text(markdown, encoding="utf-8")
    # Append to daily? For simplicity, ensure daily contains latest
    try:
        # If daily exists, append; else create
        if daily_path.exists():
            existing = daily_path.read_text(encoding="utf-8")
            # avoid duplicate if same task already in daily
            if task_id not in existing:
                daily_path.write_text(existing + "\n\n---\n\n" + markdown, encoding="utf-8")
        else:
            daily_path.write_text(markdown, encoding="utf-8")
    except Exception:
        pass
    return per_task_path
