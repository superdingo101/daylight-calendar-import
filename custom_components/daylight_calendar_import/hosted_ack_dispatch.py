"""Dormant one-shot ACK reconciliation for a runtime-owned operation.

The caller must hold the entry's exclusive lifecycle-operation ownership from
credential snapshot through completion. This module is not scheduled or wired.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from .hosted_credentials import CredentialCheckpoint
from .hosted_delivery_journal import HostedDeliveryJournalEntry
from .hosted_transport import HostedLifecycleTransport
from .storage import PendingImportStore


class AckDispatchBlocked(ValueError):
    """The saved delivery cannot be acknowledged under this credential."""


class Checkpoints(Protocol):
    @property
    def snapshot(self) -> CredentialCheckpoint: ...


async def async_dispatch_ack(
    *, store: PendingImportStore, credentials: Checkpoints,
    transport: HostedLifecycleTransport, entry: HostedDeliveryJournalEntry,
    local_config_entry_id: str,
) -> HostedDeliveryJournalEntry:
    """Persist an attempt, submit exactly once, and commit confirmed response.

    Transport errors and uncertain writes propagate; neither marks confirmation.
    Caller owns serialization with credential changes, and retry/backoff policy.
    """
    checkpoint = credentials.snapshot
    credential = checkpoint.current
    evidence = entry.evidence
    if (credential is None or not checkpoint.processing_consent
            or checkpoint.service_binding is None
            or checkpoint.service_binding != transport.service_binding
            or checkpoint.local_disconnect != "none" or checkpoint.reconnect is not None
            or checkpoint.candidate is not None or checkpoint.rotation_not_before is not None
            or evidence.local_config_entry_id != local_config_entry_id
            or not evidence.matches_namespace(*credential.namespace)):
        raise AckDispatchBlocked("hosted ACK authority unavailable")
    expiry = datetime.fromisoformat(evidence.source_expires_at.replace("Z", "+00:00"))
    now = datetime.now(UTC)
    if now >= expiry:
        return await store.async_reconcile_hosted_ack(entry=entry, action="expire")
    if now >= datetime.fromisoformat(credential.expires_at):
        raise AckDispatchBlocked("hosted ACK authority unavailable")
    attempted = await store.async_reconcile_hosted_ack(entry=entry, action="attempt")
    if credentials.snapshot != checkpoint:
        raise AckDispatchBlocked("hosted ACK authority changed")
    await transport.acknowledge_source(token=credential.token, delivery_id=evidence.delivery_id,
                                       lease_token=evidence.lease_token)
    if credentials.snapshot != checkpoint:
        raise AckDispatchBlocked("hosted ACK authority changed")
    return await store.async_reconcile_hosted_ack(entry=attempted, action="confirm")
