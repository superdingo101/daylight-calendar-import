"""Config flow for Daylight Calendar Import."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_AI_TASK_ENTITY,
    CONF_CALENDAR_ENTITIES,
    CONF_CALENDAR_ENTITY,
    CONF_EMAIL_ENABLED,
    CONF_EMAIL_HOST,
    CONF_EMAIL_MAILBOX,
    CONF_EMAIL_PASSWORD,
    CONF_EMAIL_PORT,
    CONF_EMAIL_USERNAME,
    CONF_EMAIL_VERIFY_SSL,
    DOMAIN,
)
from .email_runtime import DEFAULT_EMAIL_MAILBOX, DEFAULT_EMAIL_PORT
from .settings import (
    EMAIL_OPTION_KEYS,
    SettingsValidationError,
    async_validate_email_options,
    effective_core_options,
    normalize_core_options,
)


def _ai_task_selector() -> selector.EntitySelector:
    """Select one AI Task entity capable of structured generation."""
    return selector.EntitySelector(
        selector.EntitySelectorConfig(
            filter={
                "domain": "ai_task",
                "supported_features": [
                    "ai_task.AITaskEntityFeature.GENERATE_DATA"
                ],
            }
        )
    )


def _calendar_selector(*, multiple: bool = False) -> selector.EntitySelector:
    """Select one or more writable calendar entities."""
    return selector.EntitySelector(
        selector.EntitySelectorConfig(
            multiple=multiple,
            filter={
                "domain": "calendar",
                "supported_features": [
                    "calendar.CalendarEntityFeature.CREATE_EVENT"
                ],
            },
        )
    )


class DaylightCalendarImportConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure Daylight Calendar Import."""

    VERSION = 2

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Create the integration options flow."""
        del config_entry
        return DaylightCalendarImportOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial setup step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                core_options = normalize_core_options(
                    user_input[CONF_AI_TASK_ENTITY],
                    user_input[CONF_CALENDAR_ENTITY],
                    user_input[CONF_CALENDAR_ENTITIES],
                )
            except SettingsValidationError as err:
                errors[CONF_CALENDAR_ENTITY] = err.code
            else:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Daylight Calendar Import",
                    data=core_options,
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_AI_TASK_ENTITY): _ai_task_selector(),
                vol.Required(CONF_CALENDAR_ENTITY): _calendar_selector(),
                vol.Required(CONF_CALENDAR_ENTITIES): _calendar_selector(
                    multiple=True
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)



class DaylightCalendarImportOptionsFlow(config_entries.OptionsFlowWithReload):
    """Configure AI, calendar, and optional Direct IMAP settings."""

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Configure core runtime choices and enable or disable email ingestion."""
        current = self.config_entry.options
        ai_task_entity, default_calendar, allowed_calendars = effective_core_options(
            self.config_entry
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                self._pending_core_options = normalize_core_options(
                    user_input[CONF_AI_TASK_ENTITY],
                    user_input[CONF_CALENDAR_ENTITY],
                    user_input[CONF_CALENDAR_ENTITIES],
                )
            except SettingsValidationError as err:
                errors[CONF_CALENDAR_ENTITY] = err.code
            else:
                if not user_input[CONF_EMAIL_ENABLED]:
                    return self.async_create_entry(
                        data={
                            **current,
                            **self._pending_core_options,
                            CONF_EMAIL_ENABLED: False,
                        }
                    )
                return await self.async_step_email()

        schema = vol.Schema(
            {
                vol.Required(CONF_AI_TASK_ENTITY): _ai_task_selector(),
                vol.Required(CONF_CALENDAR_ENTITY): _calendar_selector(),
                vol.Required(CONF_CALENDAR_ENTITIES): _calendar_selector(
                    multiple=True
                ),
                vol.Required(CONF_EMAIL_ENABLED): selector.BooleanSelector(),
            }
        )
        suggested_values = (
            user_input
            if user_input is not None
            else {
                CONF_AI_TASK_ENTITY: ai_task_entity,
                CONF_CALENDAR_ENTITY: default_calendar,
                CONF_CALENDAR_ENTITIES: allowed_calendars,
                CONF_EMAIL_ENABLED: current.get(CONF_EMAIL_ENABLED, False),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                schema,
                suggested_values,
            ),
            errors=errors,
        )

    async def async_step_email(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Validate and save Direct IMAP connection options."""
        current = self.config_entry.options
        core_options = getattr(self, "_pending_core_options", None)
        if core_options is None:
            ai_task_entity, default_calendar, allowed_calendars = effective_core_options(
                self.config_entry
            )
            core_options = {
                CONF_AI_TASK_ENTITY: ai_task_entity,
                CONF_CALENDAR_ENTITY: default_calendar,
                CONF_CALENDAR_ENTITIES: allowed_calendars,
            }
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                email_patch = await async_validate_email_options(
                    self.config_entry.entry_id,
                    current,
                    user_input,
                )
            except SettingsValidationError as err:
                errors["base"] = err.code
            else:
                return self.async_create_entry(
                    data={
                        **current,
                        **core_options,
                        **email_patch,
                    }
                )

        suggested_values = (
            user_input
            if user_input is not None
            else {
                key: current[key]
                for key in EMAIL_OPTION_KEYS
                if key in current
            }
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_EMAIL_HOST): selector.TextSelector(),
                vol.Required(
                    CONF_EMAIL_PORT,
                    default=DEFAULT_EMAIL_PORT,
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1,
                        max=65535,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(CONF_EMAIL_USERNAME): selector.TextSelector(),
                vol.Optional(
                    CONF_EMAIL_PASSWORD,
                    default="",
                ): selector.TextSelector(
                    selector.TextSelectorConfig(
                        type=selector.TextSelectorType.PASSWORD,
                    )
                ),
                vol.Required(
                    CONF_EMAIL_MAILBOX,
                    default=DEFAULT_EMAIL_MAILBOX,
                ): selector.TextSelector(),
                vol.Required(
                    CONF_EMAIL_VERIFY_SSL,
                    default=True,
                ): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="email",
            data_schema=self.add_suggested_values_to_schema(
                schema,
                suggested_values,
            ),
            errors=errors,
        )
