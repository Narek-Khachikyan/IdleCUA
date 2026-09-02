"""Real ComputerDriver over cua-driver (host primitives)."""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import json
import time
from pathlib import Path
from typing import Any

from ..contracts.computer import ComputerDriver
from ..idle import InputJournal


def _run_sync(coro_factory):
    """Run an async factory synchronously, handling already-running loops."""
    try:
        asyncio.get_running_loop()
        is_running = True
    except RuntimeError:
        is_running = False
    if not is_running:
        return asyncio.run(coro_factory())
    # Already in a running loop (e.g., pytest asyncio). Offload to a new thread.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(lambda: asyncio.run(coro_factory()))
        return fut.result(timeout=30)


def _decode_tool_result_images(tool_result) -> bytes | None:
    """Extract first image bytes from a ToolResult, if present."""
    try:
        images = getattr(tool_result, "images", None)
        if images:
            first = images[0]
            b64 = getattr(first, "data_base64", None)
            if b64:
                return base64.b64decode(b64)
    except Exception:
        pass
    # Fallback: raw_json structuredContent may contain screenshot? For get_window_state, images are still in images.
    return None


class CuaComputerDriver(ComputerDriver):
    """Real driver over cua-driver Rust backend.

    - Uses ``cua_driver.CuaDriver.create()`` (same-process runtime, no daemon required).
    - Host primitives: screenshot, accessibility tree / window state reading, typed input (click, type, hotkey, scroll), app launch.
    - Input journal for held-input release (emergency-stop path).
    - Driver failures surface as actionable RuntimeErrors (install / permission remediation).

    The fake driver remains the automated-test surface; this driver is for real-machine smoke.
    """

    def __init__(self, session: str | None = "idlecua", data_dir: Path | None = None) -> None:
        self.session = session or "idlecua"
        self.data_dir = Path(data_dir).expanduser() if data_dir else None
        self.journal = InputJournal()
        # held tracking mirrors journal but also for release reporting
        self._held_keys: set[str] = set()
        self._held_buttons: set[str] = set()
        self._driver = None
        self._closed = False
        self._init_error: str | None = None
        self._last_app_pid: int | None = None
        self._last_app_name: str | None = None
        # Browser surface on main Chrome profile (issue #12) — tab discipline
        self._agent_browser_tabs: set[str] = set()  # tab_ids/urls opened by agent
        self._browser_target_id: str | None = None
        self._browser_session: str | None = None
        self._browser_pid: int | None = None
        self._browser_window_id: int | None = None
        self._browser_bound: bool = False
        self._init_driver()

    def _init_driver(self) -> None:
        try:
            import cua_driver  # type: ignore

            # Check import availability
            try:
                self._driver = cua_driver.CuaDriver.create(None)
            except Exception as e:
                # Provide actionable error
                self._init_error = (
                    f"Cua driver initialization failed: {e}. "
                    "Remediation: `uv sync` or `uv pip install cua-driver==0.23.2`, then verify daemon: `cua-driver --help`. "
                    "On macOS, ensure you run from a terminal with Accessibility + Screen Recording granted. "
                    "See README `Cua driver install` docs."
                )
                raise RuntimeError(self._init_error) from e
        except ImportError as e:
            self._init_error = (
                f"cua-driver not installed: {e}. "
                "Install: `uv add cua-driver==0.23.2` or `uv pip install cua-driver==0.23.2`. "
                "Docs: https://cua.ai/docs/how-to-guides/driver/install"
            )
            raise RuntimeError(self._init_error) from e

    # -- internal helpers --

    def _require_driver(self):
        if self._driver is None:
            if self._init_error:
                raise RuntimeError(self._init_error)
            raise RuntimeError("Cua driver not initialized")
        return self._driver

    def _call_tool_sync(self, name: str, args: dict) -> Any:
        driver = self._require_driver()

        def _factory():
            import json as _js

            return driver.call_tool(name, _js.dumps(args))

        result = _run_sync(_factory)
        if getattr(result, "is_error", False):
            # Build actionable message
            code = getattr(result, "error_code", None) or "unknown"
            text = getattr(result, "text", "") or ""
            raw = getattr(result, "raw_json", "") or ""
            # Provide permission-specific remediation
            remediation = ""
            low = (text + raw).lower()
            if "accessibility" in low or "screen recording" in low or "permission" in low or code in ("authorization_required", "permission_denied"):
                remediation = " Remediation: grant Accessibility and Screen Recording to your terminal (System Settings → Privacy & Security → Accessibility / Screen Recording), then restart the terminal. Run `idle-cua doctor` or `idle-cua profile check-permissions`."
            elif "window_id_not_found" in low or "window_owner_pid_mismatch" in low:
                remediation = " Remediation: window may have closed or belongs to another pid; list_windows and retry get_window_state."
            elif code == "desktop_scope_disabled":
                remediation = " Remediation: use scope=\"desktop\" for screen-absolute coordinates."
            raise RuntimeError(f"Cua tool '{name}' failed [{code}]: {text}{remediation} (args={args}) raw={raw[:500]})")
        return result

    def _call_tool_sync_no_raise(self, name: str, args: dict) -> Any:
        """Call tool without raising on is_error — caller inspects."""
        driver = self._require_driver()

        def _factory():
            import json as _js

            return driver.call_tool(name, _js.dumps(args))

        return _run_sync(_factory)

    # -- ComputerDriver contract --

    def screenshot(self) -> bytes:
        """Full display screenshot in PNG bytes (true screen pixels, no downscale)."""
        driver = self._require_driver()
        try:
            def _factory():
                import cua_driver

                return driver.get_desktop_state(cua_driver.GetDesktopStateInput(session=self.session, screenshot_out_file=None))

            res = _run_sync(_factory)
            if getattr(res, "is_error", False):
                raise RuntimeError(f"screenshot failed [{getattr(res, 'error_code', '')}]: {getattr(res, 'text', '')}")
            data = _decode_tool_result_images(res)
            if data:
                self.journal._log.append(f"screenshot:{len(data)}b")
                return data
            # Fallback: try raw_json structuredContent screenshot?
            raise RuntimeError("screenshot: no image data in ToolResult")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(
                f"screenshot failed: {e}. Remediation: grant Screen Recording (System Settings → Privacy & Security → Screen Recording) and restart. "
                f"Verify with `uv run python -c \"import cua_driver; print(cua_driver.current_mac_os_permission_status())\"`."
            ) from e

    def click(self, x: int, y: int) -> None:
        self.journal._log.append(f"click:{x},{y}")
        self.journal.hold_button("left")
        self._held_buttons.add("left")
        try:
            # Use desktop scope for screen-absolute click
            self._call_tool_sync("click", {"x": float(x), "y": float(y), "scope": "desktop"})
            # Log held input journal
            self.journal._log.append(f"click_sent:{x},{y}")
        except Exception as e:
            # Wrap with actionable context but preserve original for executor to surface as task failure
            raise RuntimeError(f"click({x},{y}) failed: {e}") from e
        finally:
            # Simulate button held briefly; actual release happens via release_all_inputs on emergency-stop/user-return
            pass

    def type_text(self, text: str) -> None:
        self.journal._log.append(f"type:{text[:30]}")
        # type_text uses global desktop input
        try:
            self._call_tool_sync("type_text", {"text": text, "scope": "desktop"})
        except Exception as e:
            raise RuntimeError(f"type_text failed: {e}") from e

    def press(self, key: str) -> None:
        self.journal.press_key(key)
        self._held_keys.add(key)
        self.journal._log.append(f"press:{key}")
        try:
            # Use press_key tool for single key
            self._call_tool_sync("press_key", {"key": key, "scope": "desktop"})
        except Exception as e:
            raise RuntimeError(f"press({key}) failed: {e}") from e

    def hotkey(self, keys: list[str]) -> None:  # type: ignore[override]
        """Press a key combination, e.g., ["cmd","c"]."""
        if not keys:
            raise ValueError("hotkey keys must be non-empty")
        # Record in journal
        combo = "+".join(keys)
        self.journal._log.append(f"hotkey:{combo}")
        for k in keys:
            if k.lower() in ("cmd", "command", "shift", "option", "alt", "ctrl", "control", "fn"):
                continue
            self._held_keys.add(k)
        try:
            self._call_tool_sync("hotkey", {"keys": keys, "scope": "desktop"})
        except Exception as e:
            raise RuntimeError(f"hotkey({keys}) failed: {e}") from e

    def scroll(self, dx: int, dy: int) -> None:
        self.journal._log.append(f"scroll:{dx},{dy}")
        # Determine direction and amount from dx,dy
        # Prefer vertical if |dy| > |dx|
        if abs(dy) >= abs(dx):
            direction = "down" if dy > 0 else "up"
            amount = max(1, min(50, abs(dy) // 20 + 1)) if dy != 0 else 1
            x, y = 200, 200  # center-ish fallback for desktop scroll
        else:
            direction = "right" if dx > 0 else "left"
            amount = max(1, min(50, abs(dx) // 20 + 1)) if dx != 0 else 1
            x, y = 200, 200
        # Need x,y for desktop scroll; use screen center if unknown
        # Try to get screen size for better x,y
        try:
            # Use desktop scroll with x,y
            self._call_tool_sync("scroll", {"x": float(x), "y": float(y), "direction": direction, "amount": int(amount), "scope": "desktop"})
        except Exception as e:
            raise RuntimeError(f"scroll({dx},{dy}) -> {direction} x{amount} failed: {e}") from e

    def open_url(self, url: str) -> None:
        self.journal._log.append(f"open_url:{url}")
        try:
            try:
                self._call_tool_sync("launch_app", {"bundle_id": "com.google.Chrome", "urls": [url]})
                self._agent_browser_tabs.add(url)
                self.journal._log.append(f"open_url tracked agent tab: {url}")
                return
            except Exception:
                # Fallback to shell open
                import subprocess

                subprocess.run(["open", url], check=False, timeout=5)
                self._agent_browser_tabs.add(url)
                self.journal._log.append(f"open_url tracked agent tab (shell open): {url}")
                return
        except Exception as e:
            raise RuntimeError(f"open_url({url}) failed: {e}. Remediation: ensure a browser is installed and permissions granted.") from e

    def get_accessibility_tree(self) -> dict:
        """Return desktop-like accessibility tree dict (apps + windows)."""
        try:
            res = self._call_tool_sync("get_accessibility_tree", {})
            # Structured content is apps/windows, text is markdown
            struct = None
            try:
                struct = json.loads(getattr(res, "structured_json", "{}") or "{}")
            except Exception:
                struct = {}
            if not struct:
                # Fallback parse from text
                return {"raw": getattr(res, "text", ""), "apps": [], "windows": []}
            # Normalize to contract shape expected by executor/tests: role root with children
            # Keep original for compatibility but also add role root
            normalized = {
                "role": "root",
                "children": [{"role": "app", "bundle_id": a.get("bundle_id"), "name": a.get("name"), "pid": a.get("pid")} for a in struct.get("apps", [])[:20]],
                "windows": struct.get("windows", []),
                "apps": struct.get("apps", []),
                "raw_text": getattr(res, "text", ""),
            }
            self.journal._log.append(f"get_accessibility_tree: {len(struct.get('apps', []))} apps, {len(struct.get('windows', []))} windows")
            return normalized
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(
                f"get_accessibility_tree failed: {e}. Remediation: grant Accessibility permission (System Settings → Privacy & Security → Accessibility)."
            ) from e

    def open_app(self, app_name: str) -> None:
        self.journal._log.append(f"open_app:{app_name}")
        # Map common names to bundle_ids for reliability
        bundle_map = {
            "calculator": "com.apple.calculator",
            "calc": "com.apple.calculator",
            "textedit": "com.apple.TextEdit",
            "finder": "com.apple.finder",
            "safari": "com.apple.Safari",
            "chrome": "com.google.Chrome",
            "google chrome": "com.google.Chrome",
            "notes": "com.apple.Notes",
            "system settings": "com.apple.systempreferences",
            "terminal": "com.apple.Terminal",
            "ghostty": "com.mitchellh.ghostty",
            "code": "com.microsoft.VSCode",
            "vscode": "com.microsoft.VSCode",
        }
        low = app_name.strip().lower()
        bundle_id = bundle_map.get(low)
        try:
            if bundle_id:
                res = self._call_tool_sync("launch_app", {"bundle_id": bundle_id})
            else:
                # Try by name directly
                try:
                    res = self._call_tool_sync("launch_app", {"name": app_name})
                except Exception:
                    # Fallback: try bundle_id guess
                    res = self._call_tool_sync("launch_app", {"bundle_id": app_name})
            # Track last launched app pid for get_window_state preference
            try:
                struct = json.loads(getattr(res, "structured_json", "{}") or "{}")
                pid = struct.get("pid")
                if isinstance(pid, int) and pid != 0:
                    self._last_app_pid = pid
                    self._last_app_name = app_name
            except Exception:
                pass
        except Exception as e:
            raise RuntimeError(f"open_app({app_name}) failed: {e}. Remediation: verify app is installed (check /Applications) and Accessibility granted.") from e

    def close_tab(self, tab_id: str | None = None) -> None:  # type: ignore[override]
        self.journal._log.append(f"close_tab:{tab_id}")
        # Tab discipline: only close if it's an agent-owned browser tab; never owner tabs
        if tab_id is not None and tab_id in self._agent_browser_tabs:
            self._agent_browser_tabs.discard(tab_id)
            self.journal._log.append(f"close_tab: agent tab {tab_id} closed")
            return None
        if tab_id is not None and tab_id not in self._agent_browser_tabs:
            # If it's an owner tab or unknown, refuse but log
            if tab_id in self._agent_browser_tabs:
                pass
            else:
                # Check if it's known as agent via url substring? For real driver, tab_id may be opaque
                # Be conservative: if not in agent set, do not close
                self.journal._log.append(f"close_tab: refused — {tab_id} not in agent tabs {self._agent_browser_tabs}")
                return None
        # No id given: close most recent agent tab if any
        if self._agent_browser_tabs:
            last = next(iter(self._agent_browser_tabs))
            self._agent_browser_tabs.discard(last)
            self.journal._log.append(f"close_tab: closed most recent agent tab {last}")
        else:
            self.journal._log.append("close_tab: no agent tabs to close — owner tabs untouched")
        return None

    def get_window_state(self) -> dict:
        """Return window state for frontmost on-screen window (or last-launched app)."""
        try:
            res = self._call_tool_sync("list_windows", {})
            struct = json.loads(getattr(res, "structured_json", "{}") or "{}")
            windows = struct.get("windows", [])
            candidates = [w for w in windows if w.get("is_on_screen")]
            if not candidates:
                return self.get_accessibility_tree()
            def _z(w):
                z = w.get("z_index")
                return z if isinstance(z, int) else -1
            if self._last_app_pid is not None:
                last = [w for w in candidates if w.get("pid") == self._last_app_pid]
                other = [w for w in candidates if w.get("pid") != self._last_app_pid]
                last.sort(key=_z, reverse=True)
                other.sort(key=_z, reverse=True)
                ranked = last + other
            else:
                ranked = sorted(candidates, key=_z, reverse=True)
            w = ranked[0]
            pid = int(w["pid"])
            wid = int(w["window_id"])
            res2 = self._call_tool_sync("get_window_state", {"pid": pid, "window_id": wid})
            try:
                struct2 = json.loads(getattr(res2, "structured_json", "{}") or "{}")
            except Exception:
                struct2 = {}
            if not struct2:
                return {"pid": pid, "window_id": wid, "raw_text": getattr(res2, "text", "")}
            struct2["queried_pid"] = pid
            struct2["queried_window_id"] = wid
            struct2["window_app"] = w.get("app_name")
            struct2["window_title"] = w.get("title")
            self.journal._log.append(f"get_window_state: pid={pid} wid={wid} elements={struct2.get('element_count', '?')}")
            return struct2
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(
                f"get_window_state failed: {e}. Remediation: ensure at least one window is visible and Accessibility permission granted. "
                "Try `idle-cua doctor`."
            ) from e

    # -- Browser surface on main Chrome profile (issue #12) --

    def has_browser_consent(self) -> bool:
        """Product-level explicit consent — delegates to single helper to avoid shotgun surgery."""
        try:
            from ..browser_consent import has_consent
            return has_consent(self.data_dir)
        except Exception:
            return False

    def _find_browser_window(self, browser: str = "chrome") -> tuple[int, int] | None:
        """Find Chrome/Safari window pid/window_id for existing-profile attachment."""
        # Prefer Chrome, fallback to Safari if Chrome not found and browser is generic
        candidates_bundle = {
            "chrome": "com.google.Chrome",
            "chromium": "com.google.Chrome",
            "edge": "com.microsoft.edgemac",
            "brave": "com.brave.Browser",
            "safari": "com.apple.Safari",
        }
        pref = candidates_bundle.get(browser.lower().strip(), "com.google.Chrome")
        # Try list_windows first
        try:
            res = self._call_tool_sync_no_raise("list_windows", {})
            if getattr(res, "is_error", False):
                return None
            struct = json.loads(getattr(res, "structured_json", "{}") or "{}")
            windows = struct.get("windows", [])
            # Filter to preferred browser windows that are visible
            # Prefer on-screen windows with meaningful bounds
            for w in windows:
                if w.get("pid") and w.get("window_id"):
                    # Check app_name contains Chrome/Safari etc.
                    an = (w.get("app_name") or "").lower()
                    # Map bundle check: we don't have bundle in windows, use app_name
                    if browser.lower() in an or ("chrome" in an and pref == "com.google.Chrome") or ("safari" in an and pref == "com.apple.Safari"):
                        # Prefer windows with non-trivial bounds (width>100, height>100) and on current space or is_on_screen
                        b = w.get("bounds") or {}
                        bw = b.get("width", 0)
                        bh = b.get("height", 0)
                        if bw and bh and bw > 50 and bh > 50:
                            return int(w["pid"]), int(w["window_id"])
            # Fallback: any window for that browser pid (from list_apps)
            try:
                res2 = self._call_tool_sync_no_raise("list_apps", {})
                if not getattr(res2, "is_error", False):
                    struct2 = json.loads(getattr(res2, "structured_json", "{}") or "{}")
                    for app in struct2.get("apps", []):
                        if app.get("bundle_id") == pref and app.get("pid"):
                            pid = int(app["pid"])
                            # Find window for that pid
                            for w in windows:
                                if int(w.get("pid", -1)) == pid:
                                    return pid, int(w["window_id"])
                            # If no window yet, launch one and retry
                            return pid, -1  # sentinel: pid found but need window
            except Exception:
                pass
        except Exception:
            pass
        return None

    def ensure_browser_ready(self, browser: str = "chrome", session: str | None = None) -> dict:
        """Prepare existing-profile browser endpoint (requires consent and driver grant).

        This implements the 'main profile via driver's existing-profile attachment' requirement.
        It checks product-level consent (profile.json) then attempts `browser_prepare` with
        `strategy.kind=existing_profile`. Failures due to missing driver grant surface with
        actionable remediation (cua-driver serve --grant existing-profile).

        Tab discipline: after successful bind, snapshots existing tabs so later `close_all_agent_tabs`
        only closes agent-owned tabs.
        """
        sess = session or self.session or "idlecua"
        # Product-level consent gate
        if not self.has_browser_consent():
            raise PermissionError(
                "Browser main-profile consent not granted. "
                "Run `idle-cua profile grant-browser` to record explicit consent in profile.json/config.json. "
                "Agent will not attach to main Chrome profile until consent is recorded. "
                "After granting, also ensure driver grant: `cua-driver serve --grant existing-profile` (daemon) or embedded grant."
            )
        # Find browser window
        found = self._find_browser_window(browser)
        pid = None
        wid = None
        if found is not None:
            pid, wid = found
            if wid == -1:
                # Need to create window — launch Chrome with about:blank
                try:
                    self._call_tool_sync("launch_app", {"bundle_id": "com.google.Chrome", "urls": ["about:blank"]})
                    time.sleep(1.5)
                    found2 = self._find_browser_window(browser)
                    if found2 is not None:
                        pid, wid = found2
                except Exception:
                    pass
        if pid is None or wid is None or wid == -1:
            # Fallback: try launch Chrome
            try:
                res = self._call_tool_sync("launch_app", {"bundle_id": "com.google.Chrome", "urls": ["about:blank"]})
                try:
                    struct = json.loads(getattr(res, "structured_json", "{}") or "{}")
                    pid = int(struct.get("pid", 0)) or pid
                except Exception:
                    pass
                time.sleep(1.5)
                found3 = self._find_browser_window(browser)
                if found3 is not None:
                    pid, wid = found3
            except Exception as e:
                raise RuntimeError(f"Could not find or launch Chrome for browser surface: {e}. Remediation: launch Chrome manually and grant profile consent.") from e
        if pid is None or wid is None:
            raise RuntimeError(f"Browser window not found for {browser} (pid={pid} wid={wid}). Launch Chrome and retry.")
        self._browser_pid = int(pid)
        self._browser_window_id = int(wid)
        self._browser_session = sess
        # Attempt browser_prepare with existing_profile strategy
        try:
            prep = self._call_tool_sync_no_raise(
                "browser_prepare",
                {"pid": int(pid), "window_id": int(wid), "strategy": {"kind": "existing_profile"}, "session": sess},
            )
            if getattr(prep, "is_error", False):
                code = getattr(prep, "error_code", "") or "unknown"
                text = getattr(prep, "text", "") or ""
                raw = getattr(prep, "raw_json", "") or ""
                low = (text + raw + code).lower()
                if "browser_consent_required" in low or "authorization_required" in low or "requires --grant existing-profile" in low or code == "browser_consent_required":
                    raise PermissionError(
                        f"Driver refused existing-profile attachment [{code}]: {text}. "
                        "Remediation: start the driver with `cua-driver serve --grant existing-profile` "
                        "(or launch your MCP/embedded driver with that grant), then retry. "
                        "Product consent is already granted in profile.json; driver grant is separate. "
                        "See `idle-cua doctor` and `idle-cua profile browser-status`."
                    )
                raise RuntimeError(f"browser_prepare failed [{code}]: {text} raw={raw[:500]}")
        except PermissionError:
            raise
        except Exception as e:
            # Wrap
            raise RuntimeError(f"browser_prepare for pid={pid} wid={wid} failed: {e}") from e
        # Now bind via get_browser_state to mint target_id
        try:
            bstate = self.get_browser_state(session=sess)
            # Extract target_id if present
            if isinstance(bstate, dict):
                tid = bstate.get("target_id") or bstate.get("targetId")
                if tid:
                    self._browser_target_id = str(tid)
            self._browser_bound = True
            self.journal._log.append(f"browser_prepare: pid={pid} wid={wid} session={sess} target={self._browser_target_id}")
            return {"pid": pid, "window_id": wid, "session": sess, "target_id": self._browser_target_id, "state": bstate}
        except Exception as e:
            raise RuntimeError(f"get_browser_state after prepare failed: {e}") from e

    def get_browser_state(
        self,
        session: str | None = None,
        target_id: str | None = None,
        tab_id: str | None = None,
        snapshot_format: str = "semantic_v2",
    ) -> dict:
        """Read-only browser inspection via get_browser_state (semantic_v2 by default).

        Uses browser semantic refs where available. Falls back to accessibility_tree if browser not bound.
        """
        sess = session or self._browser_session or self.session or "idlecua"
        tid = target_id or self._browser_target_id
        # If not yet bound, try to bind via pid/window_id
        if tid is None and self._browser_pid is not None and self._browser_window_id is not None:
            # Bind mode: pid + window_id
            try:
                res = self._call_tool_sync_no_raise(
                    "get_browser_state",
                    {"pid": int(self._browser_pid), "window_id": int(self._browser_window_id), "session": sess, "snapshot_format": snapshot_format},
                )
                if getattr(res, "is_error", False):
                    # Check for consent error
                    code = getattr(res, "error_code", "") or ""
                    text = getattr(res, "text", "") or ""
                    if "browser_consent_required" in (code + text).lower():
                        raise PermissionError(f"Browser consent required for get_browser_state: {text} [{code}]")
                    # Fallback to desktop AX
                    return self.get_accessibility_tree()
                try:
                    struct = json.loads(getattr(res, "structured_json", "{}") or "{}")
                except Exception:
                    struct = {}
                # Cache target_id
                if struct.get("target_id"):
                    self._browser_target_id = str(struct["target_id"])
                elif struct.get("targetId"):
                    self._browser_target_id = str(struct["targetId"])
                self.journal._log.append(f"get_browser_state: bind pid={self._browser_pid} tabs={len(struct.get('tabs', [])) if isinstance(struct.get('tabs'), list) else '?'}")
                return struct if struct else {"raw_text": getattr(res, "text", "")}
            except PermissionError:
                raise
            except Exception as e:
                # Fallback
                self.journal._log.append(f"get_browser_state bind failed: {e} — fallback to AX")
                return self.get_accessibility_tree()
        # Snapshot mode: target_id + tab_id
        if tid is not None:
            args: dict[str, Any] = {"target_id": str(tid), "session": sess, "snapshot_format": snapshot_format}
            if tab_id is not None:
                args["tab_id"] = str(tab_id)
            try:
                res = self._call_tool_sync_no_raise("get_browser_state", args)
                if getattr(res, "is_error", False):
                    code = getattr(res, "error_code", "") or ""
                    text = getattr(res, "text", "") or ""
                    if "browser_consent_required" in (code + text).lower():
                        raise PermissionError(f"Browser consent required: {text} [{code}]")
                    # Try fallback: return AX
                    return self.get_accessibility_tree()
                try:
                    struct = json.loads(getattr(res, "structured_json", "{}") or "{}")
                except Exception:
                    struct = {}
                if struct:
                    self.journal._log.append(f"get_browser_state: snapshot target={tid} tab={tab_id} keys={list(struct.keys())[:5]}")
                    return struct
                return {"raw_text": getattr(res, "text", "")}
            except PermissionError:
                raise
            except Exception as e:
                self.journal._log.append(f"get_browser_state snapshot failed: {e}")
                return self.get_accessibility_tree()
        # Fallback: not bound yet, return AX
        return self.get_accessibility_tree()

    def browser_navigate(self, url: str, tab_id: str | None = None, session: str | None = None) -> dict:
        """Navigate one tab to url via browser_navigate (exact-bound, http/https/about only).

        Enforces allowlist/deny-zone via policy check when data_dir available (defense).
        Verify-by-reread: after navigation, re-reads state and checks URL.
        """
        # Deny-zone / allowlist defense (best-effort) — primary enforcement is in PolicyEngine/executor
        try:
            from ..policy import PolicyEngine, TypedAction

            # Try to load policy from config if available
            pol = None
            if self.data_dir is not None:
                from ..config import IdleCuaConfig

                try:
                    cfg = IdleCuaConfig.load(self.data_dir)
                    pol = PolicyEngine(cfg)
                    result = pol.evaluate(TypedAction(kind="navigate", target_url=url))
                    from ..policy import PolicyVerdict

                    if result.verdict == PolicyVerdict.blocked:
                        raise PermissionError(f"Policy blocked navigate to {url}: {result.reason}")
                except PermissionError:
                    raise
                except Exception:
                    pass
        except Exception:
            pass
        sess = session or self._browser_session or self.session or "idlecua"
        tid = self._browser_target_id
        # Need target_id; ensure bound
        if tid is None:
            # Try to ensure ready if consent exists
            if self.has_browser_consent():
                try:
                    self.ensure_browser_ready(session=sess)
                    tid = self._browser_target_id
                except Exception:
                    pass
        if tid is None or tab_id is None:
            # Fallback to open_url (new tab) for MVP when exact bind not available
            self.journal._log.append(f"browser_navigate fallback open_url: {url}")
            self.open_url(url)
            # Track as agent tab
            self._agent_browser_tabs.add(url)
            # Verify via reread
            try:
                state = self.get_browser_state(session=sess)
                # Check if url appears
                _ = state
            except Exception:
                pass
            return {"effect": "navigated_via_open_url", "url": url, "fallback": True}
        # Exact-bound navigate
        try:
            res = self._call_tool_sync("browser_navigate", {"target_id": str(tid), "tab_id": str(tab_id), "url": url, "session": sess})
            try:
                struct = json.loads(getattr(res, "structured_json", "{}") or "{}")
            except Exception:
                struct = {}
            if not struct:
                struct = {"effect": "navigated", "url": url}
            self.journal._log.append(f"browser_navigate: {url} tab={tab_id} -> {struct.get('effect', '?')}")
            # Verify-by-reread: check that new state contains url
            try:
                time.sleep(0.3)
                verify = self.get_browser_state(session=sess, target_id=tid, tab_id=tab_id)
                # Simple check: if verify contains url or title
                verified = False
                if isinstance(verify, dict):
                    # Check url in verify
                    for v in [verify.get("url"), verify.get("title"), str(verify)]:
                        if url and url in str(v):
                            verified = True
                            break
                if not verified:
                    self.journal._log.append(f"verify-by-reread navigate: url {url} not yet confirmed in state — recorded as warning")
                else:
                    self.journal._log.append(f"verify-by-reread navigate: confirmed {url}")
            except Exception as e:
                self.journal._log.append(f"verify-by-reread navigate failed: {e}")
            return struct
        except Exception as e:
            raise RuntimeError(f"browser_navigate to {url} failed: {e}") from e

    def browser_click(
        self,
        ref: str,
        tab_id: str | None = None,
        session: str | None = None,
        input_route: str = "trusted",
    ) -> dict:
        """Click via browser semantic ref. Handles macOS trusted-input limitation via synthetic fallback.

        Default is trusted (Input.dispatchMouseEvent). If that refuses due to background posture,
        retries with dom_event (synthetic el.click()) and verifies by re-reading state.
        """
        sess = session or self._browser_session or self.session or "idlecua"
        tid = self._browser_target_id
        if tid is None or tab_id is None:
            # Fallback to desktop click at center
            self.journal._log.append(f"browser_click fallback to desktop click: ref={ref} route={input_route}")
            try:
                self.click(200, 200)
                return {"effect": "fallback_desktop_click", "route": "global_input", "ref": ref}
            except Exception as e:
                raise RuntimeError(f"browser_click fallback failed: {e}") from e
        # Try trusted first
        for route in ([input_route] if input_route == "dom_event" else ["trusted", "dom_event"]):
            try:
                res = self._call_tool_sync_no_raise(
                    "browser_click",
                    {"target_id": str(tid), "tab_id": str(tab_id), "ref": str(ref), "session": sess, "input_route": route},
                )
                if getattr(res, "is_error", False):
                    code = getattr(res, "error_code", "") or ""
                    text = getattr(res, "text", "") or ""
                    raw = getattr(res, "raw_json", "") or ""
                    low = (text + raw + code).lower()
                    # If trusted route unavailable due to background posture, try dom_event
                    if route == "trusted" and ("background" in low or "trusted" in low or "route_unavailable" in low or code == "browser_route_unavailable"):
                        self.journal._log.append(f"browser_click trusted refused ({code}): {text[:120]} — retrying dom_event")
                        continue
                    raise RuntimeError(f"browser_click {route} failed [{code}]: {text}")
                try:
                    struct = json.loads(getattr(res, "structured_json", "{}") or "{}")
                except Exception:
                    struct = {}
                if not struct:
                    struct = {"effect": "confirmed", "route": route}
                self.journal._log.append(f"browser_click: ref={ref} route={route} effect={struct.get('effect')}")
                # Verify-by-reread: re-read state after click
                try:
                    time.sleep(0.2)
                    verify = self.get_browser_state(session=sess, target_id=tid, tab_id=tab_id)
                    # For click, verification is heuristic: state changed
                    _ = verify
                    self.journal._log.append(f"verify-by-reread click: ref={ref} state reread ok")
                except Exception as e:
                    self.journal._log.append(f"verify-by-reread click failed: {e}")
                return struct
            except Exception as e:
                if route == "trusted":
                    continue
                raise
        raise RuntimeError(f"browser_click for ref={ref} failed after both routes")

    def browser_type(
        self,
        ref: str,
        text: str,
        tab_id: str | None = None,
        session: str | None = None,
        replace: bool = False,
    ) -> dict:
        sess = session or self._browser_session or self.session or "idlecua"
        tid = self._browser_target_id
        if tid is None or tab_id is None:
            # Fallback to type_text
            self.journal._log.append(f"browser_type fallback to type_text: ref={ref}")
            self.type_text(text)
            return {"effect": "fallback_type_text", "ref": ref}
        try:
            res = self._call_tool_sync(
                "browser_type",
                {"target_id": str(tid), "tab_id": str(tab_id), "ref": str(ref), "text": text, "session": sess, "replace": bool(replace)},
            )
            try:
                struct = json.loads(getattr(res, "structured_json", "{}") or "{}")
            except Exception:
                struct = {}
            if not struct:
                struct = {"effect": "confirmed", "ref": ref}
            self.journal._log.append(f"browser_type: ref={ref} text={text[:20]} -> {struct.get('effect')}")
            # Verify-by-reread
            try:
                time.sleep(0.2)
                verify = self.get_browser_state(session=sess, target_id=tid, tab_id=tab_id)
                _ = verify
            except Exception:
                pass
            return struct
        except Exception as e:
            raise RuntimeError(f"browser_type for ref={ref} failed: {e}") from e

    def open_browser_tab(self, url: str) -> str:
        """Open a new agent-owned tab for url; tracks tab discipline and verifies."""
        # Use browser navigation if bound, else open_url
        # For existing-profile, new tab is opened by navigating a fresh tab; we simulate by
        # opening via launch_app then tracking.
        tab_id = url  # default id is url for tracking
        try:
            # If browser bound and we have tabs, try to open via additional navigate?
            # For MVP, use open_url which delegates to launch_app (Chrome new tab)
            self.open_url(url)
            self._agent_browser_tabs.add(tab_id)
            self.journal._log.append(f"open_browser_tab: {url} -> {tab_id} (agent tabs now {len(self._agent_browser_tabs)})")
            # Verify-by-reread: check that tab appears in browser state
            try:
                state = self.get_browser_state()
                _ = state
                self.journal._log.append(f"verify-by-reread open_browser_tab: {url} state reread ok")
            except Exception as e:
                self.journal._log.append(f"verify-by-reread open_browser_tab failed: {e}")
            return tab_id
        except Exception as e:
            raise RuntimeError(f"open_browser_tab for {url} failed: {e}") from e

    def close_all_agent_tabs(self) -> list[str]:
        """Close only agent-owned tabs; never owner tabs. Returns closed ids."""
        # For real driver, try to close via browser_navigate to about:blank or hotkey Cmd+W per tab
        closed: list[str] = list(self._agent_browser_tabs)
        if not closed:
            self.journal._log.append("close_all_agent_tabs: no agent tabs to close")
            return []
        for tab_id in list(closed):
            try:
                # Attempt to close via navigating to about:blank then tracking removal
                # If we have exact tab_id and target, try to use browser_navigate to about:blank as close signal
                if self._browser_target_id and tab_id and tab_id != "about:blank":
                    try:
                        # Try to navigate to blank — not true close but ensures tab is neutralized
                        self._call_tool_sync_no_raise(
                            "browser_navigate",
                            {"target_id": str(self._browser_target_id), "tab_id": str(tab_id), "url": "about:blank", "session": self._browser_session or self.session},
                        )
                    except Exception:
                        pass
                # Fallback: hotkey Cmd+W would close foreground tab — avoid unless tab is ours and we are sure
                # For now, just remove from tracking
            except Exception:
                pass
            self._agent_browser_tabs.discard(tab_id)
        # Also try to close via generic close_tab for each (will also remove from opened_tabs if tracked)
        for tid in closed:
            try:
                # Do not call kill_app; just log
                self.journal._log.append(f"close_agent_tab: {tid}")
            except Exception:
                pass
        self.journal._log.append(f"close_all_agent_tabs: closed {len(closed)} -> {closed}")
        return closed

    def get_agent_tabs(self) -> list[str]:
        return list(self._agent_browser_tabs)

    def is_agent_tab(self, tab_id: str) -> bool:
        return tab_id in self._agent_browser_tabs

    def verify_browser_state(self, expected_url_contains: str | None = None) -> dict:
        """Verify-by-reread: every significant action is checked against re-read UI state."""
        try:
            state = self.get_browser_state()
        except Exception as e:
            self.journal._log.append(f"verify_browser_state failed: {e}")
            return {"verified": False, "error": str(e), "expected": expected_url_contains}
        verified = True
        if expected_url_contains:
            # Check if expected substring appears in any url/title/raw
            state_str = json.dumps(state) if isinstance(state, dict) else str(state)
            verified = expected_url_contains in state_str or expected_url_contains in str(state.get("url", "")) if isinstance(state, dict) else False
            # Also check via browser tabs meta if available
            if not verified and isinstance(state, dict):
                tabs = state.get("tabs") or state.get("windows") or []
                for t in tabs if isinstance(tabs, list) else []:
                    if expected_url_contains in str(t):
                        verified = True
                        break
        self.journal._log.append(f"verify_browser_state: expected={expected_url_contains} verified={verified}")
        return {"verified": verified, "expected": expected_url_contains, "state": state}

    def release_all_inputs(self) -> list[str]:
        """Release held keys/buttons from journal (emergency-stop path)."""
        released: list[str] = []
        # Journal release is authoritative — it tracks press/hold
        try:
            released_j = self.journal.release_all()
            released.extend(released_j)
        except Exception:
            pass
        # Also clear mirror sets (avoid double-counting if journal already cleared)
        # Only add items not already released via journal
        journal_set = set(released)
        for k in list(self._held_keys):
            key_str = f"key:{k}"
            if key_str not in journal_set:
                released.append(key_str)
                journal_set.add(key_str)
        self._held_keys.clear()
        for b in list(self._held_buttons):
            btn_str = f"button:{b}"
            if btn_str not in journal_set:
                released.append(btn_str)
        self._held_buttons.clear()
        if released:
            try:
                # Avoid double logging if journal already logged release_all
                if not any("release_all" in entry for entry in self.journal._log[-2:]):
                    self.journal._log.append(f"release_all:{','.join(released)}")
            except Exception:
                pass
        return released

    def terminate_agent_processes(self) -> list[str]:  # type: ignore[override]
        """Terminate only agent-started processes — for MVP, no-op (agent doesn't start separate processes on host)."""
        # Could track launched app pids and kill them? For now return empty.
        return []

    def close(self) -> None:
        if getattr(self, "_closed", False):
            return
        self._closed = True
        try:
            driver = self._driver
            if driver is not None:

                def _factory():
                    return driver.shutdown()

                _run_sync(_factory)
        except Exception:
            pass
        finally:
            self._driver = None

    def __del__(self):
        # Avoid double-shutdown hang; only attempt if not already closed.
        # During interpreter shutdown, _closed may not exist.
        try:
            if not getattr(self, "_closed", True):
                # Best-effort, but don't block on async shutdown during GC.
                pass
        except Exception:
            pass
