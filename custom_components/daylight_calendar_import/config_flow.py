"""Config flow for Daylight Calendar Import."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import CONF_AI_TASK_ENTITY, CONF_CALENDAR_ENTITY, DOMAIN


class DaylightCalendarImportConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure Daylight Calendar Import."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial setup step."""
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="Daylight Calendar Import",
                data=user_input,
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_AI_TASK_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="ai_task")
                ),
                vol.Required(CONF_CALENDAR_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="calendar")
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)
