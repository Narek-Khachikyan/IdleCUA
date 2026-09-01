"""Compatibility alias — same as idlecua.policy."""
from .policy import *
from .policy import (  # noqa: F401
    DEFAULT_DENY_ZONE_PATTERNS,
    PRESEEDED_ALLOWLIST,
    ActionClass,
    PolicyEngine,
    PolicyResult,
    PolicyVerdict,
    TypedAction,
)
