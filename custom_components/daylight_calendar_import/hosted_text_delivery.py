"""Dormant one-shot acceptance and ACK of a new hosted text delivery.

The caller holds the entry-scoped exclusive lifecycle operation throughout.
This operation neither claims nor retries work; a later scheduler owns both.
"""
from __future__ import annotations

from .hosted_ack_dispatch import AckDispatchBlocked, async_dispatch_ack
from .hosted_text_acceptance import async_accept_hosted_text


async def async_accept_and_ack_text(
    *, store, credentials, claim_checkpoint, service_binding, transport,
    local_config_entry_id, delivery, route_source, parse_source,
):
    """Commit an outcome and its ACK obligation before any ACK is sent.

    Failure after acceptance leaves the journal available to the normal ACK
    reconciliation path. Caller must not repeat parsing on an uncertain result.
    """
    if transport.service_binding != service_binding or credentials.snapshot != claim_checkpoint:
        raise AckDispatchBlocked("hosted ACK authority unavailable")
    result = await async_accept_hosted_text(
        store=store, credentials=credentials, claim_checkpoint=claim_checkpoint,
        service_binding=service_binding, local_config_entry_id=local_config_entry_id,
        delivery=delivery, route_source=route_source, parse_source=parse_source,
    )
    matches = [item for item in store.hosted_ack_journal()
               if item.evidence.local_config_entry_id == local_config_entry_id
               and item.evidence.delivery_id == delivery["delivery_id"]
               and item.evidence.public_source_id == delivery["source_id"]
               and item.evidence.lease_token == delivery["lease_token"]
               and item.ack_status == "pending"]
    if len(matches) != 1 or credentials.snapshot != claim_checkpoint:
        raise AckDispatchBlocked("hosted ACK authority unavailable")
    await async_dispatch_ack(
        store=store, credentials=credentials, transport=transport,
        entry=matches[0], local_config_entry_id=local_config_entry_id,
    )
    return result
