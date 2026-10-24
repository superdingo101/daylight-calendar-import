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
    credential_expires_at: str | None = None


class HostedLifecycleController:
    """Reconcile securely saved credentials; failures never establish authority.

    One controller per loaded store. Storage/transient failures preserve bundles.
    Replacement is blocked until a durable journal reconciliation consumer exists.
    """

    def __init__(
        self, store: HostedCredentialStore, transport: HostedLifecycleTransport
    ) -> None:
        self._store = store
        self._transport = transport
        self._lock = asyncio.Lock()

    @staticmethod
    def _binding(status: dict[str, Any], credential: Any) -> BoundCredential:
        bound = BoundCredential(
            status["account_id"],
            status["installation_id"],
            credential.token,
            status["credential_expires_at"],
        )
        if (
            bound.installation_id != credential.installation_id
            or datetime.fromisoformat(bound.expires_at)
            > datetime.fromisoformat(credential.expires_at)
            or (
                isinstance(credential, BoundCredential)
                and bound.namespace != credential.namespace
            )
        ):
            raise HostedTransportError("identity_mismatch")
        return bound

    @staticmethod
    def _unauthorized(error: HostedTransportError) -> bool:
        return (
            error.status == 401
            and error.code == "unauthenticated"
            or error.status == 403
            and error.code == "forbidden"
        )

    async def async_reconcile(self) -> HostedConnectionResult:
        """Recover rotation/first connection after restart, without mutation replay."""
        async with self._lock:
            checkpoint = self._store.snapshot
            if checkpoint.local_disconnect != "none":
                return self._disconnected(checkpoint.local_disconnect)
            if not checkpoint.processing_consent:
                return HostedConnectionResult("disconnected")
            candidate = checkpoint.candidate
            if candidate is not None:
                if (
                    checkpoint.current is not None
                    and candidate.installation_id != checkpoint.current.installation_id
                ):
                    return HostedConnectionResult("reconciliation_required")
                try:
                    status = await self._transport.installation_status(candidate.token)
                except HostedTransportError as error:
                    if not self._unauthorized(error):
                        raise
                    try:
                        await self._confirm(candidate)
                    except HostedTransportError as confirmation_error:
                        if not self._unauthorized(confirmation_error):
                            raise
                        return await self._previous()
                    # Confirm has no household binding: status must establish it.
                    status = await self._transport.installation_status(candidate.token)
                bound = self._binding(status, candidate)
                if (
                    checkpoint.current is not None
                    and bound.namespace != checkpoint.current.namespace
                ):
                    raise HostedTransportError("identity_mismatch")
                await self._store.async_checkpoint(
                    checkpoint,
                    replace(
                        checkpoint,
                        current=bound,
                        candidate=None,
                        pairing_verifier=None,
                    ),
                )
                # An overlap token still authorizes ordinary work until its
                # effective deadline; lifecycle repair is a separate concern.
                return HostedConnectionResult(
                    "connected", status["credential_status"], bound.expires_at
                )
            return await self._previous()

    @staticmethod
    def _disconnected(state: str) -> HostedConnectionResult:
        return HostedConnectionResult(
            "disconnected" if state == "confirmed" else "remote_disconnect_unconfirmed"
        )

    async def async_disconnect(self) -> HostedConnectionResult:
        """Persist the local stop before one bearer-authorized disconnect attempt.

        An uncertain attempt is never replayed, including after restart. The
        owner must reconcile it through the account portal before reconnecting.
        Imports, review data and calendar events are not touched here.
        """
        async with self._lock:
            checkpoint = self._store.snapshot
            if checkpoint.local_disconnect != "none":
                return self._disconnected(checkpoint.local_disconnect)
            stopped = await self._store.async_checkpoint(
                checkpoint,
                replace(checkpoint, local_disconnect="unconfirmed"),
            )
            if stopped.current is None:
                return self._disconnected("unconfirmed")
            try:
                status = await self._transport.installation_status(
                    stopped.current.token
                )
                self._binding(status, stopped.current)
                if status["credential_status"] != "current":
                    return self._disconnected("unconfirmed")
                result = await self._transport.disconnect(stopped.current.token)
            except HostedTransportError as error:
                if self._unauthorized(error):
                    return self._disconnected("unconfirmed")
                raise
            if result["installation_id"] != stopped.current.installation_id:
                raise HostedTransportError("identity_mismatch")
            await self._store.async_checkpoint(
                stopped,
                replace(
                    stopped,
                    processing_consent=False,
                    current=None,
                    candidate=None,
                    pairing_verifier=None,
                    local_disconnect="confirmed",
                ),
            )
            return self._disconnected("confirmed")

    async def _confirm(self, candidate: Any) -> None:
        confirmed = await self._transport.confirm(candidate.token)
        if confirmed[
            "installation_id"
        ] != candidate.installation_id or datetime.fromisoformat(
            confirmed["expires_at"].upper()
        ) != datetime.fromisoformat(candidate.expires_at):
            raise HostedTransportError("identity_mismatch")

    async def _previous(self) -> HostedConnectionResult:
        checkpoint = self._store.snapshot
        previous = checkpoint.current
        if previous is None:
            return HostedConnectionResult("repair_required")
        try:
            status = await self._transport.installation_status(previous.token)
        except HostedTransportError as error:
            if self._unauthorized(error):
                return HostedConnectionResult("repair_required")
            raise
        bound = self._binding(status, previous)
        return HostedConnectionResult(
            "previous_retained" if checkpoint.candidate is not None else "connected",
            status["credential_status"],
            bound.expires_at,
        )
