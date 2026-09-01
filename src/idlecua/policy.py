from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse

from .config import IdleCuaConfig


class PolicyVerdict(str, Enum):
    """Result of policy evaluation for a single typed action."""

    allowed = "allowed"
    needs_confirmation = "needs-confirmation"
    blocked = "blocked"


class ActionClass(str, Enum):
    auto_allowed = "auto_allowed"
    confirmation_required = "confirmation_required"
    forbidden = "forbidden"
    unknown = "unknown"


# -- Preseeded allowlist (must match IdleCuaConfig default) --
PRESEEDED_ALLOWLIST: list[str] = [
    "x.com",
    "reddit.com",
    "youtube.com",
    "github.com",
    "news.ycombinator.com",
    "arxiv.org",
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "tiktok.com",
    "bsky.app",
    "threads.net",
    "mastodon.social",
    "google.com",
]

# -- Deny-zones inside allowed domains --
# Substrings matched case-insensitively against the URL (path + query).
DEFAULT_DENY_ZONE_PATTERNS: list[str] = [
    "/messages",
    "/inbox",
    "/dm",
    "/direct",
    "/chat",
    "/settings",
    "/account",
    "/password",
    "/2fa",
    "/two-factor",
    "/billing",
    "/payment",
    "/reauth",
    "/re-auth",
    "/notifications",
]

# Normalize to lower for matching.
_DEFAULT_DENY_ZONES_LOWER = [p.lower() for p in DEFAULT_DENY_ZONE_PATTERNS]

# -- Action classification --
AUTO_ALLOWED: frozenset[str] = frozenset(
    [
        "open_allowed_site",
        "open_app",
        "search",
        "read_ui",
        "scroll",
        "open_link",
        "extract_public_info",
        "save_note",
        "create_note",
        "save_link",
        "close_own_tab",
        "close_own_app",
        "navigate",
        "read",
        "extract",
    ]
)

CONFIRMATION_REQUIRED: frozenset[str] = frozenset(
    [
        "like",
        "follow",
        "comment",
        "post",
        "publish",
        "message",
        "send_message",
        "submit_form",
        "form_submit",
        "download",
        "edit_document",
        "edit",
        "change_account_settings",
        "account_settings_change",
        "move_file",
        "delete_file",
        "delete",
        "close_user_tab",
        "close_user_app",
        "close_user_window",
        "file_move",
        "file_delete",
    ]
)

FORBIDDEN: frozenset[str] = frozenset(
    [
        "payment",
        "payments",
        "purchase",
        "pay",
        "bypass_captcha",
        "captcha_bypass",
        "captcha",
        "evade_rate_limit",
        "rate_limit_evasion",
        "mask_automation",
        "bulk_engage",
        "bulk_engagement",
        "spam",
        "harvest_private_data",
        "harvest_data",
        "harvest",
        "access_other_account",
        "access_others_account",
        "disable_policy_engine",
        "disable_policy",
        "expand_allowlist",
        "self_expand_allowlist",
        "self_expanding_allowlist",
        "install_software",
        "remove_software",
        "uninstall_software",
        "change_system_settings",
        "system_settings_change",
        "enter_password_via_llm",
        "enter_password",
        "credential_entry_via_llm",
        "enter_credentials",
        "follow_page_instructions",
        "follow_instructions_in_page",
        "execute_page_instructions",
        "follow_instructions",
    ]
)


def _normalize_domain(domain: str) -> str:
    """Lowercase, strip port, strip leading www.? Keep as-is for matching."""
    d = domain.strip().lower()
    # Remove port if present
    if ":" in d:
        d = d.split(":", 1)[0]
    # Strip trailing dot
    d = d.rstrip(".")
    return d


