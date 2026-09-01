from __future__ import annotations

import json
from pathlib import Path

from .models import Profile


def load_profile(path: Path) -> Profile | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return Profile.from_dict(data)


def save_profile(profile: Profile, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    profile.touch()
    path.write_text(json.dumps(profile.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def profile_exists(path: Path) -> bool:
    return path.exists()
