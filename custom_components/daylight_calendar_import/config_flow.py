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

_MAILBOX_PRIVACY_URL = (
    "https://github.com/superdingo101/daylight-calendar-import/"
    "blob/main/docs/direct-imap.md#mailbox-privacy-and-access"
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


def _effective_core_options(
    entry: config_entries.ConfigEntry,
) -> tuple[str, str, list[str]]:
    """Return effective post-setup editable options."""
    current = entry.options
    ai_task_entity = current.get(
        CONF_AI_TASK_ENTITY,
        entry.data[CONF_AI_TASK_ENTITY],
    )
    default_calendar = current.get(
        CONF_CALENDAR_ENTITY,
        entry.data[CONF_CALENDAR_ENTITY],
    )
    allowed_calendars = list(
        current.get(
            CONF_CALENDAR_ENTITIES,
            entry.data.get(
                CONF_CALENDAR_ENTITIES,
                [entry.data[CONF_CALENDAR_ENTITY]],
            ),
        )
    )
    return (
        ai_task_entity,
        default_calendar,
        list(dict.fromkeys((*allowed_calendars, default_calendar))),
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
        ai_task_entity, default_calendar, allowed_calendars = _effective_core_options(
            self.config_entry
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            selected_allowed = list(
                dict.fromkeys(user_input[CONF_CALENDAR_ENTITIES])
            )
            if user_input[CONF_CALENDAR_ENTITY] not in selected_allowed:
                errors[CONF_CALENDAR_ENTITY] = "default_not_allowed"
            else:
                self._pending_core_options = {
                    CONF_AI_TASK_ENTITY: user_input[CONF_AI_TASK_ENTITY],
                    CONF_CALENDAR_ENTITY: user_input[CONF_CALENDAR_ENTITY],
                    CONF_CALENDAR_ENTITIES: selected_allowed,
                }
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
            description_placeholders={"privacy_url": _MAILBOX_PRIVACY_URL},
        )

    async def async_step_email(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Validate and save Direct IMAP connection options."""
        current = self.config_entry.options
        core_options = getattr(self, "_pending_core_options", None)
        if core_options is None:
            ai_task_entity, default_calendar, allowed_calendars = _effective_core_options(
                self.config_entry
            )
            core_options = {
                CONF_AI_TASK_ENTITY: ai_task_entity,
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
                **core_options,
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
