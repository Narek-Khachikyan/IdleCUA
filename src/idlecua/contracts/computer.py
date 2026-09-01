from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

class ComputerDriver(ABC):
    """Minimal seam over Cua on the real host. Walking skeleton: small interface, fake for tests.

    Real implementation will use accessibility tree, window/app control, and browser CDP
    with semantic refs over the owner's main Chrome profile. Every significant action
    must be verifiable by re-reading UI state.
    """

    @abstractmethod
    def screenshot(self) -> bytes:
        raise NotImplementedError

    @abstractmethod
    def click(self, x: int, y: int) -> None:
        raise NotImplementedError

    @abstractmethod
    def type_text(self, text: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def press(self, key: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def scroll(self, dx: int, dy: int) -> None:
        raise NotImplementedError

    @abstractmethod
    def open_url(self, url: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_accessibility_tree(self) -> dict:
        raise NotImplementedError

@dataclass
class FakeComputerDriver(ComputerDriver):
    """In-memory fake — records every call, performs no real action.

    Use in tests via the public Application API to prove dry-runs execute nothing.
    """

    calls: list[tuple[str, tuple, dict]] = field(default_factory=list)

    def _record(self, name: str, *args, **kwargs) -> None:
        self.calls.append((name, args, kwargs))

    def screenshot(self) -> bytes:
        self._record("screenshot")
        return b"fake-screenshot"

    def click(self, x: int, y: int) -> None:
        self._record("click", x, y)

    def type_text(self, text: str) -> None:
        self._record("type_text", text)

    def press(self, key: str) -> None:
        self._record("press", key)

    def scroll(self, dx: int, dy: int) -> None:
        self._record("scroll", dx, dy)

    def open_url(self, url: str) -> None:
        self._record("open_url", url)

    def get_accessibility_tree(self) -> dict:
        self._record("get_accessibility_tree")
        return {"role": "root", "children": []}

    def reset(self) -> None:
        self.calls.clear()
