"""Entry-owned P02 lifecycle execution with explicit opt-in bounded maintenance."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, replace
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_point_in_utc_time

from .const import DOMAIN, PARSER_PROVIDER_DAYLIGHT_CLOUD
from .hosted_automation import HostedAutomationPolicy
from .hosted_credentials import (
    HostedCredentialConflict,
    HostedCredentialStorageError,
    HostedCredentialStore,
    CredentialCheckpoint,
)
from .hosted_lifecycle import (
    HostedConnectionResult,
    HostedLifecycleController,
    HostedPairingInstructions,
)
from .hosted_namespace import LocalOnlyNamespaceReconciler
from .hosted_transport import (
    HostedLifecycleTransport,
    HostedServiceBinding,
    HostedTransportError,
)
from .storage import PendingImportStore
from .settings import effective_parser_provider
from .providers import ParserProvider, ProviderError, SourceValidationError

CONF_HOSTED_SERVICE = "hosted_service"
CONF_HOSTED_AUTOMATIC = "hosted_automatic_lifecycle"
CONF_HOSTED_DELIVERY_POLL = "hosted_delivery_polling"
CONF_HOSTED_PARSE_OPERATIONS = "hosted_parse_operations_v2"
_LOGGER = logging.getLogger(__name__)
_DELIVERY_INTERVAL = 60
_MAX_DELIVERY_BACKOFF = 900


def hosted_signal(entry_id: str) -> str:
    """Private admin subscriber notification; no payload authority."""
    return f"{DOMAIN}_hosted_changed_{entry_id}"


_STOP_SECONDS = 1
_LOAD_SECONDS = 5
_ACTIONS = {
    "connect": "async_start_pairing",
    "reconnect": "async_start_reconnect",
    "poll": "async_poll_pairing",
    "reconcile": "async_reconcile",
    "rotate": "async_rotate",
    "disconnect": "async_disconnect",
}


class HostedRuntimeError(RuntimeError):
    """A fixed local action-admission error, without private data."""


def service_from_options(options: Any) -> HostedServiceBinding | None:
    """Read only explicit, closed local configuration; never infer a service."""
    value = options.get(CONF_HOSTED_SERVICE)
    if value is None:
        return None
    if type(value) is not dict or set(value) != {"api_origin", "account_origin"}:
        raise ValueError("invalid hosted configuration")
    return HostedServiceBinding(**value)


def _transport_state(error: HostedTransportError) -> str:
    return (
        "service_repair" if error.code == "service_reconciliation_required"
        else "access_unavailable" if error.status == 403 and error.code == "entitlement_required"
        else "repair_required" if error.code == "identity_mismatch" or error.status in (401, 403)
        else "service_unavailable"
    )


class HostedConnectionRuntime:
    """One loaded store/controller and one admitted operation per HA entry.

    Loading makes no request. An explicit native admin consumer owns consent and
    actions. Opt-in maintenance shares the same operation owner and durable policy.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry_id = entry.entry_id
        self._service: HostedServiceBinding | None = None
        self._controller: HostedLifecycleController | None = None
        self._credential_store: HostedCredentialStore | None = None
        self._parse_generation = 0
        self._verified_parse_credential = None
        self._state = HostedConnectionResult("not_configured")
        self._accepting = False
        self._stopped = False
        self._loaded = False
        self._task: asyncio.Task | None = None
        self._delivery_owner: asyncio.Task | None = None
        self._delivery_handlers = None
        registry = hass.data.setdefault(f"{DOMAIN}_hosted_tasks", {})
        self._tasks: set[asyncio.Task] = registry.setdefault(entry.entry_id, set())
        self._options = entry.options
        self._commercial_status = None
        self._automatic = entry.options.get(CONF_HOSTED_AUTOMATIC) is True
        self._delivery_poll_enabled = entry.options.get(CONF_HOSTED_DELIVERY_POLL) is True
        self._delivery_parser_selected = (
            self._delivery_poll_enabled
            and effective_parser_provider(entry) == PARSER_PROVIDER_DAYLIGHT_CLOUD
        )
        self._delivery_failures = 0
        self._delivery_due_at: datetime | None = None
        self._delivery_poll_timer = None
        self._delivery_poll_running = False
        self._automation: HostedAutomationPolicy | None = None
        self._cancel_timer = None
        self._cancel_stop = None

    async def async_load(self) -> None:
        """Load once, retaining hosted errors separately from local/BYO setup."""
        if self._loaded:
            raise HostedRuntimeError("hosted_already_loaded")
        self._loaded = True
        try:
            self._service = service_from_options(self._options)
        except (ValueError, TypeError):
            self._state = HostedConnectionResult("configuration_repair")
            return
        if self._service is None:
            return
        if self._tasks:
            self._state = HostedConnectionResult("operation_finishing")
            return
        await self._async_load_controller()

    async def _async_load_controller(self) -> None:
        """Load only at setup or in an admitted explicit recovery operation."""
        store = HostedCredentialStore(self._hass, self._entry_id)
        try:
            async with asyncio.timeout(_LOAD_SECONDS):
                checkpoint = await store.async_load()
        except (HostedCredentialStorageError, TimeoutError):
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError
            self._state = HostedConnectionResult("storage_repair")
            return
        pending = self._hass.data.get(DOMAIN, {}).get(self._entry_id)
        kwargs = ({"reconciler": LocalOnlyNamespaceReconciler(pending, allow_delivery=True)}
                  if type(pending) is PendingImportStore else {})
        self._credential_store = store
        self._verified_parse_credential = None
        self._controller = HostedLifecycleController(store, HostedLifecycleTransport(
            async_get_clientsession(self._hass),
            api_origin=self._service.api_origin,
            account_origin=self._service.account_origin,
        ), **kwargs)
        state = "disconnected"
        if checkpoint.service_binding is not None and checkpoint.service_binding != self._service:
            state = "service_repair"
        elif checkpoint.reconnect is not None:
            state = "reconnect_pending"
        elif checkpoint.local_disconnect == "unconfirmed":
            state = "remote_disconnect_unconfirmed"
        elif checkpoint.local_disconnect == "none" and checkpoint.processing_consent:
            state = "recovery_required"
            if (not self._automatic and checkpoint.service_binding == self._service
                    and checkpoint.current is not None and checkpoint.candidate is None
                    and checkpoint.pairing_verifier is None):
                state = "checking_access"
        self._state = HostedConnectionResult(state)
        if self._automatic:
            self._automation = HostedAutomationPolicy(store, self._controller, self._service)

    def snapshot(self) -> dict[str, Any]:
        """Return only safe fixed state; never credentials or approval instructions."""
        return {
            "entry_id": self._entry_id,
            "configured": self._service is not None,
            "automatic_lifecycle": self._automatic,
            **({"delivery_polling": True} if self._delivery_poll_enabled else {}),
            **({"delivery_parse_uncertain": True} if self._has_delivery_parse_attempt() else {}),
            "reconnect_in_progress": (self._controller is not None
                                      and self._controller.reconnect_in_progress is True),
            "api_origin": self._service.api_origin if self._service else None,
            "account_origin": self._service.account_origin if self._service else None,
            "state": self._state.state,
            "credential_status": self._state.credential_status,
            "credential_expires_at": self._state.credential_expires_at,
            "account_url": self._state.account_url,
            **({"parse_operations_v2": True, "commercial_status": self._commercial_status}
               if self._options.get(CONF_HOSTED_PARSE_OPERATIONS) is True else {}),
        }

    def parse_session(self, *, delivery_owner: bool = False) -> tuple[ParserProvider, Callable[[], None]]:
        """Create one private, generation-fenced parser and acceptance guard.

        No credential is added to the public settings snapshot. A previously
        loaded checkpoint is insufficient: the current runtime must have verified
        it, and lifecycle changes invalidate an already-started parse permanently.
        """
        from .hosted_parser import DaylightHostedParserProvider

        generation = self._parse_generation
        store, service = self._credential_store, self._service
        task = asyncio.current_task()

        def active() -> bool:
            return (
                self.accepting and (not self.busy or (
                    delivery_owner and task is self._delivery_owner
                    and self._tasks == {task}
                ))
                and self._parse_generation == generation
                and self._state.credential_status in ("current", "overlap")
                and store is not None and self._credential_store is store
                and self._service == service
                and task is not None and not task.cancelling()
            )

        def checkpoint() -> CredentialCheckpoint | None:
            try:
                return store.snapshot if store is not None else None
            except HostedCredentialStorageError:
                return None

        initial = checkpoint()

        def guard() -> None:
            current = checkpoint()
            if (not active() or service is None or initial is None
                    or current is None or current.service_binding != service
                    or not current.processing_consent or current.local_disconnect != "none"
                    or current.reconnect is not None or current.current is None
                    or current.current != initial.current
                    or current.current != self._verified_parse_credential
                    or datetime.now(UTC) >= datetime.fromisoformat(current.current.expires_at)):
                raise ProviderError(
                    "hosted_connection_changed",
                    "Daylight Cloud connection changed or needs verification. Open Daylight Cloud settings before retrying.",
                )

        guard()
        return DaylightHostedParserProvider(
            self._hass, service_binding=service, checkpoint=checkpoint, active=active,
            **({"operation_store": self._hass.data[DOMAIN][self._entry_id],
                "operation_namespace": self._entry_id + ":" + service.api_origin}
               if self._options.get(CONF_HOSTED_PARSE_OPERATIONS) is True else {}),
        ), guard

    async def async_account_status(self):
        """Fetch trusted high-level access status without exposing provider IDs."""
        from .hosted_operations import validate_account
        from .hosted_parse_transport import HostedParseError
        try:
            provider, guard = self.parse_session()
            credential = provider._authority()
            value = await provider._transport.async_v2_get("/account", token=credential.token)
            guard()
            self._commercial_status = validate_account(value, self._service.account_origin)
        except (HostedParseError, ProviderError, ValueError):
            raise HostedRuntimeError("hosted_action_failed") from None
        return self.snapshot()

    def configure_delivery(self, *, store, route_source, parse_source) -> None:
        """Bind entry-local routing and parsing without starting delivery."""
        if self._delivery_handlers is not None:
            raise HostedRuntimeError("hosted_already_configured")
        self._delivery_handlers = (store, route_source, parse_source)

    async def async_delivery_pass(self, *, limit=20):
        """Run one explicitly requested recovery-first pass; never schedule it."""
        if not self.accepting or self.busy or self._controller is None:
            raise HostedRuntimeError("hosted_unavailable")
        if self._credential_store is None or self._service is None or self._delivery_handlers is None:
            raise HostedRuntimeError("hosted_unavailable")
        if (self._state.credential_status not in ("current", "overlap")
                or self._credential_store.snapshot.current is None
                or self._credential_store.snapshot.current != self._verified_parse_credential):
            raise HostedRuntimeError("hosted_unavailable")
        store, route_source, parse_source = self._delivery_handlers
        from .hosted_ack_recovery import async_recover_pending_acks
        from .hosted_parse_transport import HostedParseError
        from .hosted_source_transport import HostedSourceTransport
        from .hosted_text_batch import async_process_text_batch

        async def run():
            self._delivery_owner = asyncio.current_task()
            try:
                owner = self._delivery_owner
                generation = self._parse_generation

                def guard_delivery(*, require_owner=False):
                    if (not self.accepting or owner is None or owner.cancelling()
                            or (require_owner and asyncio.current_task() is not owner)
                            or self._delivery_owner is not owner or self._tasks != {owner}
                            or self._parse_generation != generation):
                        raise HostedRuntimeError("hosted_unavailable")

                async def guarded_parse(source):
                    guard_delivery(require_owner=True)
                    if self._delivery_poll_running:
                        await store.async_change_hosted_parse_attempt(source.upstream_source_id)
                        guard_delivery(require_owner=True)
                    try:
                        outcome, parser_guard = await parse_source(source)
                    except (SourceValidationError, HostedParseError) as error:
                        definitive = isinstance(error, SourceValidationError) or (
                            not error.uncertain and error.status in (400, 401, 403, 413, 429)
                        )
                        if self._delivery_poll_running and definitive:
                            await store.async_change_hosted_parse_attempt(None)
                        raise
                    guard_delivery(require_owner=True)

                    def acceptance_guard():
                        guard_delivery()
                        if parser_guard is not None:
                            parser_guard()

                    acceptance_guard.hosted_operation_id = getattr(parser_guard, "hosted_operation_id", None)
                    return outcome, acceptance_guard

                session = async_get_clientsession(self._hass)
                claim = HostedSourceTransport(session, self._service)
                ack = HostedLifecycleTransport(
                    session, api_origin=self._service.api_origin,
                    account_origin=self._service.account_origin,
                )
                recovered = await async_recover_pending_acks(
                    store=store, credentials=self._credential_store, transport=ack,
                    local_config_entry_id=self._entry_id, limit=limit,
                )
                guard_delivery(require_owner=True)
                if recovered and any(item.ack_status == "expired" for item in recovered):
                    self._delivery_attention_issue("hosted_delivery_expired")
                try:
                    return await async_process_text_batch(
                        store=store, credentials=self._credential_store,
                        claim_transport=claim, ack_transport=ack,
                        local_config_entry_id=self._entry_id, route_source=route_source,
                        parse_source=guarded_parse, limit=limit,
                    )
                finally:
                    if type(store) is PendingImportStore and not store._storage_blocked and any(
                        item.ack_status == "expired"
                        and item.evidence.local_config_entry_id == self._entry_id
                        for item in store.hosted_ack_journal()
                    ):
                        self._delivery_attention_issue("hosted_delivery_expired")
            finally:
                self._delivery_owner = None

        self._parse_generation += 1
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None
        task = self._hass.async_create_task(run(), "Daylight hosted delivery", eager_start=False)
        self._task = task
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        try:
            await asyncio.wait({task})
            return task.result()
        except asyncio.CancelledError:
            task.cancel()
            raise

    @property
    def accepting(self) -> bool:
        """Whether the entry owner currently admits explicit native actions."""
        return self._accepting and not self._stopped

    @property
    def busy(self) -> bool:
        """Whether any still-owned lifecycle operation prevents another action."""
        return bool(self._tasks)

    def pause(self) -> None:
        """Close admission before platform unload starts."""
        self._accepting = False
        self._cancel_delivery_poll()
        self._parse_generation += 1
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None

    def resume(self) -> None:
        """A failed platform unload may reopen the same retained runtime."""
        if not self._stopped:
            self._accepting = True
            if (self._automatic or self._delivery_poll_enabled) and self._cancel_stop is None:
                self._cancel_stop = self._hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_STOP, self._async_ha_stop,
                )
            if not self._delivery_poll_enabled:
                ir.async_delete_issue(self._hass, DOMAIN, f"hosted_delivery_retry_{self._entry_id}")
            if not self._automatic:
                ir.async_delete_issue(self._hass, DOMAIN, f"hosted_lifecycle_{self._entry_id}")
            if self._state.state == "checking_access" and not self.busy:
                self._start_work("verify_saved", {})
            self._schedule_automation()
            self._schedule_delivery_poll()

    async def _async_ha_stop(self, _event) -> None:
        await self.async_stop()

    def _schedule_automation(self) -> None:
        """One HA timer, only after activation, with no IO or competing owner."""
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None
        if not self.accepting or self.busy:
            return
        if self._automatic and self._state.state in (
            "storage_repair", "configuration_repair", "service_repair", "repair_required",
            "reconciliation_required", "fresh_pairing_required", "remote_disconnect_unconfirmed",
        ):
            self._attention_issue()
            return
        if self._automation is None or self._controller is None:
            return
        plan = self._automation.plan(self._state)
        if plan is None:
            return
        if plan.attention:
            self._state = replace(self._state, state="automatic_attention_required")
            self._attention_issue()
            return
        self._cancel_timer = async_track_point_in_utc_time(
            self._hass, self._automatic_tick,
            max(plan.due_at, datetime.now(UTC) + timedelta(seconds=1)),
        )

    def _cancel_delivery_poll(self) -> None:
        if self._delivery_poll_timer is not None:
            self._delivery_poll_timer()
            self._delivery_poll_timer = None

    def _has_delivery_parse_attempt(self) -> bool:
        return (
            self._delivery_handlers is not None
            and getattr(self._delivery_handlers[0], "hosted_parse_uncertain", False) is True
        )

    def _foreground_delivery_conflict(self) -> bool:
        store = self._delivery_handlers[0]
        return bool(getattr(store, "active_submissions", ()) or
                    getattr(store, "active_service_handlers", ()))

    async def async_retry_delivery_parse(self) -> dict[str, Any]:
        """Clear an uncertain attempt only after an explicit administrator decision."""
        if not self.accepting or not self._delivery_poll_enabled or not self._has_delivery_parse_attempt():
            raise HostedRuntimeError("hosted_unavailable")
        if self.busy or self._delivery_poll_running:
            raise HostedRuntimeError("hosted_busy")
        self._delivery_poll_running = True
        try:
            await self._delivery_handlers[0].async_change_hosted_parse_attempt(None)
            ir.async_delete_issue(self._hass, DOMAIN, f"hosted_delivery_uncertain_{self._entry_id}")
            self._delivery_due_at = None
        finally:
            self._delivery_poll_running = False
            self._schedule_delivery_poll()
        return self.snapshot()

    def _schedule_delivery_poll(self) -> None:
        self._cancel_delivery_poll()
        if (not self._delivery_poll_enabled or not self._delivery_parser_selected
                or not self.accepting or self.busy or self._delivery_poll_running
                or self._delivery_handlers is None
                or self._state.credential_status not in ("current", "overlap")):
            return
        if self._has_delivery_parse_attempt():
            self._delivery_attention_issue("hosted_delivery_uncertain")
            return
        now = datetime.now(UTC)
        due = self._delivery_due_at or now + timedelta(seconds=1)
        self._delivery_poll_timer = async_track_point_in_utc_time(
            self._hass, self._delivery_poll_tick, max(due, now + timedelta(seconds=1)),
        )

    @callback
    def _delivery_poll_tick(self, _now) -> None:
        self._delivery_poll_timer = None
        if not self.accepting or self.busy or self._delivery_poll_running:
            return
        if self._foreground_delivery_conflict():
            self._delivery_due_at = datetime.now(UTC) + timedelta(seconds=_DELIVERY_INTERVAL)
            self._schedule_delivery_poll()
            return
        self._delivery_poll_running = True
        self._hass.async_create_task(self._async_delivery_poll(), "Daylight delivery poll", eager_start=False)

    async def _async_delivery_poll(self) -> None:
        from .hosted_parse_transport import HostedParseError

        delay = _DELIVERY_INTERVAL
        try:
            if self._foreground_delivery_conflict():
                return
            # Each pass makes one claim POST at most. A new pass after backoff
            # first checks the durable ACK journal, even after an uncertain POST.
            async with asyncio.timeout(110):
                await self.async_delivery_pass(limit=1)
        except Exception as error:
            self._delivery_failures = min(6, self._delivery_failures + 1)
            delay = min(_MAX_DELIVERY_BACKOFF, _DELIVERY_INTERVAL * 2 ** self._delivery_failures)
            if isinstance(error, (HostedTransportError, HostedParseError)):
                delay = max(delay, error.retry_after or 0)
            if self._delivery_failures == 5:
                self._delivery_attention_issue("hosted_delivery_retry")
                _LOGGER.warning("Daylight hosted delivery polling is backing off")
        else:
            self._delivery_failures = 0
            ir.async_delete_issue(self._hass, DOMAIN, f"hosted_delivery_retry_{self._entry_id}")
        finally:
            self._delivery_due_at = datetime.now(UTC) + timedelta(seconds=delay)
            self._delivery_poll_running = False
            self._schedule_delivery_poll()

    def _delivery_attention_issue(self, key: str) -> None:
        ir.async_create_issue(
            self._hass, DOMAIN, f"{key}_{self._entry_id}", is_fixable=False,
            severity=ir.IssueSeverity.WARNING, translation_key=key,
            is_persistent=key in ("hosted_delivery_expired", "hosted_delivery_uncertain"),
        )

    def _attention_issue(self) -> None:
        ir.async_create_issue(
            self._hass, DOMAIN, f"hosted_lifecycle_{self._entry_id}",
            is_fixable=False, severity=ir.IssueSeverity.WARNING,
            translation_key="hosted_lifecycle", is_persistent=False,
        )

    @callback
    def _automatic_tick(self, _now) -> None:
        self._cancel_timer = None
        if self.accepting and not self.busy and self._controller is not None:
            self._start_work("automatic", {})

    async def async_action(self, action: str, **kwargs: Any) -> dict[str, Any]:
        """Admit one explicit action; reject queues and never replay a mutation."""
        if action not in _ACTIONS or (kwargs and action not in ("connect", "reconnect")):
            raise HostedRuntimeError("invalid_hosted_action")
        if not self._accepting or self._stopped:
            raise HostedRuntimeError("hosted_unloading")
        if self._tasks:
            raise HostedRuntimeError("hosted_busy")
        if self._controller is None and not (
            action == "reconcile" and self._state.state == "operation_finishing"
        ):
            raise HostedRuntimeError("hosted_unavailable")
        task = self._start_work(action, kwargs)
        try:
            # Cancellation of a browser waiter must still leave ownership of a
            # cancellation-resistant operation visible to unload and reload.
            await asyncio.wait({task})
            return task.result()
        except asyncio.CancelledError:
            if self._controller is not None:
                self._state = HostedConnectionResult("recovery_required")
            task.cancel()
            raise

    def _start_work(self, action: str, kwargs: dict[str, Any]) -> asyncio.Task:
        self._parse_generation += 1
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None
        task = self._hass.async_create_task(
            self._perform(action, kwargs), "Daylight hosted lifecycle", eager_start=False,
        )
        self._task = task
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()
        self._schedule_automation()
        self._schedule_delivery_poll()
        if self.accepting:
            async_dispatcher_send(self._hass, hosted_signal(self._entry_id))

    async def _perform(self, action: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        try:
            if self._controller is None:
                await self._async_load_controller()
                if self._controller is None:
                    return self.snapshot()
            if action == "automatic":
                result = await self._automation.async_step(self._state)
                if result is None:
                    return self.snapshot()
            elif action == "verify_saved":
                result = await self._controller.async_verify_saved()
            else:
                try:
                    result = await getattr(self._controller, _ACTIONS[action])(**kwargs)
                except HostedTransportError as error:
                    if self._automation is not None:
                        await self._automation.async_note_failure(
                            HostedConnectionResult(_transport_state(error)), error,
                        )
                    raise
                if self._automation is not None and isinstance(result, HostedConnectionResult):
                    await self._automation.async_note_verified(result)
            if isinstance(result, HostedConnectionResult):
                self._verified_parse_credential = (
                    self._credential_store.snapshot.current
                    if result.credential_status in ("current", "overlap")
                    and self._credential_store is not None else None
                )
        except HostedCredentialStorageError:
            self._controller = None
            self._automation = None
            self._state = HostedConnectionResult("storage_repair")
        except HostedCredentialConflict:
            self._controller = None
            self._automation = None
            self._state = HostedConnectionResult("recovery_required")
        except HostedTransportError as error:
            self._state = HostedConnectionResult(_transport_state(error))
            if error.retryable and self._automation is not None and self._state.state == "service_unavailable":
                plan = self._automation.plan(self._state)
                if plan is not None and plan.phase == "pairing" and not plan.attention:
                    self._state = HostedConnectionResult("pairing_retry")
        except asyncio.CancelledError:
            if self._controller is not None:
                self._state = HostedConnectionResult("recovery_required")
            raise
        except Exception:  # noqa: BLE001 -- fixed sanitized boundary
            # Never forward raw storage, request, provider or argument errors.
            self._state = HostedConnectionResult("service_unavailable")
            raise HostedRuntimeError("hosted_action_failed") from None
        else:
            if isinstance(result, HostedPairingInstructions):
                self._state = HostedConnectionResult("reconnect_pending" if action == "reconnect" else "pairing_pending")
                return {**self.snapshot(), "pairing": asdict(result)}
            self._state = result
            if self._automatic and (result.credential_status in ("current", "overlap")
                                    or result.state in ("access_unavailable", "disconnected")):
                ir.async_delete_issue(self._hass, DOMAIN, f"hosted_lifecycle_{self._entry_id}")
        if self._automation is not None:
            plan = self._automation.plan(self._state)
            if plan is not None and plan.attention:
                self._state = replace(self._state, state="automatic_attention_required")
        return self.snapshot()

    async def async_stop(self) -> None:
        """Bound cancellation while retaining ownership until work actually ends."""
        self.pause()
        if self._stopped:
            return
        self._stopped = True
        if self._cancel_stop is not None:
            self._cancel_stop()
            self._cancel_stop = None
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.wait({self._task}, timeout=_STOP_SECONDS)


async def async_setup_hosted_runtime(
    hass: HomeAssistant, entry: ConfigEntry,
) -> HostedConnectionRuntime:
    """Construct a paused runtime; activation follows successful platform setup."""
    runtime = HostedConnectionRuntime(hass, entry)
    await runtime.async_load()
    return runtime
