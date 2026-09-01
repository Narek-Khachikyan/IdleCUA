from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

_PROVIDERS_FILENAME = "providers.json"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class ProviderConfig:
    """Non-secret provider configuration.

    Secrets (api_key) are never stored here — they live in the system
    credential store (Keychain) under service ``idlecua``.
    """

    name: str
    base_url: str
    model: str

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("Provider name must be non-empty")
        if not _NAME_RE.match(self.name):
            raise ValueError(
                "Provider name must be alphanumeric with ., _, - (e.g. 'openrouter', 'opencode-go')"
            )
        if not self.base_url or not self.base_url.strip():
            raise ValueError("base_url must be non-empty")
        url = self.base_url.strip()
        if not (url.startswith("https://") or url.startswith("http://")):
            raise ValueError("base_url must start with https:// or http://")
        # Normalize: no trailing slash
        object.__setattr__(self, "base_url", url.rstrip("/"))
        if not self.model or not self.model.strip():
            raise ValueError("model must be non-empty")
        object.__setattr__(self, "model", self.model.strip())
        object.__setattr__(self, "name", self.name.strip())

    def to_dict(self) -> dict:
        return {"name": self.name, "base_url": self.base_url, "model": self.model}

    @classmethod
    def from_dict(cls, data: dict) -> "ProviderConfig":
        return cls(
            name=str(data["name"]),
            base_url=str(data["base_url"]),
            model=str(data["model"]),
        )


@dataclass
class ProviderStore:
    """Persistent store for non-secret provider configs.

    Backed by ``{data_dir}/providers.json``. API keys are kept in the
    system credential store and never touch this file.
    """

    data_dir: Path
    providers: Dict[str, ProviderConfig]
    selected: Optional[str] = None

    @property
    def path(self) -> Path:
        return self.data_dir / _PROVIDERS_FILENAME

    @classmethod
    def load(cls, data_dir: Path | str) -> "ProviderStore":
        base = Path(data_dir).expanduser()
        p = base / _PROVIDERS_FILENAME
        if not p.exists():
            return cls(data_dir=base, providers={}, selected=None)
        raw = json.loads(p.read_text(encoding="utf-8"))
        providers_raw = raw.get("providers", {})
        providers: Dict[str, ProviderConfig] = {}
        for name, cfg in providers_raw.items():
            try:
                providers[name] = ProviderConfig.from_dict(cfg)
            except Exception:
                # Skip malformed entries rather than crashing load
                continue
        selected = raw.get("selected")
        if selected is not None and selected not in providers:
            selected = None
        return cls(data_dir=base, providers=providers, selected=selected)

    def save(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "providers": {name: cfg.to_dict() for name, cfg in self.providers.items()},
            "selected": self.selected,
        }
        # Ensure file is not world-readable (best effort)
        p = self.path
        p.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        try:
            p.chmod(0o600)
        except Exception:
            pass
        return p

    # -- mutations --

    def add(self, config: ProviderConfig) -> None:
        self.providers[config.name] = config
        # Auto-select first provider if none selected
        if self.selected is None:
            self.selected = config.name
        self.save()

    def remove(self, name: str) -> None:
        if name not in self.providers:
            raise KeyError(f"Provider '{name}' not found")
        del self.providers[name]
        if self.selected == name:
            # Select first remaining or None
            self.selected = next(iter(self.providers), None)
        self.save()

    def select(self, name: str) -> None:
        if name not in self.providers:
            raise KeyError(f"Provider '{name}' not found")
        self.selected = name
        self.save()

    def get(self, name: str) -> ProviderConfig:
        if name not in self.providers:
            raise KeyError(f"Provider '{name}' not found")
        return self.providers[name]

    def get_selected(self) -> Optional[ProviderConfig]:
        if self.selected is None:
            return None
        return self.providers.get(self.selected)

    def list(self) -> List[ProviderConfig]:
        return list(self.providers.values())

    def is_selected(self, name: str) -> bool:
        return self.selected == name


# Pre-documented provider presets (for help text / docs; not auto-inserted)
PRESET_OPENROUTER = ProviderConfig(
    name="openrouter",
    base_url="https://openrouter.ai/api/v1",
    model="anthropic/claude-3.5-sonnet",
)

PRESET_OPENCODE_GO = ProviderConfig(
    name="opencode-go",
    base_url="https://opencode.ai/zen/go/v1",
    model="anthropic/claude-3.5-sonnet",
)
