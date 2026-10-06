# Native settings API

The Daylight panel uses an integration-owned WebSocket API for configuration.
The commands are registered once at integration setup so they remain available
while the config entry reloads after a settings change.

All settings commands are **administrator-only**.

## Read settings

`daylight_calendar_import/settings/get`

Request:

```json
{
  "type": "daylight_calendar_import/settings/get"
}
```

Because Daylight currently permits one config entry, the panel read command discovers
that sole entry automatically. The response includes its `entry_id` for subsequent
update commands, plus the effective AI Task entity, default calendar, writable
calendar list, and Direct IMAP settings. The saved IMAP password is never
returned. The response exposes only `password_configured: true|false`.

## Update AI and calendar settings

`daylight_calendar_import/settings/core/update`

The request contains `entry_id`, `ai_task_entity`, `calendar_entity`, and
`calendar_entities`. The default calendar must be present in the writable
calendar list. Saving updates config-entry options and reloads the entry.

## Update Direct IMAP settings

`daylight_calendar_import/settings/email/update`

The request contains `entry_id` and `enabled`. When enabled, the request may
also contain `host`, `port`, `username`, `password`, `mailbox`, and
`verify_ssl`.

A blank or omitted password preserves the existing stored password. Enabling or
changing Direct IMAP validates the connection before settings are persisted.
Disabling Direct IMAP does not destroy the saved connection fields.

Validation failures use stable WebSocket error codes:

- `invalid_auth`
- `invalid_mailbox`
- `invalid_email_config`
- `cannot_connect`
- `default_not_allowed`
- `invalid_ai_task`
- `invalid_calendar`
- `entry_not_found`

The API intentionally keeps config-entry options as the source of truth. The
existing Home Assistant Configure/options flow and the Daylight panel therefore
share the same settings model and validation helpers instead of maintaining
parallel configuration stores.
