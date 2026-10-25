"""Durable bounded lifecycle policy; timers and opt-in belong to the runtime."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from .hosted_credentials import (
    AutomationAttempt,
    AutomationState,
    HostedCredentialStore,
)
from .hosted_lifecycle import HostedConnectionResult, HostedLifecycleController
from .hosted_transport import HostedServiceBinding, HostedTransportError

_ATTENTION = "automatic_attention_required"
_TERMINAL = {
    "repair_required", "service_repair", "storage_repair", "reconciliation_required",
    "disconnected", "remote_disconnect_unconfirmed", "fresh_pairing_required", "not_configured",
}
_METHODS = {"reconcile": "async_reconcile", "poll": "async_poll_pairing", "rotate": "async_rotate"}


@dataclass(frozen=True, slots=True)
class AutomationPlan:
    """Internal scheduling metadata, containing neither a token nor a verifier."""

    action: str
    phase: str
    scope: str
    limit: int
    due_at: datetime
    attention: bool = False


def _scope(phase: str, identity: str) -> str:
    return hashlib.sha256((phase + ":" + identity).encode("ascii")).hexdigest()


def _jitter() -> int:
    return secrets.randbelow(5) + 1


class HostedAutomationPolicy:
    """Reserve before work; never create pairing or replay uncertain issuance.

    One instance belongs to the entry runtime's serialized operation owner.
    The private checkpoint remains the only durable scheduling substrate.
    """

    def __init__(self, store: HostedCredentialStore, controller: HostedLifecycleController,
                 service: HostedServiceBinding) -> None:
        self._store = store
        self._controller = controller
        self._service = service
        self._lock = asyncio.Lock()

    def _eligible(self, checkpoint: Any) -> bool:
        return (checkpoint.processing_consent and checkpoint.local_disconnect == "none"
                and checkpoint.service_binding == self._service)

    def plan(self, state: HostedConnectionResult) -> AutomationPlan | None:
        """Read a plan without IO, remote work, or treating a deadline as authority."""
        checkpoint = self._store.snapshot
        if not self._eligible(checkpoint) or state.state in _TERMINAL:
            return None
        now = datetime.now(UTC)
        due = now
        if checkpoint.candidate is not None:
            phase, action, limit = "candidate", "reconcile", 6
            identity = checkpoint.candidate.token
        elif checkpoint.current is None:
            attempt = checkpoint.pairing
            if attempt is None:
                return None
            phase, action, limit = "pairing", "poll", 32
            identity = attempt.pairing_id
            if attempt.exchange_attempted or now >= datetime.fromisoformat(attempt.expires_at):
                return AutomationPlan(action, phase, _scope(phase, identity), limit, due, True)
            due = max(now, datetime.fromisoformat(attempt.next_poll_at))
        elif state.credential_status == "overlap":
            return None
        elif state.credential_status == "current" and state.state in (
            "connected", "previous_retained", _ATTENTION,
        ):
            phase, action, limit = "rotation", "rotate", 6
            identity = checkpoint.current.token
            if checkpoint.rotation_not_before is not None:
                # A lost issuance may have committed. Only an explicit human
                # action may try fresh issuance after the existing wait gate.
                return AutomationPlan(action, phase, _scope(phase, identity), limit, due, True)
            expiry = datetime.fromisoformat(state.credential_expires_at)
            if expiry <= now:
                phase, action, limit = "status", "reconcile", 6
            else:
                due = max(now, expiry - timedelta(days=7))
        else:
            phase, action, limit = "status", "reconcile", 6
            identity = checkpoint.current.token
        scope = _scope(phase, identity)
        record = getattr(checkpoint.automation or AutomationState(), phase)
        attention = False
        if record is not None and record.scope == scope:
            due = max(due, datetime.fromisoformat(record.not_before))
            attention = record.attempts >= limit or due > now + timedelta(days=91)
        if phase == "pairing":
            due = min(due, datetime.fromisoformat(checkpoint.pairing.expires_at))
        return AutomationPlan(action, phase, scope, limit, due, attention)

    async def async_step(self, state: HostedConnectionResult) -> HostedConnectionResult | None:
        """Execute one eligible operation, with durable attempt and retry gates."""
        async with self._lock:
            plan = self.plan(state)
            if plan is None:
                return None
            if plan.attention:
                return replace(state, state=_ATTENTION)
            if datetime.now(UTC) < plan.due_at:
                return None
            checkpoint = self._store.snapshot
            automatic = checkpoint.automation or AutomationState()
            record = getattr(automatic, plan.phase)
            attempts = record.attempts if record is not None and record.scope == plan.scope else 0
            reservation = AutomationAttempt(
                plan.scope, attempts + 1,
                (datetime.now(UTC) + timedelta(seconds=15 + _jitter())).isoformat(),
            )
            await self._store.async_checkpoint(checkpoint, replace(checkpoint, automation=replace(
                automatic, **{plan.phase: reservation},
            )))
            if datetime.now(UTC) >= datetime.fromisoformat(reservation.not_before):
                return None  # A slow write cannot authorize work from a stale reservation.
            try:
                result = await getattr(self._controller, _METHODS[plan.action])()
            except HostedTransportError as error:
                await self._complete(plan, error=error)
                raise
            await self._complete(plan, result=result)
            return result

    @staticmethod
    def _verified_status(checkpoint: Any, result: HostedConnectionResult) -> AutomationAttempt | None:
        if checkpoint.current is None or result.credential_status not in ("current", "overlap"):
            return None
        return AutomationAttempt(
            _scope("status", checkpoint.current.token), 0,
            (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        )

    async def _complete(self, plan: AutomationPlan, *, result: HostedConnectionResult | None = None,
                        error: HostedTransportError | None = None) -> None:
        checkpoint = self._store.snapshot
        if not self._eligible(checkpoint):
            return
        automatic = checkpoint.automation or AutomationState()
        record = getattr(automatic, plan.phase)
        if record is None or record.scope != plan.scope:
            return  # A newer phase/identity owns its own independent budget.
        if error is not None:
            attempts = record.attempts if error.retryable else plan.limit
            delay = (error.retry_after if error.retry_after is not None
                     else min(300, 15 * 2 ** (record.attempts - 1)))
            record = replace(record, attempts=attempts, not_before=max(
                datetime.fromisoformat(record.not_before),
                datetime.now(UTC) + timedelta(seconds=delay + _jitter()),
            ).isoformat())
            automatic = replace(automatic, **{plan.phase: record})
            automatic = self._carry_failure_gate(checkpoint, automatic, record.not_before)
        if result is not None:
            verified = self._verified_status(checkpoint, result)
            if verified is not None:
                automatic = replace(automatic, status=verified)
        await self._store.async_checkpoint(checkpoint, replace(checkpoint, automation=automatic))

    @staticmethod
    def _carry_failure_gate(checkpoint: Any, automatic: AutomationState, gate: str) -> AutomationState:
        """A renewal and recovery can call the same status endpoint."""
        if checkpoint.current is None:
            return automatic
        scope = _scope("status", checkpoint.current.token)
        record = automatic.status
        if record is None or record.scope != scope:
            record = AutomationAttempt(scope, 0, gate)
        else:
            record = replace(record, not_before=max(
                datetime.fromisoformat(record.not_before), datetime.fromisoformat(gate),
            ).isoformat())
        return replace(automatic, status=record)

    async def async_note_failure(self, state: HostedConnectionResult, error: HostedTransportError) -> None:
        """Manual failures must gate subsequent automatic work without granting authority."""
        async with self._lock:
            plan = self.plan(state)
            if plan is None or plan.attention:
                return
            checkpoint = self._store.snapshot
            automatic = checkpoint.automation or AutomationState()
            record = getattr(automatic, plan.phase)
            attempts = record.attempts if record is not None and record.scope == plan.scope else 0
            delay = error.retry_after if error.retry_after is not None else 15
            gate = max(plan.due_at, datetime.now(UTC) + timedelta(seconds=delay + _jitter())).isoformat()
            record = AutomationAttempt(plan.scope, attempts if error.retryable else plan.limit, gate)
            automatic = replace(automatic, **{plan.phase: record})
            automatic = self._carry_failure_gate(checkpoint, automatic, gate)
            await self._store.async_checkpoint(checkpoint, replace(checkpoint, automation=automatic))

    async def async_note_verified(self, result: HostedConnectionResult) -> None:
        """A successful explicit recovery can reopen bounded status scheduling."""
        async with self._lock:
            checkpoint = self._store.snapshot
            if not self._eligible(checkpoint):
                return
            verified = self._verified_status(checkpoint, result)
            if verified is not None:
                await self._store.async_checkpoint(checkpoint, replace(checkpoint, automation=replace(
                    checkpoint.automation or AutomationState(), status=verified,
                )))
