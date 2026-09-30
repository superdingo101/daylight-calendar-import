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
The normal panel edits title, ISO start/end, all-day, location, and description.
Timed values must include an explicit UTC offset; all-day values are dates with
an exclusive end. The panel sends no destination-calendar override, so existing
legacy event destinations are preserved. Long descriptions and meeting details
are sent without frontend truncation.
`edit_pending_event` accepts an optional `expected_event` containing the event
snapshot from `get_pending`. If the event changed before the edit acquires the
store lock, the action fails and the client must refresh instead of overwriting it.
`get_pending_event` returns one event by `pending_id` and `event_id`.
The panel's approve/reject controls require explicit confirmation and submit
the complete loaded `expected_event`. The backend compares it under the store
lock before any calendar write or rejection; stale decisions fail with a refresh
message. Existing action callers may omit the snapshot for compatibility.

Existing stored imports without source metadata load as `manual_text` with no
source title, warnings, or skipped duplicate count. Upload bytes, temporary
media paths, upstream source IDs, and source fingerprints are not exposed by
the read actions.

`list_activity` returns the most recent 500 import summaries, newest first,
without source text or transition detail. `get_activity` accepts `pending_id`
and returns the same summary with its latest 32 transitions. These reads share
the authenticated control check used by pending reads. Each transition has a
type, timestamp, and optional event ID. Review ready, reject, write checkpoint,
calendar creation, uncertain write, and explicit uncertainty resolutions are
stored with the corresponding pending or deduplication update. A write-started
checkpoint reports an uncertain current status until creation commits. Completed
imports remain in activity after leaving the pending queue; older entries and
transitions are pruned independently of deduplication history. Existing storage
without activity loads with an empty history.
Summaries include created and rejected event counts. When both outcomes occur,
the completed import has `mixed` status regardless of decision order. Reads
return copies so callers cannot mutate the stored ledger.
Active import history is retained even when completed records are pruned; the
500-record bound applies to completed history while active imports occupy
additional slots as needed. Active titles track the next pending event.
Editing the leading pending draft refreshes that title in the same storage
transaction without adding a new lifecycle transition.
