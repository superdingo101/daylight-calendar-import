"""Home Assistant queue sensors backed by durable pending-import transitions."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .storage import PendingImportStore


def pending_counts(store: PendingImportStore) -> tuple[int, int]:
    """Count pending review records, including uncertain calendar writes."""
    items = store.list()
    return len(items), sum(len(item.events) for item in items)


class _QueueSensor(SensorEntity):
    """Track persisted queue changes without polling or exposing private content."""

    _attr_should_poll = False
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self, entry: ConfigEntry, store: PendingImportStore, *,
        key: str, name: str, icon: str,
    ) -> None:
        self._store = store
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_icon = icon

    @property
    def native_value(self) -> int:
        """Read the most recently committed queue snapshot."""
        imports, events = pending_counts(self._store)
        return imports if self._key == "pending_imports" else events

    async def async_added_to_hass(self) -> None:
        """Subscribe only for the lifecycle of this HA entity."""
        self.async_on_remove(self._store.async_subscribe(self.async_write_ha_state))


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities,
) -> None:
    """Expose one import-count sensor and one event-count sensor."""
    store = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        _QueueSensor(
            entry, store, key="pending_imports",
            name="Daylight Calendar Import Pending Imports", icon="mdi:tray-full",
        ),
        _QueueSensor(
            entry, store, key="pending_events",
            name="Daylight Calendar Import Pending Events", icon="mdi:calendar-clock",
        ),
    ])
