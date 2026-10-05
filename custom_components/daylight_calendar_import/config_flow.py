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
from .direct_imap import (
    DirectImapAuthenticationError,
    DirectImapError,
    DirectImapMailboxError,
    DirectImapSource,
)
from .email_runtime import (
    DEFAULT_EMAIL_MAILBOX,
    DEFAULT_EMAIL_PORT,
    direct_imap_settings_from_options,
)


_EMAIL_OPTION_KEYS = (
    CONF_EMAIL_HOST,
    CONF_EMAIL_PORT,
    CONF_EMAIL_USERNAME,
    CONF_EMAIL_MAILBOX,
    CONF_EMAIL_VERIFY_SSL,
)


def _calendar_selector() -> selector.EntitySelector:
    """Select one writable calendar entity."""
    return selector.EntitySelector(
        selector.EntitySelectorConfig(
            filter={
                "domain": "calendar",
                "supported_features": [
                    "calendar.CalendarEntityFeature.CREATE_EVENT"
                ],
            }
        )
    )


def _calendar_options(
    entry: config_entries.ConfigEntry,
    selected_default: str | None = None,
) -> tuple[str, list[str]]:
    """Return the effective default and allowed calendar options."""
    current = entry.options
    default = selected_default or current.get(
        CONF_CALENDAR_ENTITY,
        entry.data[CONF_CALENDAR_ENTITY],
    )
    allowed = list(
        current.get(
            CONF_CALENDAR_ENTITIES,
            entry.data.get(CONF_CALENDAR_ENTITIES, [entry.data[CONF_CALENDAR_ENTITY]]),
        )
    )
    return default, list(dict.fromkeys((*allowed, default)))


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
            if user_input[CONF_CALENDAR_ENTITY] not in user_input[CONF_CALENDAR_ENTITIES]:
                errors[CONF_CALENDAR_ENTITY] = "default_not_allowed"
            else:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Daylight Calendar Import",
                    data=user_input,
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_AI_TASK_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        filter={
                            "domain": "ai_task",
                            "supported_features": [
                                "ai_task.AITaskEntityFeature.GENERATE_DATA"
                            ],
                        }
                    )
                ),
                vol.Required(CONF_CALENDAR_ENTITY): _calendar_selector(),
                vol.Required(CONF_CALENDAR_ENTITIES): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        multiple=True,
                        filter={
                            "domain": "calendar",
                            "supported_features": [
                                "calendar.CalendarEntityFeature.CREATE_EVENT"
                            ],
                        },
                    )
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)



class DaylightCalendarImportOptionsFlow(config_entries.OptionsFlowWithReload):
    """Configure the default calendar and optional Direct IMAP ingestion."""

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Configure the default calendar and enable or disable email ingestion."""
        current = self.config_entry.options
        default_calendar, allowed_calendars = _calendar_options(self.config_entry)
        if user_input is not None:
            default_calendar, allowed_calendars = _calendar_options(
                self.config_entry,
                user_input[CONF_CALENDAR_ENTITY],
            )
            self._calendar_options = {
                CONF_CALENDAR_ENTITY: default_calendar,
                CONF_CALENDAR_ENTITIES: allowed_calendars,
            }
            if not user_input[CONF_EMAIL_ENABLED]:
                return self.async_create_entry(
                    data={
                        **current,
                        **self._calendar_options,
                        CONF_EMAIL_ENABLED: False,
                    }
                )
            return await self.async_step_email()

        schema = vol.Schema(
            {
                vol.Required(CONF_CALENDAR_ENTITY): _calendar_selector(),
                vol.Required(CONF_EMAIL_ENABLED): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                schema,
                {
                    CONF_CALENDAR_ENTITY: default_calendar,
                    CONF_EMAIL_ENABLED: current.get(CONF_EMAIL_ENABLED, False),
                },
            ),
        )

    async def async_step_email(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Validate and save Direct IMAP connection options."""
        current = self.config_entry.options
        calendar_options = getattr(self, "_calendar_options", None)
        if calendar_options is None:
            default_calendar, allowed_calendars = _calendar_options(self.config_entry)
            calendar_options = {
                CONF_CALENDAR_ENTITY: default_calendar,
                CONF_CALENDAR_ENTITIES: allowed_calendars,
            }
        errors: dict[str, str] = {}
        if user_input is not None:
            port = user_input.get(CONF_EMAIL_PORT)
            if isinstance(port, float) and port.is_integer():
                user_input = {
                    **user_input,
                    CONF_EMAIL_PORT: int(port),
                }
            password = (
                user_input.get(CONF_EMAIL_PASSWORD)
                or current.get(CONF_EMAIL_PASSWORD, "")
            )
            options = {
                **current,
                **calendar_options,
                CONF_EMAIL_ENABLED: True,
                **user_input,
                CONF_EMAIL_PASSWORD: password,
            }
            try:
                settings = direct_imap_settings_from_options(
                    self.config_entry.entry_id,
                    options,
                )
                await DirectImapSource(settings).async_validate()
            except DirectImapAuthenticationError:
                errors["base"] = "invalid_auth"
            except DirectImapMailboxError:
                errors["base"] = "invalid_mailbox"
            except ValueError:
                errors["base"] = "invalid_email_config"
            except DirectImapError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(data=options)

        suggested_values = (
            user_input
            if user_input is not None
            else {
                key: current[key]
                for key in _EMAIL_OPTION_KEYS
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
