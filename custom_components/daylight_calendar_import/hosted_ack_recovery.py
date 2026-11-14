"""Dormant bounded recovery of already durable hosted ACK obligations.

The caller holds entry-scoped exclusive lifecycle ownership for the entire pass.
This module does not schedule work, retry automatically, or acquire new leases.
"""
from __future__ import annotations

from .hosted_ack_dispatch import async_dispatch_ack


async def async_recover_pending_acks(
    *, store, credentials, transport, local_config_entry_id: str, limit: int = 20,
):
    """Attempt each selected durable pending ACK once and stop on any failure.

    A stale lease may be rejected by the server. Its journal stays pending for
    later evidence-checked re-lease; this pass must not reparse the source.
    """
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("ACK recovery limit must be 1 to 50")
    pending = [entry for entry in store.hosted_ack_journal()
               if entry.ack_status == "pending"
               and entry.evidence.local_config_entry_id == local_config_entry_id]
    results = []
    for entry in pending[:limit]:
        results.append(await async_dispatch_ack(
            store=store, credentials=credentials, transport=transport, entry=entry,
            local_config_entry_id=local_config_entry_id,
        ))
    return results
