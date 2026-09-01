from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

SERVICE = "idlecua"
# For env-based fallback during automation (never committed), allow override:
_ENV_KEY_PREFIX = "IDLECUA_API_KEY_"

# -- Abstraction --


class CredentialStore(ABC):
    @abstractmethod
    def set(self, provider_name: str, api_key: str) -> None: ...

    @abstractmethod
    def get(self, provider_name: str) -> Optional[str]: ...

    @abstractmethod
    def delete(self, provider_name: str) -> None: ...


def _key(provider_name: str) -> str:
    return f"provider:{provider_name}"


# -- System keychain via ``keyring`` (macOS Keychain on darwin) --


class SystemCredentialStore(CredentialStore):
    """System credential store — macOS Keychain via ``keyring``.

    Falls back to a locked file under ``data_dir`` only when ``keyring``
    is unavailable (CI/Linux without a backend). That fallback is still
    never written to ``providers.json`` or logs.
    """

    def __init__(self, data_dir: Optional[Path] = None) -> None:
        self.data_dir = Path(data_dir).expanduser() if data_dir is not None else None
        self._has_keyring: bool = False
        self._keyring = None
        try:
            import keyring  # type: ignore

            # probe backend — on macOS this hits Keychain; on headless Linux it may be null
            # keyring.get_keyring() will always return something; we test operability
            self._keyring = keyring
            self._has_keyring = True
            # Quick backend check: try to see if it is fail backend
            try:
                from keyring.backends.fail import Keyring as FailKeyring  # type: ignore

                if isinstance(keyring.get_keyring(), FailKeyring):
                    self._has_keyring = False
            except Exception:
                pass
        except Exception:
            self._has_keyring = False
            self._keyring = None

    # -- file fallback helpers --

    @property
    def _fallback_path(self) -> Optional[Path]:
        if self.data_dir is None:
            return None
        return self.data_dir / ".credentials.json"

    def _fallback_read(self) -> dict:
        p = self._fallback_path
        if p is None or not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _fallback_write(self, data: dict) -> None:
        p = self._fallback_path
        if p is None:
            raise RuntimeError("No data_dir for fallback credential storage")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        try:
            p.chmod(0o600)
        except Exception:
            pass

    # -- CredentialStore impl --

    def set(self, provider_name: str, api_key: str) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("api_key must be non-empty")
        # Prefer system keychain
        if self._has_keyring and self._keyring is not None:
            try:
                self._keyring.set_password(SERVICE, _key(provider_name), api_key)
                return
            except Exception:
                # Fall through to file fallback on keyring failure
                pass
        # File fallback (CI / no keyring backend)
        data = self._fallback_read()
        data[_key(provider_name)] = api_key
        self._fallback_write(data)

    def get(self, provider_name: str) -> Optional[str]:
        # Check env override first for tests (e.g., IDLECUA_API_KEY_OPENROUTER)
        env_name = _ENV_KEY_PREFIX + provider_name.upper().replace("-", "_")
        env_val = os.environ.get(env_name)
        if env_val:
            return env_val
        if self._has_keyring and self._keyring is not None:
            try:
                val = self._keyring.get_password(SERVICE, _key(provider_name))
                if val is not None:
                    return val
            except Exception:
                pass
        # fallback file
        data = self._fallback_read()
        return data.get(_key(provider_name))

    def delete(self, provider_name: str) -> None:
        if self._has_keyring and self._keyring is not None:
            try:
                # type: ignore[no-untyped-call]
                self._keyring.delete_password(SERVICE, _key(provider_name))
            except Exception:
                pass
        # also clear fallback
        data = self._fallback_read()
        if _key(provider_name) in data:
            del data[_key(provider_name)]
            self._fallback_write(data)


class MemoryCredentialStore(CredentialStore):
    """In-memory store — for tests / isolated temp dirs."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def set(self, provider_name: str, api_key: str) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("api_key must be non-empty")
        self._data[_key(provider_name)] = api_key

    def get(self, provider_name: str) -> Optional[str]:
        return self._data.get(_key(provider_name))

    def delete(self, provider_name: str) -> None:
        self._data.pop(_key(provider_name), None)


# -- Module-level convenience (real usage) --

_default_store: Optional[SystemCredentialStore] = None


def get_default_store(data_dir: Optional[Path | str] = None) -> SystemCredentialStore:
    global _default_store
    # Always create a store tied to data_dir so fallback path is correct.
    # The global cache is only for callers that pass no data_dir.
    if data_dir is None:
        if _default_store is None:
            _default_store = SystemCredentialStore()
        return _default_store
    return SystemCredentialStore(data_dir=Path(data_dir))


def set_api_key(provider_name: str, api_key: str, data_dir: Optional[Path | str] = None) -> None:
    get_default_store(data_dir).set(provider_name, api_key)


def get_api_key(provider_name: str, data_dir: Optional[Path | str] = None) -> Optional[str]:
    return get_default_store(data_dir).get(provider_name)


def delete_api_key(provider_name: str, data_dir: Optional[Path | str] = None) -> None:
    get_default_store(data_dir).delete(provider_name)
