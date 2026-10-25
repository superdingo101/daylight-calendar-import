"""P02 candidate reconciliation, dormant until the native settings consumer.

Only confirmation has a bounded protocol-defined retry. Issuance, exchange,
rotation and disconnect are never replayed here. Callers own scheduling and UI.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .hosted_credentials import (
    BoundCredential,
    CandidateCredential,
    HostedCredentialConflict,
    HostedCredentialStore,
    PairingAttempt,
    ReconnectIntent,
)
from .hosted_namespace import NamespaceReconciliationError
from .hosted_transport import (
    HostedLifecycleTransport,
    HostedTransportError,
    new_pairing_verifier,
)


class _NamespaceReconciler(Protocol):
    async def async_receipt(self, prior_namespace: tuple[str, str] | None) -> str:
        """Prove the supported local storage contract, without browser authority."""
        ...


@dataclass(frozen=True, slots=True)
class HostedConnectionResult:
    """Safe outcome without credentials, approval codes or account data."""

    state: str
    credential_status: str | None = None
    credential_expires_at: str | None = None
    account_url: str | None = None


@dataclass(frozen=True, slots=True)
class HostedPairingInstructions:
    """Transient owner display; never contains the private verifier or token."""

    user_code: str = field(repr=False)
    verification_uri: str
    expires_at: str
    poll_interval_seconds: int


class HostedLifecycleController:
    """Reconcile securely saved credentials; failures never establish authority.

    One controller per loaded store. Storage/transient failures preserve bundles.
    Replacement is blocked until a durable journal reconciliation consumer exists.
    """

    def __init__(
        self, store: HostedCredentialStore, transport: HostedLifecycleTransport,
        reconciler: _NamespaceReconciler | None = None,
    ) -> None:
        self._store = store
        self._transport = transport
        self._reconciler = reconciler
        self._lock = asyncio.Lock()

    def _require_service(self, checkpoint: Any) -> None:
        if checkpoint.service_binding != self._transport.service_binding:
            raise HostedTransportError("service_reconciliation_required")

    async def _namespace_receipt(self, namespace: tuple[str, str] | None) -> str | None:
        if self._reconciler is None:
            return None
        try:
            receipt = await self._reconciler.async_receipt(namespace)
        except NamespaceReconciliationError:
            return None
        return receipt if type(receipt) is str and receipt == "pending-v2-local-only" else None

    async def _reconnect_authorized(self, checkpoint: Any) -> bool:
        intent = checkpoint.reconnect
        return (intent is not None and
                await self._namespace_receipt(intent.namespace) == intent.journal_contract)

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
            if checkpoint.local_disconnect != "none" and checkpoint.reconnect is None:
                return self._disconnected(checkpoint.local_disconnect)
            if not checkpoint.processing_consent:
                return HostedConnectionResult("disconnected")
            self._require_service(checkpoint)
            reconnect = checkpoint.reconnect
            if reconnect is not None and not await self._reconnect_authorized(checkpoint):
                return HostedConnectionResult("reconciliation_required")
            candidate = checkpoint.candidate
            if candidate is not None:
                if (
                    checkpoint.current is not None
                    and candidate.installation_id != checkpoint.current.installation_id
                    and reconnect is None
                ):
                    return HostedConnectionResult("reconciliation_required")
                if (reconnect is not None and reconnect.prior_installation_id is not None
                        and candidate.installation_id == reconnect.prior_installation_id):
                    return HostedConnectionResult("reconciliation_required")
                try:
                    status = await self._transport.installation_status(candidate.token)
                except HostedTransportError as error:
                    if not self._unauthorized(error):
                        raise
                    try:
                        if not await self._confirm(candidate):
                            return (HostedConnectionResult("reconciliation_required")
                                    if reconnect is not None else await self._previous())
                        checkpoint = self._store.snapshot
                        candidate = checkpoint.candidate
                    except HostedTransportError as confirmation_error:
                        if not self._unauthorized(confirmation_error):
                            raise
                        return (HostedConnectionResult("reconciliation_required")
                                if reconnect is not None else await self._previous())
                    # Confirm has no household binding: status must establish it.
                    status = await self._transport.installation_status(candidate.token)
                bound = self._binding(status, candidate)
                if reconnect is not None:
                    if status["credential_status"] != "current":
                        return HostedConnectionResult("reconciliation_required")
                    if (reconnect.prior_account_id is not None
                            and bound.account_id != reconnect.prior_account_id):
                        return HostedConnectionResult("reconciliation_required")
                    if not await self._reconnect_authorized(checkpoint):
                        return HostedConnectionResult("reconciliation_required")
                elif (checkpoint.current is not None
                      and bound.namespace != checkpoint.current.namespace):
                    raise HostedTransportError("identity_mismatch")
                await self._store.async_checkpoint(
                    checkpoint,
                    replace(
                        checkpoint,
                        current=bound,
                        candidate=None,
                        pairing_verifier=None,
                        rotation_not_before=None,
                        pairing=None,
                        local_disconnect="none",
                        reconnect=None,
                        automation=None if reconnect is not None else checkpoint.automation,
                    ),
                )
                # An overlap token still authorizes ordinary work until its
                # effective deadline; lifecycle repair is a separate concern.
                return HostedConnectionResult(
                    "connected", status["credential_status"], bound.expires_at,
                    status.get("account_url"),
                )
            if (checkpoint.current is None or reconnect is not None) and checkpoint.pairing_verifier is not None:
                attempt = checkpoint.pairing
                live = (attempt is not None and not attempt.exchange_attempted
                        and datetime.now(UTC) < datetime.fromisoformat(attempt.expires_at))
                return HostedConnectionResult(
                    "pairing_pending" if live else "fresh_pairing_required"
                )
            return await self._previous()

    @staticmethod
    def _disconnected(state: str) -> HostedConnectionResult:
        return HostedConnectionResult(
            "disconnected" if state == "confirmed" else "remote_disconnect_unconfirmed"
        )

    async def async_start_pairing(
        self, *, processing_consent: bool, installation_label: str
    ) -> HostedPairingInstructions | HostedConnectionResult:
        """Explicit first connection; a lost creation is abandoned, never replayed.

        Each invocation creates a fresh verifier and needs owner approval. A
        saved installation or local disconnect requires separate owner/journal
        reconciliation; this operation cannot silently replace either one.
        """
        async with self._lock:
            checkpoint = self._store.snapshot
            if type(processing_consent) is not bool or not processing_consent:
                return HostedConnectionResult("consent_required")
            if checkpoint.local_disconnect != "none":
                return self._disconnected(checkpoint.local_disconnect)
            if (checkpoint.current is not None or checkpoint.candidate is not None
                    or checkpoint.reconnect is not None):
                return HostedConnectionResult("reconciliation_required")
            if checkpoint.service_binding is not None:
                self._require_service(checkpoint)
            elif checkpoint.processing_consent or checkpoint.pairing_verifier is not None:
                # Legacy material has no trustworthy destination provenance.
                raise HostedTransportError("service_reconciliation_required")
            verifier = new_pairing_verifier()
            intent = await self._store.async_checkpoint(
                checkpoint, replace(
                    checkpoint, processing_consent=True, pairing_verifier=verifier,
                    service_binding=self._transport.service_binding,
                    pairing=None,
                ),
            )
            started = await self._transport.start_pairing(verifier, installation_label)
            now = datetime.now(UTC)
            attempt = PairingAttempt(
                started["pairing_id"], started["expires_at"],
                started["poll_interval_seconds"],
                (now + timedelta(seconds=started["poll_interval_seconds"])).isoformat(),
            )
            if datetime.fromisoformat(attempt.expires_at) > now + timedelta(seconds=630):
                raise HostedTransportError("invalid_response")
            await self._store.async_checkpoint(intent, replace(intent, pairing=attempt))
            return HostedPairingInstructions(
                started["user_code"], started["verification_uri"], attempt.expires_at,
                attempt.poll_interval_seconds,
            )

    async def async_start_reconnect(
        self, *, processing_consent: bool, installation_label: str,
    ) -> HostedPairingInstructions | HostedConnectionResult:
        """Stage explicit repair without discarding old credentials or local stop.

        The local receipt proves only a supported namespace/storage contract.
        A new verifier-bound pairing still requires fresh owner approval in the
        account portal; local intent never selects connect versus replacement.
        """
        async with self._lock:
            checkpoint = self._store.snapshot
            if type(processing_consent) is not bool or not processing_consent:
                return HostedConnectionResult("consent_required")
            if checkpoint.candidate is not None:
                return HostedConnectionResult("reconciliation_required")
            self._require_service(checkpoint)
            prior = checkpoint.current.namespace if checkpoint.current is not None else None
            receipt = await self._namespace_receipt(prior)
            if receipt is None:
                return HostedConnectionResult("reconciliation_required")
            intent = ReconnectIntent(
                prior[0] if prior else None, prior[1] if prior else None,
                checkpoint.local_disconnect, receipt,
            )
            verifier = new_pairing_verifier()
            saved = await self._store.async_checkpoint(checkpoint, replace(
                checkpoint, processing_consent=True, pairing_verifier=verifier,
                reconnect=intent, pairing=None, automation=None,
            ))
            if not await self._reconnect_authorized(saved):
                return HostedConnectionResult("reconciliation_required")
            started = await self._transport.start_pairing(verifier, installation_label)
            now = datetime.now(UTC)
            attempt = PairingAttempt(
                started["pairing_id"], started["expires_at"], started["poll_interval_seconds"],
                (now + timedelta(seconds=started["poll_interval_seconds"])).isoformat(),
            )
            if datetime.fromisoformat(attempt.expires_at) > now + timedelta(seconds=630):
                raise HostedTransportError("invalid_response")
            await self._store.async_checkpoint(saved, replace(saved, pairing=attempt))
            return HostedPairingInstructions(
                started["user_code"], started["verification_uri"], attempt.expires_at,
                attempt.poll_interval_seconds,
            )

    async def async_poll_pairing(self) -> HostedConnectionResult:
        """One bounded poll and, only after owner approval, one durable exchange.

        No background task or transparent POST retry is installed. A consumed
        or uncertain exchange requires a new explicit pairing and owner approval.
        Saved candidates must pass the existing separate reconciliation path.
        """
        async with self._lock:
            checkpoint = self._store.snapshot
            if checkpoint.local_disconnect != "none" and checkpoint.reconnect is None:
                return self._disconnected(checkpoint.local_disconnect)
            if not checkpoint.processing_consent:
                return HostedConnectionResult("disconnected")
            self._require_service(checkpoint)
            if checkpoint.reconnect is not None:
                if not await self._reconnect_authorized(checkpoint):
                    return HostedConnectionResult("reconciliation_required")
            elif checkpoint.current is not None:
                return HostedConnectionResult("reconciliation_required")
            if checkpoint.candidate is not None:
                return HostedConnectionResult("recovery_required")
            attempt = checkpoint.pairing
            if attempt is None or attempt.exchange_attempted:
                return HostedConnectionResult("fresh_pairing_required")
            now = datetime.now(UTC)
            if now >= datetime.fromisoformat(attempt.expires_at):
                return HostedConnectionResult("fresh_pairing_required")
            if now < datetime.fromisoformat(attempt.next_poll_at):
                return HostedConnectionResult("pairing_wait")
            polling = await self._store.async_checkpoint(checkpoint, replace(
                checkpoint, pairing=replace(attempt, next_poll_at=(
                    now + timedelta(seconds=15 + attempt.poll_interval_seconds)
                ).isoformat()),
            ))
            if datetime.now(UTC) + timedelta(seconds=attempt.poll_interval_seconds) > datetime.fromisoformat(
                polling.pairing.next_poll_at
            ):
                return HostedConnectionResult("pairing_wait")
            try:
                status = await self._transport.pairing_status(
                    attempt.pairing_id, polling.pairing_verifier
                )
            except HostedTransportError as error:
                if error.retry_after is not None:
                    await self._store.async_checkpoint(polling, replace(
                        polling, pairing=replace(polling.pairing, next_poll_at=max(
                            datetime.fromisoformat(polling.pairing.next_poll_at),
                            datetime.now(UTC) + timedelta(seconds=error.retry_after),
                        ).isoformat()),
                    ))
                raise
            if datetime.fromisoformat(status["expires_at"].upper()) != datetime.fromisoformat(
                attempt.expires_at
            ):
                raise HostedTransportError("identity_mismatch")
            if status["status"] != "approved":
                return HostedConnectionResult(
                    "pairing_pending" if status["status"] == "pending" else "fresh_pairing_required"
                )
            if checkpoint.reconnect is not None and not await self._reconnect_authorized(polling):
                return HostedConnectionResult("reconciliation_required")
            exchange = await self._store.async_checkpoint(polling, replace(
                polling, pairing=replace(polling.pairing, exchange_attempted=True),
            ))
            if checkpoint.reconnect is not None and not await self._reconnect_authorized(exchange):
                return HostedConnectionResult("reconciliation_required")
            issued = await self._transport.exchange(
                attempt.pairing_id, exchange.pairing_verifier
            )
            candidate = self._issued_candidate(issued)
            await self._store.async_checkpoint(exchange, replace(exchange, candidate=candidate))
            return HostedConnectionResult("candidate_saved")

    @staticmethod
    def _issued_candidate(issued: dict[str, Any]) -> CandidateCredential:
        candidate = CandidateCredential(
            issued["installation_id"], issued["installation_token"],
            issued["expires_at"], issued["confirmation_expires_at"],
        )
        received = datetime.now(UTC)
        if (
            datetime.fromisoformat(candidate.expires_at)
            > received + timedelta(days=90, seconds=30)
            or datetime.fromisoformat(candidate.confirmation_expires_at)
            > received + timedelta(seconds=630)
        ):
            raise HostedTransportError("invalid_response")
        return candidate

    async def async_rotate(self) -> HostedConnectionResult:
        """Explicitly request one renewal; reconcile saved candidates separately.

        A durable gate survives crashes and response loss. It is a scheduling
        bound only: the server rechecks current authority on every issuance.
        This dormant operation starts no automatic timer or background work.
        """
        async with self._lock:
            checkpoint = self._store.snapshot
            if checkpoint.reconnect is not None:
                return HostedConnectionResult("reconciliation_required")
            if checkpoint.local_disconnect != "none":
                return self._disconnected(checkpoint.local_disconnect)
            if not checkpoint.processing_consent:
                return HostedConnectionResult("disconnected")
            self._require_service(checkpoint)
            if checkpoint.candidate is not None or checkpoint.pairing_verifier is not None:
                return HostedConnectionResult("recovery_required")
            current = checkpoint.current
            if current is None:
                return HostedConnectionResult("repair_required")
            now = datetime.now(UTC)
            if checkpoint.rotation_not_before is not None and now < datetime.fromisoformat(
                checkpoint.rotation_not_before
            ):
                return HostedConnectionResult("rotation_wait")
            try:
                status = await self._transport.installation_status(current.token)
            except HostedTransportError as error:
                if self._unauthorized(error):
                    return HostedConnectionResult("repair_required")
                raise
            bound = self._binding(status, current)
            if status["credential_status"] != "current":
                return HostedConnectionResult(
                    "repair_required", status["credential_status"], bound.expires_at,
                    status.get("account_url"),
                )
            # No POST before this durable checkpoint. The gate includes the
            # transport's whole-request budget (15s), the protocol candidate
            # lifetime (600s), and 30s scheduling skew. Local time never grants
            # authority and a server rotation_pending still prevents reissue.
            intent = await self._store.async_checkpoint(
                checkpoint,
                replace(checkpoint, rotation_not_before=(
                    datetime.now(UTC) + timedelta(seconds=645)
                ).isoformat()),
            )
            if datetime.now(UTC) + timedelta(seconds=615) > datetime.fromisoformat(
                intent.rotation_not_before
            ):
                return HostedConnectionResult("rotation_wait")
            issued = await self._transport.rotate(current.token)
            candidate = self._issued_candidate(issued)
            if candidate.installation_id != current.installation_id:
                raise HostedTransportError("identity_mismatch")
            await self._store.async_checkpoint(intent, replace(intent, candidate=candidate))
            return HostedConnectionResult("candidate_saved")

    async def async_disconnect(self) -> HostedConnectionResult:
        """Persist the local stop before one bearer-authorized disconnect attempt.

        An uncertain attempt is never replayed, including after restart. The
        owner must reconcile it through the account portal before reconnecting.
        Imports, review data and calendar events are not touched here.
        """
        async with self._lock:
            checkpoint = self._store.snapshot
            if checkpoint.reconnect is not None:
                await self._store.async_checkpoint(checkpoint, replace(
                    checkpoint, reconnect=None, local_disconnect="unconfirmed",
                ))
                return self._disconnected("unconfirmed")
            if checkpoint.local_disconnect != "none":
                return self._disconnected(checkpoint.local_disconnect)
            stopped = await self._store.async_checkpoint(
                checkpoint,
                replace(checkpoint, local_disconnect="unconfirmed"),
            )
            if stopped.current is None:
                return self._disconnected("unconfirmed")
            self._require_service(stopped)
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
                    rotation_not_before=None,
                    pairing=None,
                    automation=None,
                    reconnect=None,
                ),
            )
            return self._disconnected("confirmed")

    async def _confirm(self, candidate: Any) -> bool:
        checkpoint = self._store.snapshot
        if checkpoint.candidate != candidate:
            raise HostedCredentialConflict("candidate recovery changed")
        if candidate.confirmation_attempts >= 2:
            return False
        await self._store.async_checkpoint(checkpoint, replace(
            checkpoint, candidate=replace(candidate, confirmation_attempts=candidate.confirmation_attempts + 1),
        ))
        checkpoint = self._store.snapshot
        if checkpoint.reconnect is not None and not await self._reconnect_authorized(checkpoint):
            return False
        confirmed = await self._transport.confirm(candidate.token)
        if confirmed[
            "installation_id"
        ] != candidate.installation_id or datetime.fromisoformat(
            confirmed["expires_at"].upper()
        ) != datetime.fromisoformat(candidate.expires_at):
            raise HostedTransportError("identity_mismatch")
        return True

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
            status.get("account_url"),
        )
