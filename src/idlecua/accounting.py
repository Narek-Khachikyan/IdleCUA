from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict

_FILENAME = "llm_usage.json"


def _path(data_dir: Path | str) -> Path:
    return Path(data_dir).expanduser() / _FILENAME


def _today_iso() -> str:
    return date.today().isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_llm_call(data_dir: Path | str, model: str | None = None) -> int:
    """Increment the daily LLM call counter and persist it.

    Returns the new count for today. This is the accounting hook that the
    planner ticket will use to enforce ``≤ 150 LLM calls per day``.
    """
    p = _path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    data: Dict[str, int] = {}
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            # Support both flat {date: count} and extended {counts, history}
            if isinstance(raw, dict) and "counts" in raw:
                data = {k: int(v) for k, v in raw.get("counts", {}).items()}
            elif isinstance(raw, dict):
                data = {k: int(v) for k, v in raw.items() if isinstance(v, int)}
        except Exception:
            data = {}
    today = _today_iso()
    data[today] = int(data.get(today, 0)) + 1
    # Prune to last 30 days to bound file size
    if len(data) > 30:
        # keep most recent 30 by sorted ISO date string
        for k in sorted(data)[:-30]:
            del data[k]
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    try:
        p.chmod(0o600)
    except Exception:
        pass
    return data[today]


def get_today_count(data_dir: Path | str) -> int:
    p = _path(data_dir)
    if not p.exists():
        return 0
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "counts" in raw:
            raw = raw["counts"]
        return int(raw.get(_today_iso(), 0))
    except Exception:
        return 0


def get_counts(data_dir: Path | str) -> Dict[str, int]:
    p = _path(data_dir)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "counts" in raw:
            raw = raw["counts"]
        return {k: int(v) for k, v in raw.items() if isinstance(v, int)}
    except Exception:
        return {}


def will_exceed_daily_cap(data_dir: Path | str, cap: int = 150) -> bool:
    return get_today_count(data_dir) >= cap
