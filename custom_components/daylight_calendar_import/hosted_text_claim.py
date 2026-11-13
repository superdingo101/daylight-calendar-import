"""Dormant one-shot handling of new and re-leased text claims.

The caller owns the entry-scoped exclusive lifecycle operation throughout.
Polling, backoff, and retry scheduling are deliberately separate.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

from .hosted_ack_dispatch import AckDispatchBlocked, async_dispatch_ack
from .hosted_source_transport import _date
from .hosted_text_delivery import async_accept_and_ack_text
from .hosted_text_re_lease import async_re_lease_hosted_text


class _LeaseBoundTransport:
    """Recheck claim authority after the durable ACK attempt, before I/O."""

    def __init__(self, transport, credentials, checkpoint, lease_expires_at):
        self._transport = transport
        self._credentials = credentials
        self._checkpoint = checkpoint
        self._lease_expires_at = lease_expires_at
        self.service_binding = transport.service_binding

    async def acknowledge_source(self, **kwargs):
        if (self._credentials.snapshot != self._checkpoint
                or _date(self._lease_expires_at) <= datetime.now(UTC)):
            raise AckDispatchBlocked("hosted ACK authority unavailable")
        return await self._transport.acknowledge_source(**kwargs)


async def async_handle_text_claim(
    *, store, credentials, claim_checkpoint, service_binding, transport,
    local_config_entry_id, delivery, route_source, parse_source,
):
    """Accept a new delivery or rebind proven prior acceptance before ACK.

    An uncertain write or ACK propagates; the caller must resume from the
    persisted journal and never reparse a previously accepted source.
    """
    delivery = deepcopy(delivery)
    if (type(delivery) is not dict or type(delivery.get("delivery_id")) is not str
            or type(delivery.get("lease_expires_at")) is not str
            or transport.service_binding != service_binding
            or credentials.snapshot != claim_checkpoint):
        raise AckDispatchBlocked("hosted ACK authority unavailable")
    bounded_transport = _LeaseBoundTransport(
        transport, credentials, claim_checkpoint, delivery["lease_expires_at"],
    )
    matches = [item for item in store.hosted_ack_journal()
               if item.evidence.local_config_entry_id == local_config_entry_id
               and item.evidence.delivery_id == delivery["delivery_id"]]
    if len(matches) > 1:
        raise AckDispatchBlocked("hosted ACK authority unavailable")
    if not matches:
        return await async_accept_and_ack_text(
            store=store, credentials=credentials, claim_checkpoint=claim_checkpoint,
            service_binding=service_binding, transport=bounded_transport,
            local_config_entry_id=local_config_entry_id, delivery=delivery,
            route_source=route_source, parse_source=parse_source,
        )
    entry = matches[0]
    if entry.ack_status != "pending":
        raise AckDispatchBlocked("hosted ACK authority unavailable")
    updated = await async_re_lease_hosted_text(
        store=store, credentials=credentials, claim_checkpoint=claim_checkpoint,
        service_binding=service_binding, local_config_entry_id=local_config_entry_id,
        entry=entry, delivery=delivery,
    )
    # Lease can expire during the durable journal write. Never ACK it after that.
    if (credentials.snapshot != claim_checkpoint
            or _date(delivery["lease_expires_at"]) <= datetime.now(UTC)):
        raise AckDispatchBlocked("hosted ACK authority unavailable")
    return await async_dispatch_ack(
        store=store, credentials=credentials, transport=bounded_transport, entry=updated,
        local_config_entry_id=local_config_entry_id,
    )
