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

    # Optional extensions — real driver implements, fake provides defaults

    def open_app(self, app_name: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def close_tab(self, tab_id: str | None = None) -> None:  # pragma: no cover
        raise NotImplementedError

    def get_window_state(self) -> dict:  # pragma: no cover
        raise NotImplementedError

    def release_all_inputs(self) -> list[str]:  # pragma: no cover
        return []

    def hotkey(self, keys: list[str]) -> None:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover
        pass

    # -- Browser surface on main Chrome profile (issue #12) — optional extensions --

    def has_browser_consent(self) -> bool:  # pragma: no cover
        """Product-level explicit consent for main-profile attachment (profile/config)."""
        return False

    def ensure_browser_ready(self, browser: str = "chrome", session: str | None = None) -> dict:  # pragma: no cover
        """Prepare existing-profile browser endpoint (requires consent and driver grant)."""
        raise NotImplementedError

    def get_browser_state(
        self,
        session: str | None = None,
        target_id: str | None = None,
        tab_id: str | None = None,
        snapshot_format: str = "semantic_v2",
    ) -> dict:  # pragma: no cover
        raise NotImplementedError

    def browser_navigate(self, url: str, tab_id: str | None = None, session: str | None = None) -> dict:  # pragma: no cover
        raise NotImplementedError

    def browser_click(
        self,
        ref: str,
        tab_id: str | None = None,
        session: str | None = None,
        input_route: str = "trusted",
    ) -> dict:  # pragma: no cover
        raise NotImplementedError

    def browser_type(
        self,
        ref: str,
        text: str,
        tab_id: str | None = None,
        session: str | None = None,
        replace: bool = False,
    ) -> dict:  # pragma: no cover
        raise NotImplementedError

    def open_browser_tab(self, url: str) -> str:  # pragma: no cover
        """Open a new agent-owned tab for url; tracks tab discipline."""
        raise NotImplementedError

    def close_all_agent_tabs(self) -> list[str]:  # pragma: no cover
        """Close only agent-owned tabs; never owner tabs. Returns closed ids."""
        return []

    def get_agent_tabs(self) -> list[str]:  # pragma: no cover
        return []

    def is_agent_tab(self, tab_id: str) -> bool:  # pragma: no cover
        return False

    def verify_browser_state(self, expected_url_contains: str | None = None) -> dict:  # pragma: no cover
        """Verify-by-reread: re-read browser/UI state and check postcondition."""
        raise NotImplementedError

@dataclass
class FakeComputerDriver(ComputerDriver):
    """In-memory fake — records every call, performs no real action.

    Use in tests via the public Application API to prove dry-runs execute nothing.
    Extends with held-input journal and tab tracking for emergency-stop / user-return tests.
    Includes Browser surface tab discipline (issue #12) — agent tabs vs owner tabs.
    """

    calls: list[tuple[str, tuple, dict]] = field(default_factory=list)
    held_keys: set[str] = field(default_factory=set)
    held_buttons: set[str] = field(default_factory=set)
    opened_tabs: list[str] = field(default_factory=list)
    agent_started_processes: list[str] = field(default_factory=list)
    input_log: list[str] = field(default_factory=list)
    # Browser tab discipline (issue #12)
    owner_tabs: list[str] = field(default_factory=list)
    agent_tabs: set[str] = field(default_factory=set)
    _agent_counts: dict[str, int] = field(default_factory=dict)  # url -> open count (handles duplicate URLs)
    browser_consent: bool = False
    _browser_target_id: str | None = None
    _browser_tabs_meta: dict[str, dict] = field(default_factory=dict)  # tab_id -> {url, refs}
    _next_tab_id: int = field(default=1)

    def _record(self, name: str, *args, **kwargs) -> None:
        self.calls.append((name, args, kwargs))

    def screenshot(self) -> bytes:
        self._record("screenshot")
        return b"fake-screenshot"

    def click(self, x: int, y: int) -> None:
        self._record("click", x, y)
        # simulate holding mouse button briefly
        self.held_buttons.add("left")
        self.input_log.append(f"click:{x},{y}")

    def type_text(self, text: str) -> None:
        self._record("type_text", text)
        self.input_log.append(f"type:{text[:20]}")

    def press(self, key: str) -> None:
        self._record("press", key)
        self.held_keys.add(key)
        self.input_log.append(f"press:{key}")

    def hotkey(self, keys: list[str]) -> None:
        self._record("hotkey", keys)
        for k in keys:
            self.held_keys.add(k)
        self.input_log.append(f"hotkey:{'+'.join(keys)}")

    def scroll(self, dx: int, dy: int) -> None:
        self._record("scroll", dx, dy)
        self.input_log.append(f"scroll:{dx},{dy}")

    def open_url(self, url: str) -> None:
        self._record("open_url", url)
        self.opened_tabs.append(url)
        self.agent_tabs.add(url)
        self._agent_counts[url] = self._agent_counts.get(url, 0) + 1
        self.agent_started_processes.append(f"tab:{url}")
        self.input_log.append(f"open_url:{url}")
        # Also track in browser meta for get_browser_state
        tab_id = url
        # Keep count in meta as well for duplicate handling? Use url as key but track count
        if url not in self._browser_tabs_meta:
            self._browser_tabs_meta[tab_id] = {"url": url, "title": url, "refs": ["p1:0", "p1:1"], "count": 1}
        else:
            self._browser_tabs_meta[tab_id]["count"] = self._browser_tabs_meta[tab_id].get("count", 1) + 1

    # -- owner tab simulation helpers (for discipline tests) --

    def add_owner_tab_for_test(self, url: str) -> None:
        """Simulate an owner tab that the agent must never close."""
        self.owner_tabs.append(url)
        if url not in self._browser_tabs_meta:
            self._browser_tabs_meta[url] = {"url": url, "title": f"Owner: {url}", "refs": []}

    def set_browser_consent_for_test(self, granted: bool) -> None:
        self.browser_consent = bool(granted)

    def get_accessibility_tree(self) -> dict:
        self._record("get_accessibility_tree")
        return {"role": "root", "children": [{"role": "tab", "url": u} for u in self.opened_tabs[-3:]]}

    def open_app(self, app_name: str) -> None:
        self._record("open_app", app_name)
        self.agent_started_processes.append(f"app:{app_name}")

    def close_tab(self, tab_id: str | None = None) -> None:
        self._record("close_tab", tab_id)
        # Tab discipline: only close if it's an agent-owned tab; never owner tabs
        if tab_id is not None:
            if tab_id in self.agent_tabs:
                # Handle duplicate counts
                cnt = self._agent_counts.get(tab_id, 1)
                if cnt > 1:
                    self._agent_counts[tab_id] = cnt - 1
                    # Keep in set, just remove one occurrence from list
                else:
                    self.agent_tabs.discard(tab_id)
                    self._agent_counts.pop(tab_id, None)
                    self._browser_tabs_meta.pop(tab_id, None)
                if tab_id in self.opened_tabs:
                    self.opened_tabs.remove(tab_id)
                # do not touch owner_tabs
                return
            if tab_id in self.owner_tabs:
                # Refuse to close owner tab — record but do not mutate
                self._record("close_tab_refused_owner", tab_id)
                self.input_log.append(f"close_tab_refused_owner:{tab_id}")
                return
            # Unknown tab_id — try to remove if in opened_tabs but not in agent set? Treat as no-op
            if tab_id in self.opened_tabs:
                # It is not in agent set, so refuse
                self._record("close_tab_refused_unknown", tab_id)
                return
            return
        # No id given: close most recent agent tab, never owner tab
        if self.opened_tabs:
            # Find last agent tab in opened_tabs order
            for url in reversed(self.opened_tabs):
                if url in self.agent_tabs:
                    cnt = self._agent_counts.get(url, 1)
                    if cnt > 1:
                        self._agent_counts[url] = cnt - 1
                    else:
                        self.agent_tabs.discard(url)
                        self._agent_counts.pop(url, None)
                        self._browser_tabs_meta.pop(url, None)
                    self.opened_tabs.remove(url)
                    return
            # No agent tabs to close — do not touch owner tabs
            self._record("close_tab_no_agent_tabs", None)

    def get_window_state(self) -> dict:
        self._record("get_window_state")
        return {"tabs": list(self.opened_tabs), "owner_tabs": list(self.owner_tabs), "agent_tabs": list(self.agent_tabs), "held_keys": list(self.held_keys), "held_buttons": list(self.held_buttons)}

    # -- Browser surface (issue #12) --

    def has_browser_consent(self) -> bool:
        return bool(self.browser_consent)

    def ensure_browser_ready(self, browser: str = "chrome", session: str | None = None) -> dict:
        self._record("ensure_browser_ready", browser, session)
        if not self.has_browser_consent():
            raise PermissionError(
                "Browser main-profile consent not granted. Run `idle-cua profile grant-browser` "
                "and ensure driver grant `cua-driver serve --grant existing-profile`."
            )
        # Simulate successful preparation
        self._browser_target_id = f"fake-target-{browser}"
        self.input_log.append(f"browser_prepare:{browser}:{session}")
        return {"target_id": self._browser_target_id, "session": session or "idlecua", "browser": browser}

    def get_browser_state(
        self,
        session: str | None = None,
        target_id: str | None = None,
        tab_id: str | None = None,
        snapshot_format: str = "semantic_v2",
    ) -> dict:
        self._record("get_browser_state", session, target_id, tab_id, snapshot_format)
        # Return fake semantic refs
        if tab_id and tab_id in self._browser_tabs_meta:
            meta = self._browser_tabs_meta[tab_id]
            return {
                "target_id": target_id or self._browser_target_id or "fake-target",
                "tab_id": tab_id,
                "url": meta["url"],
                "title": meta.get("title", ""),
                "refs": meta.get("refs", ["p1:0"]),
                "snapshot_format": snapshot_format,
                "tabs": list(self._browser_tabs_meta.keys()),
            }
        # General state
        return {
            "target_id": self._browser_target_id or "fake-target",
            "tabs": [{"tab_id": tid, "url": m["url"]} for tid, m in self._browser_tabs_meta.items()],
            "agent_tabs": list(self.agent_tabs),
            "owner_tabs": list(self.owner_tabs),
            "snapshot_format": snapshot_format,
        }

    def browser_navigate(self, url: str, tab_id: str | None = None, session: str | None = None) -> dict:
        self._record("browser_navigate", url, tab_id, session)
        # Enforce policy-like allowlist/deny-zone check here as well (defense)
        # For fake, just navigate; real enforcement is in PolicyEngine
        if tab_id:
            if tab_id not in self._browser_tabs_meta:
                # Create new entry if navigating unknown tab (new tab)
                self._browser_tabs_meta[tab_id] = {"url": url, "title": url, "refs": ["p1:0"]}
            else:
                self._browser_tabs_meta[tab_id]["url"] = url
        else:
            # No tab_id: treat as new agent tab
            tid = url
            self.open_browser_tab(url)
            return {"effect": "navigated", "url": url, "tab_id": tid}
        self.input_log.append(f"browser_navigate:{url}:{tab_id}")
        # verify-by-reread simulation: return state after navigate
        return {"effect": "navigated", "url": url, "tab_id": tab_id, "route": "trusted"}

    def browser_click(
        self,
        ref: str,
        tab_id: str | None = None,
        session: str | None = None,
        input_route: str = "trusted",
    ) -> dict:
        self._record("browser_click", ref, tab_id, session, input_route)
        # Simulate trusted -> fallback to dom_event on synthetic limitation
        use_route = input_route
        if input_route == "trusted":
            # Simulate possible fallback: 10% chance of needing synthetic? For deterministic, we succeed with trusted
            # But we implement fallback logic in real driver; fake always succeeds
            pass
        self.input_log.append(f"browser_click:{ref}:{tab_id}:{use_route}")
        return {"effect": "confirmed" if use_route == "trusted" else "unverifiable", "route": use_route, "ref": ref}

    def browser_type(
        self,
        ref: str,
        text: str,
        tab_id: str | None = None,
        session: str | None = None,
        replace: bool = False,
    ) -> dict:
        self._record("browser_type", ref, text, tab_id, session, replace)
        self.input_log.append(f"browser_type:{ref}:{text[:20]}:{tab_id}")
        return {"effect": "confirmed", "route": "trusted", "ref": ref}

    def open_browser_tab(self, url: str) -> str:
        self._record("open_browser_tab", url)
        tab_id = url  # use url as id for fake (keeps API URL-based)
        self.opened_tabs.append(url)
        self.agent_tabs.add(tab_id)
        self._agent_counts[tab_id] = self._agent_counts.get(tab_id, 0) + 1
        if tab_id not in self._browser_tabs_meta:
            self._browser_tabs_meta[tab_id] = {"url": url, "title": f"Tab: {url}", "refs": ["p1:0", "p1:1", "p1:2"], "count": 1}
        else:
            self._browser_tabs_meta[tab_id]["count"] = self._browser_tabs_meta[tab_id].get("count", 1) + 1
        self.agent_started_processes.append(f"tab:{url}")
        self.input_log.append(f"open_browser_tab:{url}")
        return tab_id

    def close_all_agent_tabs(self) -> list[str]:
        self._record("close_all_agent_tabs")
        closed = list(self.agent_tabs)
        for tid in closed:
            # Remove all occurrences (duplicate URLs can appear as separate list entries but set dedupes)
            while tid in self.opened_tabs:
                self.opened_tabs.remove(tid)
            self._browser_tabs_meta.pop(tid, None)
        self.agent_tabs.clear()
        self._agent_counts.clear()
        # Also clean any remaining opened_tabs that were agent tabs but with duplicate tracking issues
        # Ensure no agent tab remains in opened_tabs
        self.input_log.append(f"close_all_agent_tabs:{','.join(closed)}")
        return closed

    def get_agent_tabs(self) -> list[str]:
        self._record("get_agent_tabs")
        return list(self.agent_tabs)

    def is_agent_tab(self, tab_id: str) -> bool:
        return tab_id in self.agent_tabs

    def verify_browser_state(self, expected_url_contains: str | None = None) -> dict:
        self._record("verify_browser_state", expected_url_contains)
        state = self.get_browser_state()
        ok = True
        if expected_url_contains:
            ok = any(expected_url_contains in (m.get("url", "") or "") for m in self._browser_tabs_meta.values())
        result = {"verified": ok, "expected": expected_url_contains, "state": state}
        self.input_log.append(f"verify:{expected_url_contains}:{ok}")
        return result

    def release_all_inputs(self) -> list[str]:
        released: list[str] = []
        for k in list(self.held_keys):
            released.append(f"key:{k}")
        self.held_keys.clear()
        for b in list(self.held_buttons):
            released.append(f"button:{b}")
        self.held_buttons.clear()
        if released:
            self._record("release_all_inputs", released)
            self.input_log.append(f"release_all:{','.join(released)}")
        return released

    def reset(self) -> None:
        self.calls.clear()
        self.held_keys.clear()
        self.held_buttons.clear()
        self.opened_tabs.clear()
        self.agent_started_processes.clear()
        self.input_log.clear()
        self.owner_tabs.clear()
        self.agent_tabs.clear()
        self._agent_counts.clear()
        self._browser_tabs_meta.clear()
        self.browser_consent = False
        self._browser_target_id = None

    # helpers for tests

    def hold_for_test(self, key: str = "Shift", button: str = "left") -> None:
        """Simulate held input without a driver call (for emergency-stop tests)."""
        self.held_keys.add(key)
        self.held_buttons.add(button)

    def terminate_agent_processes(self) -> list[str]:
        """Terminate only agent-started processes (not user ones)."""
        terminated = list(self.agent_started_processes)
        self.agent_started_processes.clear()
        # Also close agent tabs
        self.opened_tabs.clear()
        self._record("terminate_agent_processes", terminated)
        return terminated
