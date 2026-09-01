from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Screenshot:
    data: bytes
    width: int
    height: int


class ComputerDriver(ABC):
    """Small contract for host control. One real impl over Cua, one fake for tests."""

    @abstractmethod
    async def screenshot(self) -> Screenshot:
        ...

    @abstractmethod
    async def click(self, x: int, y: int) -> None:
        ...

    @abstractmethod
    async def type_text(self, text: str) -> None:
        ...

    @abstractmethod
    async def get_accessibility_tree(self) -> dict:
        ...


class FakeComputerDriver(ComputerDriver):
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def screenshot(self) -> Screenshot:
        self.calls.append("screenshot")
        return Screenshot(data=b"", width=800, height=600)

    async def click(self, x: int, y: int) -> None:
        self.calls.append(f"click:{x},{y}")

    async def type_text(self, text: str) -> None:
        self.calls.append(f"type:{text}")

    async def get_accessibility_tree(self) -> dict:
        self.calls.append("a11y")
        return {}
