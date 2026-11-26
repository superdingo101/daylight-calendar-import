"""Compatibility boundary for HA's immediate atomic storage writers."""

from typing import Any


def invalidate_store_cache(store: Any) -> None:
    """Invalidate HA's preload cache under the caller's existing write lock.

    HA 2026.11 moved file I/O and cache invalidation from Store to its
    I/O implementation. Use capabilities, not version comparisons. An
    unsupported implementation or failed invalidation must abort the write.
    """
    io = getattr(store, "_io", None)
    if io is not None:
        io.async_invalidate_cache()
    else:
        store._manager.async_invalidate(store.key)
