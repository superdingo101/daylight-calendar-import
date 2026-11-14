"""Dormant one-shot hosted text claim and processing batch.

The caller holds the entry's exclusive lifecycle operation for the entire call.
There is no scheduler, retry, or runtime wiring in this module.
"""
from __future__ import annotations

from datetime import UTC, datetime

from .hosted_ack_dispatch import AckDispatchBlocked
from .hosted_text_claim import async_handle_text_claim


async def async_process_text_batch(
    *, store, credentials, claim_transport, ack_transport, local_config_entry_id,
    route_source, parse_source, limit=20,
):
    """Claim once and process sequentially; failures leave later leases to expire.

    A lost claim response is ambiguous and is never retried here. Caller must
    retain exclusive lifecycle ownership until this coroutine has completed.
    """
    checkpoint = credentials.snapshot
    credential = checkpoint.current
    binding = checkpoint.service_binding
    if (credential is None or binding is None or not checkpoint.processing_consent
            or checkpoint.local_disconnect != "none" or checkpoint.reconnect is not None
            or checkpoint.candidate is not None or checkpoint.rotation_not_before is not None
            or claim_transport.service_binding != binding or ack_transport.service_binding != binding
            or datetime.fromisoformat(credential.expires_at) <= datetime.now(UTC)):
        raise AckDispatchBlocked("hosted claim authority unavailable")
    page = await claim_transport.async_claim(token=credential.token, limit=limit)
    if credentials.snapshot != checkpoint:
        raise AckDispatchBlocked("hosted claim authority changed")
    results = []
    for delivery in page["deliveries"]:
        if (credentials.snapshot != checkpoint
                or datetime.fromisoformat(credential.expires_at) <= datetime.now(UTC)):
            raise AckDispatchBlocked("hosted claim authority unavailable")
        results.append(await async_handle_text_claim(
            store=store, credentials=credentials, claim_checkpoint=checkpoint,
            service_binding=binding, transport=ack_transport,
            local_config_entry_id=local_config_entry_id, delivery=delivery,
            route_source=route_source, parse_source=parse_source,
        ))
    return results