def _extract_domain(url_or_domain: str) -> str | None:
    """Extract hostname from a URL or bare domain string."""
    if not url_or_domain:
        return None
    s = url_or_domain.strip()
    if not s:
        return None
    # If it looks like a URL with scheme, parse it.
    if "://" in s:
        try:
            parsed = urlparse(s)
            if parsed.hostname:
                return _normalize_domain(parsed.hostname)
        except Exception:
            pass
        return None
    # Bare domain or host:port/path – take up to first slash
    # e.g., "x.com/messages" -> "x.com"
    host = s.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    # Remove port
    host = host.split(":", 1)[0]
    # Basic validation: must contain a dot or be localhost? For allowlist we just normalize.
    if not host:
        return None
    return _normalize_domain(host)


@dataclass(frozen=True)
class TypedAction:
    """Minimal typed action vocabulary for policy evaluation.

    Only the owner can mutate policy; the agent never self-expands it.
    The vocabulary is closed and grows only on demonstrated need.
    """

    kind: str
    target_url: str | None = None
    target_domain: str | None = None
    description: str = ""
    payload: dict | None = None

    def __post_init__(self) -> None:
        if not self.kind or not self.kind.strip():
            raise ValueError("TypedAction.kind must be non-empty")

    @property
    def effective_domain(self) -> str | None:
        """Domain derived from target_domain or target_url."""
        if self.target_domain:
            return _normalize_domain(self.target_domain)
        if self.target_url:
            return _extract_domain(self.target_url)
        return None

    @property
    def effective_url(self) -> str | None:
        return self.target_url


@dataclass(frozen=True)
class PolicyResult:
    verdict: PolicyVerdict
    reason: str
    action_class: ActionClass
    # For debugging / dry-run labeling
    domain: str | None = None
    is_allowlisted: bool | None = None
    is_deny_zone: bool | None = None


def classify_action(kind: str) -> ActionClass:
    k = kind.strip().lower()
    # Normalize: replace hyphens/spaces with underscores
    k_norm = re.sub(r"[\s\-]+", "_", k)
    if k_norm in FORBIDDEN or k in FORBIDDEN:
        return ActionClass.forbidden
    if k_norm in CONFIRMATION_REQUIRED or k in CONFIRMATION_REQUIRED:
        return ActionClass.confirmation_required
    if k_norm in AUTO_ALLOWED or k in AUTO_ALLOWED:
        return ActionClass.auto_allowed
    # Try lower original without normalization
    low = k.lower()
    if low in FORBIDDEN:
        return ActionClass.forbidden
    if low in CONFIRMATION_REQUIRED:
        return ActionClass.confirmation_required
    if low in AUTO_ALLOWED:
        return ActionClass.auto_allowed
    # Heuristic fallback for forbidden — catch common variants and substrings
    # that spec explicitly lists as hard-blocked.
    # Payments / purchase
    if "payment" in low or low == "pay" or "purchase" in low:
        return ActionClass.forbidden
    if "captcha" in low:
        return ActionClass.forbidden
    if "credential" in low and ("password" in low or "enter" in low):
        return ActionClass.forbidden
    if "password" in low and ("enter" in low or "llm" in low):
        return ActionClass.forbidden
    if ("install" in low and "software" in low) or ("remove" in low and "software" in low) or "uninstall" in low:
        return ActionClass.forbidden
    if "system" in low and "setting" in low:
        return ActionClass.forbidden
    if "allowlist" in low and ("expand" in low or "self" in low):
        return ActionClass.forbidden
    if "disable" in low and "policy" in low:
        return ActionClass.forbidden
    if "harvest" in low or ("private" in low and "data" in low):
        return ActionClass.forbidden
    if "access" in low and "other" in low:
        return ActionClass.forbidden
    if "mask" in low and "automation" in low:
        return ActionClass.forbidden
    if "rate" in low and "limit" in low and ("evade" in low or "bypass" in low):
        return ActionClass.forbidden
    if "bulk" in low or low == "spam":
        return ActionClass.forbidden
    if "follow" in low and "instruction" in low:
        return ActionClass.forbidden
    # Confirmation heuristic — common write actions
    if low in {"post", "like", "follow", "comment", "message", "download", "edit", "delete"}:
        return ActionClass.confirmation_required
    if any(w in low for w in ["post", "like", "follow", "comment", "message", "download", "edit", "delete", "move_file", "close_user"]):
        # More specific but avoid over-matching search etc.
        # If contains confirmation keywords and not already classified
        for cand in CONFIRMATION_REQUIRED:
            if cand in low or low in cand:
                return ActionClass.confirmation_required
    return ActionClass.unknown


