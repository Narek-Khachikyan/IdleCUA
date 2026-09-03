"""Server package — Local UI + HTTP API for idle-cua serve."""

from .lock import acquire_lock, release_lock, get_lock_info, is_locked

__all__ = ["acquire_lock", "release_lock", "get_lock_info", "is_locked"]
