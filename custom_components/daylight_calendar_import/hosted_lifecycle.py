"""P02 candidate reconciliation, dormant until the native settings consumer.

Only confirmation has a bounded protocol-defined retry. Issuance, exchange,
rotation and disconnect are never replayed here. Callers own scheduling and UI.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from .hosted_credentials import BoundCredential, HostedCredentialStore
from .hosted_transport import HostedLifecycleTransport, HostedTransportError


@dataclass(frozen=True, slots=True)
class HostedConnectionResult:
    """Safe outcome without credentials, approval codes or account data."""

    state: str
    credential_status: str | None = None


class HostedLifecycleController:
    """Reconcile securely saved credentials; failures never establish authority.

    One controller per loaded store. Storage/transient failures preserve bundles.
    Replacement is blocked until a durable journal reconciliation consumer exists.
    """

    def __init__(self, store: HostedCredentialStore, transport: HostedLifecycleTransport) -> None:
        self._store = store
        self._transport = transport
        self._lock = asyncio.Lock()

    @staticmethod
    def _binding(status: dict[str, Any], credential: Any) -> BoundCredential:
        bound = BoundCredential(
            status['account_id'], status['installation_id'], credential.token,
            status['credential_expires_at'],
        )
        if (bound.installation_id != credential.installation_id
                or datetime.fromisoformat(bound.expires_at) > datetime.fromisoformat(credential.expires_at)
                or (isinstance(credential, BoundCredential) and bound.namespace != credential.namespace)):
            raise HostedTransportError('identity_mismatch')
        return bound

    async def async_reconcile(self) -> HostedConnectionResult:
        """Recover rotation/first connection after restart, without mutation replay."""
        async with self._lock:
            checkpoint = self._store.snapshot
            if not checkpoint.processing_consent:
                return HostedConnectionResult('disconnected')
            candidate = checkpoint.candidate
            if candidate is not None:
                if (checkpoint.current is not None
                        and candidate.installation_id != checkpoint.current.installation_id):
                    return HostedConnectionResult('reconciliation_required')
                try:
                    status = await self._transport.installation_status(candidate.token)
                except HostedTransportError as error:
                    if error.status not in (401, 403):
                        raise
                    try:
                        await self._confirm(candidate)
                    except HostedTransportError as confirmation_error:
                        if confirmation_error.status not in (401, 403):
                            raise
                        return await self._previous()
                    # Confirm has no household binding: status must establish it.
                    status = await self._transport.installation_status(candidate.token)
                bound = self._binding(status, candidate)
                if checkpoint.current is not None and bound.namespace != checkpoint.current.namespace:
                    raise HostedTransportError('identity_mismatch')
                if status['credential_status'] != 'current':
                    return HostedConnectionResult('repair_required', 'overlap')
                await self._store.async_checkpoint(checkpoint, replace(
                    checkpoint, current=bound, candidate=None, pairing_verifier=None,
                ))
                return HostedConnectionResult('connected', 'current')
            return await self._previous()

    async def _confirm(self, candidate: Any) -> None:
        confirmed = await self._transport.confirm(candidate.token)
        if (confirmed['installation_id'] != candidate.installation_id
                or datetime.fromisoformat(confirmed['expires_at'].upper())
                != datetime.fromisoformat(candidate.expires_at)):
            raise HostedTransportError('identity_mismatch')

    async def _previous(self) -> HostedConnectionResult:
        checkpoint = self._store.snapshot
        previous = checkpoint.current
        if previous is None:
            return HostedConnectionResult('repair_required')
        try:
            status = await self._transport.installation_status(previous.token)
        except HostedTransportError as error:
            if error.status in (401, 403):
                return HostedConnectionResult('repair_required')
            raise
        self._binding(status, previous)
        return HostedConnectionResult(
            'previous_retained' if checkpoint.candidate is not None else 'connected',
            status['credential_status'],
        )
