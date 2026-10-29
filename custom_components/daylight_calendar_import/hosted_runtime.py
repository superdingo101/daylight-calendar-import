"""Entry-owned P02 lifecycle execution with explicit opt-in bounded maintenance."""

from __future__ import annotations

import asyncio
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

from .const import DOMAIN
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
from .providers import ParserProvider, ProviderError

CONF_HOSTED_SERVICE = "hosted_service"
CONF_HOSTED_AUTOMATIC = "hosted_automatic_lifecycle"


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
        else "repair_required" if error.status in (401, 403)
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
        registry = hass.data.setdefault(f"{DOMAIN}_hosted_tasks", {})
        self._tasks: set[asyncio.Task] = registry.setdefault(entry.entry_id, set())
        self._options = entry.options
        self._automatic = entry.options.get(CONF_HOSTED_AUTOMATIC) is True
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
        kwargs = ({"reconciler": LocalOnlyNamespaceReconciler(pending)}
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
        self._state = HostedConnectionResult(state)
        if self._automatic:
            self._automation = HostedAutomationPolicy(store, self._controller, self._service)

    def snapshot(self) -> dict[str, Any]:
        """Return only safe fixed state; never credentials or approval instructions."""
        return {
            "entry_id": self._entry_id,
            "configured": self._service is not None,
            "automatic_lifecycle": self._automatic,
            "reconnect_in_progress": (self._controller is not None
                                      and self._controller.reconnect_in_progress is True),
            "api_origin": self._service.api_origin if self._service else None,
            "account_origin": self._service.account_origin if self._service else None,
            "state": self._state.state,
            "credential_status": self._state.credential_status,
            "credential_expires_at": self._state.credential_expires_at,
            "account_url": self._state.account_url,
        }

    def parse_session(self) -> tuple[ParserProvider, Callable[[], None]]:
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
                self.accepting and not self.busy
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
        ), guard

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
        self._parse_generation += 1
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None

    def resume(self) -> None:
        """A failed platform unload may reopen the same retained runtime."""
        if not self._stopped:
            self._accepting = True
            if self._automatic and self._cancel_stop is None:
                self._cancel_stop = self._hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_STOP, self._async_ha_stop,
                )
            if not self._automatic:
                ir.async_delete_issue(self._hass, DOMAIN, f"hosted_lifecycle_{self._entry_id}")
            self._schedule_automation()

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
        if self.accepting and self._automatic:
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
                                    or result.state == "disconnected"):
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
