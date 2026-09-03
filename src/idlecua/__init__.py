"""IdleCUA — policy-gated idle-time computer-use agent."""

from .app import IdleCua
from .config import IdleCuaConfig
from .contracts import ComputerDriver, CuaComputerDriver, FakeComputerDriver, FakeModelProvider, ModelProvider
from .models import AgentState, Plan, RiskLevel, Task, is_valid_transition, validate_transition
from .policy import (
    PRESEEDED_ALLOWLIST,
    ActionClass,
    PolicyEngine,
    PolicyResult,
    PolicyVerdict,
    TypedAction,
)
from .providers.config import ProviderConfig, ProviderStore

try:
    from .providers.openai_adapter import OpenAICompatibleProvider, VisionTestStatus
except Exception:  # pragma: no cover
    OpenAICompatibleProvider = None  # type: ignore
    VisionTestStatus = None  # type: ignore

__all__ = [
    "PRESEEDED_ALLOWLIST",
    "ActionClass",
    "AgentState",
    "ComputerDriver",
    "CuaComputerDriver",
    "FakeComputerDriver",
    "FakeModelProvider",
    "IdleCua",
    "IdleCuaConfig",
    "ModelProvider",
    "OpenAICompatibleProvider",
    "Plan",
    "PolicyEngine",
    "PolicyResult",
    "PolicyVerdict",
    "ProviderConfig",
    "ProviderStore",
    "RiskLevel",
    "Task",
    "TypedAction",
    "VisionTestStatus",
    "is_valid_transition",
    "validate_transition",
]
