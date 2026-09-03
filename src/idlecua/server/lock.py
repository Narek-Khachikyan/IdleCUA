from __future__ import annotations

import json
import os
import time
from pathlib import Path
from dataclasses import dataclass


LOCK_FILENAME = ".scheduler.lock"


@dataclass
class LockInfo:
    pid: int
    started_at: str
    data_dir: str


def _lock_path(data_dir: Path | str) -> Path:
    return Path(data_dir).expanduser() / LOCK_FILENAME


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it
        return True
    except OSError:
        return False


def get_lock_info(data_dir: Path | str) -> dict | None:
    p = _lock_path(data_dir)
    if not p.exists():
        return None
    try:
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data
    except Exception:
        return None


def is_locked(data_dir: Path | str) -> bool:
    info = get_lock_info(data_dir)
    if info is None:
        return False
    pid = info.get("pid")
    if pid is None:
        return False
    try:
        pid_int = int(pid)
    except Exception:
        return False
    return _is_pid_alive(pid_int)


def acquire_lock(data_dir: Path | str, pid: int | None = None) -> dict:
    """Attempt to acquire scheduler lock for data_dir.

    Returns lock info dict on success.

    Raises RuntimeError with message "already running in process X" if another
    alive process holds the lock (mutual exclusion).

    Handles stale locks via PID liveness: if lock file exists but PID is dead,
    the stale lock is removed and acquisition proceeds.

    This is crash-safe: a killed serve process never wedges the data dir.
    """
    data_dir = Path(data_dir).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    p = _lock_path(data_dir)
    my_pid = int(pid) if pid is not None else os.getpid()
    now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    # Check existing lock
    if p.exists():
        existing = get_lock_info(data_dir)
        if existing is not None:
            existing_pid = existing.get("pid")
            try:
                existing_pid_int = int(existing_pid) if existing_pid is not None else None
            except Exception:
                existing_pid_int = None
            if existing_pid_int is not None and _is_pid_alive(existing_pid_int):
                raise RuntimeError(f"already running in process {existing_pid_int}")
            # stale — remove
            try:
                p.unlink()
            except Exception:
                pass
        else:
            # unreadable lock file — treat as stale and remove
            try:
                p.unlink()
            except Exception:
                pass

    # Try to create lock file atomically (avoid race)
    # Use O_EXCL via open with 'x'
    payload = {"pid": my_pid, "started_at": now, "data_dir": str(data_dir)}
    try:
        # Use exclusive creation to fail if another process raced
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, (json.dumps(payload, indent=2) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except FileExistsError:
        # Race: another process created lock between our check and creation
        # Re-check liveness
        existing2 = get_lock_info(data_dir)
        if existing2 is not None:
            ep = existing2.get("pid")
            try:
                epi = int(ep) if ep is not None else None
            except Exception:
                epi = None
            if epi is not None and _is_pid_alive(epi):
                raise RuntimeError(f"already running in process {epi}")
            # stale race — remove and retry once
            try:
                p.unlink()
            except Exception:
                pass
            # retry once
            try:
                fd2 = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    os.write(fd2, (json.dumps(payload, indent=2) + "\n").encode("utf-8"))
                finally:
                    os.close(fd2)
            except FileExistsError:
                raise RuntimeError("already running in process unknown (race)")
        else:
            raise RuntimeError("already running in process unknown")
    return payload


def release_lock(data_dir: Path | str, pid: int | None = None) -> None:
    """Release lock if held by current pid (or given pid)."""
    data_dir = Path(data_dir).expanduser()
    p = _lock_path(data_dir)
    if not p.exists():
        return
    info = get_lock_info(data_dir)
    if info is None:
        try:
            p.unlink()
        except Exception:
            pass
        return
    my_pid = int(pid) if pid is not None else os.getpid()
    try:
        existing_pid = int(info.get("pid", -1))
    except Exception:
        existing_pid = -1
    # Only remove if we own it, or if pid is dead (stale)
    if existing_pid == my_pid or not _is_pid_alive(existing_pid):
        try:
            p.unlink()
        except Exception:
            pass
