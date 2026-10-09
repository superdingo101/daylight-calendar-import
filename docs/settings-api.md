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
calendar list, normalized `calendar_aliases` mapping, independent
`conflict_calendar_entities` list, and Direct IMAP settings. The saved IMAP password is never
returned. The response exposes only `password_configured: true|false`.

## Update AI and calendar settings

`daylight_calendar_import/settings/core/update`

The request contains `entry_id` plus at least one core setting. AI Task and
calendar settings can be patched independently, so a General-settings save does
not overwrite a newer Calendars-settings save (and vice versa). When calendars
are supplied, the effective default calendar must remain present in the
effective writable-calendar list.

Saving merges the validated patch into the latest config-entry options and then
reloads the entry.

## Update calendar intelligence settings

`daylight_calendar_import/settings/calendar_intelligence/update`

This administrator-only command accepts `entry_id` and one or both of
`calendar_aliases` (an alias-to-calendar-entity mapping) and
`conflict_calendar_entities` (a list of calendar entity IDs, possibly empty).
Aliases are exact after whitespace, Unicode NFKC and case normalization.
Alias names must be nonempty, no more than 64 normalized characters,
unique after normalization, and must target a configured writable calendar.
Conflict-observation calendars have **no write authorization**.
Select at most 15 conflict calendars: the pending event's writable destination
is always observed in addition, for a maximum scope of 16. Older settings
with more selected calendars remain readable and can be reduced in Settings.

Use `calendar_aliases: {}` to clear aliases. When removing a writable calendar
used by an alias, remove/remap that alias first in a separate settings request.

**Pending-event destination changes:** configuration never silently
retargets pending events. Removing a writable calendar used by any pending
event fails with `pending_destination_not_allowed`; choose another destination
in review (or reject the event) before retrying. Changing the default when a
legacy pending event uses the implicit default fails with
`pending_default_would_change`. The queue is checked while holding the store
transaction lock, so editing a pending event cannot race the validation.

A scope change fails closed with `pending_store_unavailable` if Daylight is not
loaded and its durable queue therefore cannot be checked. If a submission,
service handler or IMAP poll is running it fails with `calendar_change_busy`
rather than switching calendar settings under active ingestion. On successful
validation, admission of new services and scheduled polls is suspended until
the config entry finishes reloading successfully. If reloading fails after the new
options are persisted, the old runtime remains closed until restart rather
than ingesting events under stale calendar configuration. AI-only and
unchanged-calendar updates are not subject to the queue gate.

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
- `invalid_settings`
- `invalid_aliases`
- `invalid_alias`
- `duplicate_alias`
- `alias_target_not_allowed`
- `invalid_conflict_calendars`
- `pending_destination_not_allowed`
- `pending_default_would_change`
- `pending_store_unavailable`
- `calendar_change_busy`
- `conflict_calendar_limit`
- `reload_failed`

The API intentionally keeps config-entry options as the source of truth. The
existing Home Assistant Configure/options flow and the Daylight panel therefore
share the same settings model, field-scoped patch semantics, persistence helper,
and validation helpers instead of maintaining parallel configuration stores.

All settings mutations are serialized per config entry for the complete
validate/update/reload transaction. The legacy Home Assistant options flow
persists its patch explicitly inside that transaction and completes without
returning a second options payload for Home Assistant to write afterward. This
prevents a stale full-flow snapshot from overwriting a concurrent native
settings change.

In particular, a second Direct IMAP save waits for an in-progress mailbox
validation to finish before it resolves blank or omitted fields such as the
password. This prevents concurrent email saves from combining or restoring stale
connection values that were never validated together. A slow mailbox validation
can briefly delay another Daylight settings save, but it does not block unrelated
Home Assistant runtime work.

If Home Assistant cannot reload the config entry after the options are saved,
the API returns `reload_failed` instead of reporting success. The options remain
persisted, so the user is told to restart Home Assistant before relying on the
new runtime settings.
