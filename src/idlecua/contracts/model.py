from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

class ModelProvider(ABC):
    """Small seam for an OpenAI-compatible chat+vision model.

    One real implementation over HTTP (OpenRouter / OpenCode Go gateway) will
    be added later; the walking skeleton ships only the interface and a fake.
    """

    @abstractmethod
    def complete(self, prompt: str) -> str:
        raise NotImplementedError

    async def acomplete(self, prompt: str) -> str:
        # Default async wrapper — real provider may override for true async.
        return self.complete(prompt)

    def chat(self, messages: list[dict]) -> str:
        """Low-level chat with OpenAI-format messages.

        Default impl extracts the last user text and delegates to :meth:`complete`.
        Real provider overrides with a strict HTTP call. Tests may override.
        """
        # Find last user message text
        for m in reversed(messages):
            content = m.get("content", "")
            if isinstance(content, str):
                return self.complete(content)
            if isinstance(content, list):
                # vision-style content: find text part
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        return self.complete(str(part.get("text", "")))
                return self.complete("")
        return self.complete("")

@dataclass
class FakeModelProvider(ModelProvider):
    """Deterministic fake — returns a canned response, records prompts.

    No network, no cost, no secrets.
    """

    response: str = "fake-model-response"
    calls: list[str] = field(default_factory=list)
    data_dir: str | None = None  # optional for LLM accounting in tests

    def complete(self, prompt: str) -> str:
        self.calls.append(prompt)
        # Mirror real provider accounting hook when data_dir is set (cap tests)
        if self.data_dir is not None:
            try:
                from ..accounting import record_llm_call

                record_llm_call(self.data_dir, model="fake")
            except Exception:
                pass
        return self.response

    def chat(self, messages: list[dict]) -> str:
        # Use complete path so accounting stays single
        # Extract last user text like base class but keep accounting via complete
        for m in reversed(messages):
            content = m.get("content", "")
            if isinstance(content, str):
                return self.complete(content)
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        return self.complete(str(part.get("text", "")))
                return self.complete("")
        return self.complete("")

    def reset(self) -> None:
        self.calls.clear()
