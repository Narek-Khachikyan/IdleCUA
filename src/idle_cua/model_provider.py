from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ChatMessage:
    role: str
    content: str


class ModelProvider(ABC):
    @abstractmethod
    async def chat(self, messages: list[ChatMessage]) -> str:
        ...


class FakeModelProvider(ModelProvider):
    def __init__(self, response: str = "fake response") -> None:
        self.response = response
        self.calls: list[list[ChatMessage]] = []

    async def chat(self, messages: list[ChatMessage]) -> str:
        self.calls.append(messages)
        return self.response
