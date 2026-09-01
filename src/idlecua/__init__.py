"""IdleCUA — policy-gated idle-time computer-use agent."""

from .app import IdleCua
from .config import IdleCuaConfig
from .contracts import ComputerDriver, FakeComputerDriver, FakeModelProvider, ModelProvider
from .models import AgentState, Plan, RiskLevel, Task, is_valid_transition, validate_transition
from .providers.config import ProviderConfig, ProviderStore

try:
    from .providers.openai_adapter import OpenAICompatibleProvider, VisionTestStatus
except Exception:  # pragma: no cover
    OpenAICompatibleProvider = None  # type: ignore
    VisionTestStatus = None  # type: ignore

__all__ = [
    "IdleCua",
    "IdleCuaConfig",
    "ComputerDriver",
    "FakeComputerDriver",
    "ModelProvider",
    "FakeModelProvider",
    "AgentState",
    "Plan",
    "RiskLevel",
    "Task",
    "is_valid_transition",
    "validate_transition",
    "ProviderConfig",
    "ProviderStore",
    "OpenAICompatibleProvider",
    "VisionTestStatus",
]
