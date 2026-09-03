from __future__ import annotations

import re

from .models import Profile

_DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$", re.IGNORECASE)
# Also allow single-label like localhost? For allowlist we expect real domains.
_ALLOWED_HOURS_RE = re.compile(r"^\d{2}:\d{2}-\d{2}:\d{2}$")


def _is_valid_domain(domain: str) -> bool:
    domain = domain.strip().lower()
    if not domain:
        return False
    # allow with or without protocol, extract host
    if "://" in domain:
        # strip scheme
        domain = domain.split("://", 1)[1].split("/")[0]
    # strip port
    domain = domain.split(":")[0]
    # strip path
    domain = domain.split("/")[0]
    return bool(_DOMAIN_RE.match(domain))


def validate_profile(profile: Profile) -> list[str]:
    errors: list[str] = []

    # Allowlist validation
    allowed = profile.autonomy_boundaries.allowed_sites
    if not allowed:
        errors.append("allowlist is empty: at least one allowed site required (e.g. x.com, reddit.com)")
    else:
        for site in allowed:
            if not _is_valid_domain(site):
                errors.append(f"allowlist: invalid domain '{site}'")

    # Limits validation — tighten-only vs hard ceilings (ADR-0003)
    # Ceilings are 45 min, 200 actions, 150 LLM calls; Profile values above ceiling are validation errors
    ab = profile.autonomy_boundaries
    if ab.session_duration_minutes <= 0 or ab.session_duration_minutes > 45:
        errors.append(f"session_duration_minutes must be 1..45, got {ab.session_duration_minutes}")
    if ab.daily_action_limit <= 0 or ab.daily_action_limit > 1000:
        errors.append(f"daily_action_limit must be 1..1000, got {ab.daily_action_limit}")
    elif ab.daily_action_limit > 200:
        errors.append(f"daily_action_limit {ab.daily_action_limit} above ceiling 200 (tighten-only: Profile value above ceiling is rejected)")
    if ab.daily_llm_call_limit <= 0 or ab.daily_llm_call_limit > 1000:
        errors.append(f"daily_llm_call_limit must be 1..1000, got {ab.daily_llm_call_limit}")
    elif ab.daily_llm_call_limit > 150:
        errors.append(f"daily_llm_call_limit {ab.daily_llm_call_limit} above ceiling 150 (tighten-only: Profile value above ceiling is rejected)")
    cu = profile.computer_usage
    # ADR-0003: single threshold in seconds, home is Profile
    idle_seconds = int(getattr(cu, "idle_threshold_seconds", 600))
    # Back-compat: if seconds is default but minutes differs, use minutes
    if idle_seconds == 600 and int(cu.idle_threshold_minutes) != 10:
        idle_seconds = int(cu.idle_threshold_minutes) * 60
    if idle_seconds < 60 or idle_seconds > 7200:
        errors.append(f"idle_threshold_seconds must be 60..7200, got {idle_seconds}")
    # Also validate minutes derived
    mins = (idle_seconds + 59) // 60
    if mins < 1 or mins > 120:
        errors.append(f"idle_threshold_minutes must be 1..120, got {mins}")

    # Schedule / allowed_hours sanity
    allowed_hours = ab.allowed_hours.strip()
    if allowed_hours:
        if not _ALLOWED_HOURS_RE.match(allowed_hours):
            errors.append(
                f"allowed_hours must be HH:MM-HH:MM (e.g. 09:00-17:00 or 00:00-23:59), got '{allowed_hours}'"
            )
        else:
            try:
                start_s, end_s = allowed_hours.split("-")
                sh, sm = map(int, start_s.split(":"))
                eh, em = map(int, end_s.split(":"))
                if not (0 <= sh <= 23 and 0 <= sm <= 59 and 0 <= eh <= 23 and 0 <= em <= 59):
                    errors.append(f"allowed_hours has invalid hour/minute: '{allowed_hours}'")
            except Exception:
                errors.append(f"allowed_hours could not be parsed: '{allowed_hours}'")

    return errors