class PolicyEngine:
    """Gate every typed action must pass, read-only by default.

    Layered evaluation in order:
      1. closed site allowlist
      2. deny-zones inside allowed domains
      3. action classification (auto-allowed / confirmation-required / forbidden)

    Only the owner can widen any policy layer (via config/profile edit), never the agent.
    The agent cannot self-expand the allowlist (hard rule).
    """

    def __init__(
        self,
        config: IdleCuaConfig | None = None,
        *,
        allowlist: list[str] | None = None,
        deny_zones: list[str] | None = None,
    ) -> None:
        self._config = config
        # Snapshot allowlist at construction — mutations to config after do not affect this engine
        # unless owner creates a new engine/config.
        if allowlist is not None:
            src = allowlist
        elif config is not None:
            src = list(config.allowlist)
        else:
            src = list(PRESEEDED_ALLOWLIST)
        self._allowlist: frozenset[str] = frozenset(_normalize_domain(d) for d in src if d and d.strip())
        # Deny zones: snapshot
        if deny_zones is not None:
            self._deny_zones: list[str] = [p.lower() for p in deny_zones if p and p.strip()]
        elif config is not None and hasattr(config, "deny_zones"):
            # Support config.deny_zones if present
            raw = getattr(config, "deny_zones", None)
            if raw is not None:
                self._deny_zones = [p.lower() for p in raw if p and isinstance(p, str) and p.strip()]
            else:
                self._deny_zones = list(_DEFAULT_DENY_ZONES_LOWER)
        else:
            self._deny_zones = list(_DEFAULT_DENY_ZONES_LOWER)

    @property
    def allowlist(self) -> frozenset[str]:
        return self._allowlist

    @property
    def deny_zones(self) -> list[str]:
        return list(self._deny_zones)

    def is_allowed_domain(self, domain: str | None) -> bool:
        if not domain:
            return False
        d = _normalize_domain(domain)
        if d in self._allowlist:
            return True
        # Allow subdomains: foo.x.com -> x.com
        for allowed in self._allowlist:
            if d == allowed or d.endswith("." + allowed):
                # Also handle www. prefix: www.reddit.com -> reddit.com
                return True
        return False

    def is_deny_zone(self, url: str | None) -> bool:
        if not url:
            return False
        lower = url.lower()
        for pat in self._deny_zones:
            if pat in lower:
                return True
        return False

    def evaluate(self, action: TypedAction) -> PolicyResult:
        """Evaluate a typed action through the layered policy.

        Order: allowlist → deny-zone → classification.
        """
        domain = action.effective_domain
        url = action.effective_url
        kind = action.kind.strip()

        # Layer 1 & 2 only apply to actions that have a URL/domain target.
        # Local actions (save_note, etc.) skip these layers.
        has_target = bool(domain or url)

        # We need to determine if the action kind requires a target.
        # For simplicity, if action has a target, enforce allowlist/deny-zone.
        # If it has no target, skip those layers (local).
        is_allowlisted: bool | None = None
        is_deny: bool | None = None

        if has_target:
            # Layer 1: allowlist
            # If domain is missing but url present, we already extracted; if still None, treat as not allowed
            if domain is None:
                # URL without parseable domain -> block
                return PolicyResult(
                    verdict=PolicyVerdict.blocked,
                    reason="allowlist: missing or unparseable domain",
                    action_class=classify_action(kind),
                    domain=domain,
                    is_allowlisted=False,
                    is_deny_zone=False,
                )
            is_allowlisted = self.is_allowed_domain(domain)
            if not is_allowlisted:
                ac = classify_action(kind)
                # Even forbidden is blocked, but we label allowlist reason
                # Spec says order is allowlist first, so blocked due to allowlist
                return PolicyResult(
                    verdict=PolicyVerdict.blocked,
                    reason=f"allowlist: domain '{domain}' not in closed allowlist",
                    action_class=ac,
                    domain=domain,
                    is_allowlisted=False,
                    is_deny_zone=False,
                )
            # Layer 2: deny-zone (only inside allowed domains)
            # Check url if present, else check domain-derived url? We check url or fallback to domain+path patterns?
            # Use url if available, otherwise construct from domain
            check_url = url if url else f"https://{domain}"
            is_deny = self.is_deny_zone(check_url)
            if is_deny:
                ac = classify_action(kind)
                return PolicyResult(
                    verdict=PolicyVerdict.blocked,
                    reason=f"deny-zone: url '{check_url}' matches deny-zone pattern",
                    action_class=ac,
                    domain=domain,
                    is_allowlisted=True,
                    is_deny_zone=True,
                )
        else:
            # No target: allowlist/deny-zone not applicable
            is_allowlisted = None
            is_deny = None

        # Layer 3: action classification
        ac = classify_action(kind)
        if ac == ActionClass.forbidden:
            return PolicyResult(
                verdict=PolicyVerdict.blocked,
                reason=f"forbidden: action kind '{kind}' is hard-blocked",
                action_class=ac,
                domain=domain,
                is_allowlisted=is_allowlisted,
                is_deny_zone=is_deny,
            )
        if ac == ActionClass.unknown:
            return PolicyResult(
                verdict=PolicyVerdict.blocked,
                reason=f"blocked: unknown action kind '{kind}' not in vocabulary",
                action_class=ac,
                domain=domain,
                is_allowlisted=is_allowlisted,
                is_deny_zone=is_deny,
            )
        if ac == ActionClass.confirmation_required:
            return PolicyResult(
                verdict=PolicyVerdict.needs_confirmation,
                reason=f"confirmation-required: action kind '{kind}' requires owner confirmation",
                action_class=ac,
                domain=domain,
                is_allowlisted=is_allowlisted,
                is_deny_zone=is_deny,
            )
        # auto_allowed
        return PolicyResult(
            verdict=PolicyVerdict.allowed,
            reason=f"allowed: action kind '{kind}' is auto-allowed" + (f" on '{domain}'" if domain else ""),
            action_class=ac,
            domain=domain,
            is_allowlisted=is_allowlisted,
            is_deny_zone=is_deny,
        )

    # Convenience for dry-run labeling of string actions
    def evaluate_kind_and_url(self, kind: str, url: str | None = None, domain: str | None = None) -> PolicyResult:
        return self.evaluate(TypedAction(kind=kind, target_url=url, target_domain=domain))

    # Agent-originated widening is rejected — no public mutator
    # If agent calls any of these, they raise.
    def add_allowed_domain(self, domain: str, *, owner: bool = False) -> None:  # type: ignore[no-untyped-def]
        """Only the owner can widen the allowlist via config edit. Agent calls are rejected."""
        if not owner:
            raise PermissionError("agent-originated allowlist widening is rejected; only owner via config can widen")
        raise NotImplementedError("owner must edit IdleCuaConfig and create a new PolicyEngine; direct mutation is not supported")

    def add_deny_zone(self, pattern: str, *, owner: bool = False) -> None:
        if not owner:
            raise PermissionError("agent-originated deny-zone widening is rejected; only owner via config can change deny-zones")
        raise NotImplementedError("owner must edit IdleCuaConfig and create a new PolicyEngine")
