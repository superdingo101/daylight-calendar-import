# Review backend contract

The Home Assistant review UI uses the existing `daylight_calendar_import` actions.
`list_pending`, `get_pending`, and `get_pending_event` require an authenticated
user with control permission for the configured AI Task and allowed calendars.
Mutations retain the existing per-calendar permission checks and durable
calendar-write checkpoints. The UI must show backend errors and refresh after
each mutation; it must not silently overwrite stale state.

`list_pending` returns `imports`. Each summary has `id`, `created_at`,
`event_count`, `title` (the first event title), `approval_in_flight`,
`source_kind` (`manual_text`, `image`, or `pdf`), `source_title` (nullable),
`warnings` (strings), and `duplicate_events` (a count of exact candidates
skipped when the import was created). It does not return source text.

`get_pending` returns the pending source text and event drafts, plus the same
source metadata and warnings. Its events have stable IDs, statuses, destination
calendars, and editable draft fields. A write-uncertain event must be resolved
with `resolve_pending_event` before it can be edited, rejected, or retried.
It also returns `allowed_calendars` and `default_calendar` for the editor.
`edit_pending_event` accepts an optional `expected_event` containing the event
snapshot from `get_pending`. If the event changed before the edit acquires the
store lock, the action fails and the client must refresh instead of overwriting it.
`get_pending_event` returns one event by `pending_id` and `event_id`.

Existing stored imports without source metadata load as `manual_text` with no
source title, warnings, or skipped duplicate count. Upload bytes, temporary
media paths, upstream source IDs, and source fingerprints are not exposed by
the read actions.
