"""Entry-owned P02 lifecycle execution; no implicit network activity or retries."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN
from .hosted_credentials import (
    HostedCredentialConflict,
    HostedCredentialStorageError,
    HostedCredentialStore,
)
from .hosted_lifecycle import (
    HostedConnectionResult,
    HostedLifecycleController,
    HostedPairingInstructions,
)
from .hosted_transport import (
    HostedLifecycleTransport,
    HostedServiceBinding,
    HostedTransportError,
)

CONF_HOSTED_SERVICE = "hosted_service"
_STOP_SECONDS = 1
_ACTIONS = {
    "connect": "async_start_pairing",
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


class HostedConnectionRuntime:
    """One loaded store/controller and one admitted operation per HA entry.

    Loading makes no request. An explicit native admin consumer owns consent and
    actions. No parser selection, polling, renewal or reconnect bypass lives here.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry_id = entry.entry_id
        self._service: HostedServiceBinding | None = None
        self._controller: HostedLifecycleController | None = None
        self._state = HostedConnectionResult("not_configured")
        self._accepting = False
        self._stopped = False
        self._loaded = False
        self._task: asyncio.Task | None = None
        registry = hass.data.setdefault(f"{DOMAIN}_hosted_tasks", {})
        self._tasks: set[asyncio.Task] = registry.setdefault(entry.entry_id, set())
        self._options = entry.options

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
        store = HostedCredentialStore(self._hass, self._entry_id)
        try:
            checkpoint = await store.async_load()
        except HostedCredentialStorageError:
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError
            self._state = HostedConnectionResult("storage_repair")
            return
        self._controller = HostedLifecycleController(store, HostedLifecycleTransport(
            async_get_clientsession(self._hass),
            api_origin=self._service.api_origin,
            account_origin=self._service.account_origin,
        ))
        state = "disconnected"
        if checkpoint.local_disconnect == "unconfirmed":
            state = "remote_disconnect_unconfirmed"
        elif checkpoint.service_binding is not None and checkpoint.service_binding != self._service:
            state = "service_repair"
        elif checkpoint.local_disconnect == "none" and checkpoint.processing_consent:
            state = "recovery_required"
        self._state = HostedConnectionResult(state)

    def snapshot(self) -> dict[str, Any]:
        """Return only safe fixed state; never credentials or approval instructions."""
        return {
            "entry_id": self._entry_id,
            "configured": self._service is not None,
            "api_origin": self._service.api_origin if self._service else None,
            "account_origin": self._service.account_origin if self._service else None,
            "state": self._state.state,
            "credential_status": self._state.credential_status,
            "credential_expires_at": self._state.credential_expires_at,
            "account_url": self._state.account_url,
        }

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

    def resume(self) -> None:
        """A failed platform unload may reopen the same retained runtime."""
        if not self._stopped:
            self._accepting = True

    async def async_action(self, action: str, **kwargs: Any) -> dict[str, Any]:
        """Admit one explicit action; reject queues and never replay a mutation."""
        if action not in _ACTIONS or (kwargs and action != "connect"):
            raise HostedRuntimeError("invalid_hosted_action")
        if not self._accepting or self._stopped:
            raise HostedRuntimeError("hosted_unloading")
        if self._controller is None:
            raise HostedRuntimeError("hosted_unavailable")
        if self._tasks:
            raise HostedRuntimeError("hosted_busy")
        task = self._hass.async_create_task(
            self._perform(action, kwargs), "Daylight hosted lifecycle", eager_start=False,
        )
        self._task = task
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        try:
            # Cancellation of a browser waiter must still leave ownership of a
            # cancellation-resistant operation visible to unload and reload.
            await asyncio.wait({task})
            return task.result()
        except asyncio.CancelledError:
            self._state = HostedConnectionResult("recovery_required")
            task.cancel()
            raise

    def _finished(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _perform(self, action: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await getattr(self._controller, _ACTIONS[action])(**kwargs)
        except HostedCredentialStorageError:
            self._controller = None
            self._state = HostedConnectionResult("storage_repair")
        except HostedCredentialConflict:
            self._controller = None
            self._state = HostedConnectionResult("recovery_required")
        except HostedTransportError as error:
            state = (
                "service_repair" if error.code == "service_reconciliation_required"
                else "repair_required" if error.status in (401, 403)
                else "service_unavailable"
            )
            self._state = HostedConnectionResult(state)
        except asyncio.CancelledError:
            self._state = HostedConnectionResult("recovery_required")
            raise
        except Exception:  # noqa: BLE001 -- fixed sanitized boundary
            # Never forward raw storage, request, provider or argument errors.
            self._state = HostedConnectionResult("service_unavailable")
            raise HostedRuntimeError("hosted_action_failed") from None
        else:
            if isinstance(result, HostedPairingInstructions):
                self._state = HostedConnectionResult("pairing_pending")
                return {**self.snapshot(), "pairing": asdict(result)}
            self._state = result
        return self.snapshot()

    async def async_stop(self) -> None:
        """Bound cancellation while retaining ownership until work actually ends."""
        self.pause()
        if self._stopped:
            return
        self._stopped = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.wait({self._task}, timeout=_STOP_SECONDS)


async def async_setup_hosted_runtime(
    hass: HomeAssistant, entry: ConfigEntry,
) -> HostedConnectionRuntime:
    """Construct an entry-owned runtime without activating any network work."""
    runtime = HostedConnectionRuntime(hass, entry)
    await runtime.async_load()
    return runtime
