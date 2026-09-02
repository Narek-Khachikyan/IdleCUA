from __future__ import annotations

from .models import Profile


def _fmt_list(items: list[str]) -> str:
    if not items:
        return "—"
    return ", ".join(items)


def render_human_readable(profile: Profile) -> str:
    """Human-readable rendering derived from machine-readable profile (no duplication)."""
    uc = profile.user_characteristics
    cu = profile.computer_usage
    ab = profile.autonomy_boundaries
    lines: list[str] = []
    lines.append("# IdleCUA Profile")
    lines.append("")
    lines.append(f"Confirmed: {'yes' if profile.confirmed else 'NO — autonomous runs are blocked until confirmed'}")
    lines.append(f"Version: {profile.version}")
    lines.append(f"Updated: {profile.meta.updated_at}")
    lines.append("")
    lines.append("## User Characteristics")
    lines.append(f"- Occupation: {uc.occupation or '—'}")
    lines.append(f"- Projects: {_fmt_list(uc.projects)}")
    lines.append(f"- Goals: {_fmt_list(uc.goals)}")
    lines.append(f"- Interests: {_fmt_list(uc.interests)}")
    lines.append(f"- Technologies: {_fmt_list(uc.technologies)}")
    lines.append(f"- Material types: {_fmt_list(uc.material_types)}")
    lines.append(f"- Material depth: {uc.material_depth or '—'}")
    lines.append(f"- Content languages: {_fmt_list(uc.content_languages)}")
    lines.append(f"- Unwanted topics: {_fmt_list(uc.unwanted_topics)}")
    lines.append("")
    lines.append("## Computer Usage")
    lines.append(f"- Schedule: {cu.schedule or '—'}")
    lines.append(f"- Idle periods: {cu.idle_periods or '—'}")
    lines.append(f"- Overnight habits: {cu.overnight_habits or '—'}")
    lines.append(f"- Screen-lock habits: {cu.screen_lock_habits or '—'}")
    lines.append(f"- Monitors: {cu.monitors or '—'}")
    lines.append(f"- Common apps: {_fmt_list(cu.common_apps)}")
    lines.append(f"- Common sites: {_fmt_list(cu.common_sites)}")
    lines.append(f"- Return signals: {_fmt_list(cu.return_signals)}")
    lines.append(f"- Idle threshold: {cu.idle_threshold_minutes} min")
    lines.append("")
    lines.append("## Autonomy Boundaries")
    lines.append(f"- Allowed sites: {_fmt_list(ab.allowed_sites)}")
    lines.append(f"- Allowed apps: {_fmt_list(ab.allowed_apps)}")
    lines.append(f"- Auto-allowed actions: {_fmt_list(ab.auto_allowed_actions)}")
    lines.append(f"- Confirmation-required actions: {_fmt_list(ab.confirmation_required_actions)}")
    lines.append(f"- Forbidden actions: {_fmt_list(ab.forbidden_actions)}")
    lines.append(f"- Results location: {ab.results_location or '—'}")
    lines.append(f"- Session duration: {ab.session_duration_minutes} min (max 45)")
    lines.append(f"- Daily action limit: {ab.daily_action_limit}")
    lines.append(f"- Daily LLM call limit: {ab.daily_llm_call_limit}")
    lines.append(f"- Allowed hours: {ab.allowed_hours}")
    # Browser main-profile consent (issue #12)
    bc = ab.browser_consent
    if bc.main_profile_granted:
        lines.append(f"- Browser main profile: GRANTED ({bc.browser}, {bc.granted_at or 'no timestamp'}, via {bc.grant_method or 'unknown'})")
    else:
        lines.append("- Browser main profile: NOT granted — agent will not attach to your main Chrome profile until you grant (idle-cua profile grant-browser)")
    lines.append("")
    return "\n".join(lines)
