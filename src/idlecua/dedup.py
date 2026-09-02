from __future__ import annotations

import hashlib
import re
import urllib.parse


def normalize_query(query: str) -> str:
    """Normalize search query for 7-day dedup.

    - lowercase
    - strip leading/trailing whitespace
    - collapse internal whitespace to single space
    - remove punctuation that is syntax-only? Keep core alphanumeric
    - normalize quotes/spaces around operators
    """
    if not query:
        return ""
    q = query.strip().lower()
    # Replace curly quotes
    q = q.replace("“", '"').replace("”", '"').replace("’", "'")
    # Collapse whitespace
    q = re.sub(r"\s+", " ", q)
    # Strip surrounding quotes? Keep but lower.
    # Remove extra spaces around colons etc. For search syntax like site:example.com
    q = re.sub(r"\s*:\s*", ":", q)
    # Normalize - keep alphanum, spaces, colon, quotes, hyphen
    # Actually for exact dedup we want exact normalized comparison; we don't remove punctuation heavily
    # Just lower + whitespace collapse + colon normalization
    q = q.strip()
    return q


def normalize_url(url: str) -> str:
    """Normalize URL for dedup: lower host, remove fragment, sort query, strip trailing slash."""
    if not url:
        return ""
    u = url.strip()
    # Add scheme if missing bare domain
    if "://" not in u:
        u = "https://" + u
    try:
        parsed = urllib.parse.urlparse(u)
        scheme = parsed.scheme.lower() if parsed.scheme else "https"
        netloc = parsed.netloc.lower()
        # Remove default ports
        if netloc.endswith(":80") and scheme == "http":
            netloc = netloc[:-3]
        if netloc.endswith(":443") and scheme == "https":
            netloc = netloc[:-4]
        # Normalize path: remove trailing slash unless root
        path = parsed.path or "/"
        # Decode percent-encoding for consistency? keep as is but lower? path case-sensitive but we lower for dedup simplicity
        # Keep path case as is except lower? For dedup we lower.
        path = path.rstrip("/") or "/"
        # Sort query params
        query = parsed.query
        if query:
            params = urllib.parse.parse_qsl(query, keep_blank_values=True)
            params.sort()
            query = urllib.parse.urlencode(params)
        # Drop fragment
        normalized = urllib.parse.urlunparse((scheme, netloc, path, "", query, ""))
        return normalized
    except Exception:
        return u.lower().strip()


def url_fingerprint(url: str) -> str:
    """Fingerprint for URL dedup: sha256 of normalized url."""
    n = normalize_url(url)
    return hashlib.sha256(n.encode("utf-8")).hexdigest()[:16]


def plan_fingerprint(plan) -> str:
    """Fingerprint for plan dedup: hash of goal normalized + target + sorted expected_actions."""
    try:
        goal_norm = normalize_query(plan.goal)
        target = (plan.target or "").strip().lower()
        actions = sorted(plan.expected_actions or [])
        raw = f"{goal_norm}|{target}|{','.join(actions)}|{plan.max_duration_minutes}|{plan.max_actions}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    except Exception:
        return hashlib.sha256(str(plan).encode("utf-8")).hexdigest()[:16]


def is_repeat_query(query: str, memory, days: int = 7) -> bool:
    return memory.has_query_within_days(normalize_query(query), days=days)


def is_repeat_url(url: str, memory, days: int = 7) -> bool:
    fp = url_fingerprint(url)
    return memory.has_url_within_days(fp, days=days)


def is_repeat_plan(plan, memory, days: int = 7) -> bool:
    fp = plan_fingerprint(plan)
    return memory.has_plan_fingerprint_within_days(fp, days=days)
