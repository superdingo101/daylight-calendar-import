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
    CONF_EMAIL_MARK_SEEN,
    CONF_EMAIL_PASSWORD,
    CONF_EMAIL_PORT,
    CONF_EMAIL_SENDER_ALLOWLIST,
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


class DaylightCalendarImportConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure Daylight Calendar Import."""

    VERSION = 2

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Create the Direct IMAP options flow."""
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
                vol.Required(CONF_CALENDAR_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        filter={
                            "domain": "calendar",
                            "supported_features": [
                                "calendar.CalendarEntityFeature.CREATE_EVENT"
                            ],
                        }
                    )
                ),
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
    """Configure optional self-hosted Direct IMAP ingestion."""

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Enable or disable email ingestion."""
        current = self.config_entry.options
        if user_input is not None:
            if not user_input[CONF_EMAIL_ENABLED]:
                return self.async_create_entry(
                    data={CONF_EMAIL_ENABLED: False}
                )
            return await self.async_step_email()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_EMAIL_ENABLED,
                    default=current.get(CONF_EMAIL_ENABLED, False),
                ): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
        )

    async def async_step_email(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Validate and save Direct IMAP connection options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            options = {
                CONF_EMAIL_ENABLED: True,
                **user_input,
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

        current = self.config_entry.options
        suggested_values = user_input if user_input is not None else current
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
                vol.Required(CONF_EMAIL_PASSWORD): selector.TextSelector(
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
                vol.Optional(
                    CONF_EMAIL_SENDER_ALLOWLIST,
                    default="",
                ): selector.TextSelector(
                    selector.TextSelectorConfig(multiline=True)
                ),
                vol.Required(
                    CONF_EMAIL_MARK_SEEN,
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
