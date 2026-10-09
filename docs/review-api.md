# Review backend contract

The Home Assistant review UI uses the existing `daylight_calendar_import` actions.
`list_pending`, `get_pending`, and `get_pending_event` require an authenticated
user with control permission for the configured AI Task and allowed calendars.
Mutations retain the existing per-calendar permission checks and durable
calendar-write checkpoints. The UI must show backend errors and refresh after
each mutation; it must not silently overwrite stale state.

`list_pending` returns `imports`. Each summary has `id`, `created_at`,
`event_count`, `title` (the first event title), `approval_in_flight`,
`source_kind` (`manual_text`, `image`, `pdf`, or `email`), `source_title` (nullable),
`warnings` (strings), and `duplicate_events` (a count of exact candidates
skipped when the import was created). It does not return source text.

`get_pending` returns the pending source text and event drafts, plus the same
source metadata and warnings. Email imports may additionally include `source_sender`,
the normalized first `From` header; the field is omitted when no sender was available.
Its events have stable IDs, statuses, destination
calendars, and editable draft fields. A write-uncertain event must be resolved
with `resolve_pending_event` before it can be edited, rejected, or retried.
Recovery controls send the loaded `expected_event` snapshot. The backend
compares it under the storage lock so an older calendar check cannot resolve
a newer uncertain attempt with the same event ID. Each write checkpoint stores
a new `write_attempt` token; unchanged retries therefore remain distinguishable.
Existing callers may omit
the snapshot for compatibility.
It also returns `allowed_calendars` and `default_calendar` for the editor.
The normal panel edits title, ISO start/end, all-day, location, description, and
destination calendar. Timed values must include an explicit UTC offset; all-day
values are dates with an exclusive end. The calendar selector is limited to
`allowed_calendars` and preselects the event's saved destination, falling back
to `default_calendar` when the event has no explicit destination. On save, the
panel sends the selected destination as top-level `calendar_entity` alongside
the complete event draft and stale-state snapshot. Existing event destinations
therefore remain unchanged unless the user selects a different allowed calendar.
Long descriptions and meeting details are sent without frontend truncation.
`edit_pending_event` accepts optional top-level `calendar_entity` plus an optional
`expected_event` containing the event snapshot from `get_pending`. If the event changed before the edit acquires the
store lock, the action fails and the client must refresh instead of overwriting it.
`get_pending_event` returns one event by `pending_id` and `event_id`.
The panel's approve/reject controls require explicit confirmation and submit
the complete loaded `expected_event`. The backend compares it under the store
lock before any calendar write or rejection; stale decisions fail with a refresh
message. Existing action callers may omit the snapshot for compatibility.

Existing stored imports without source metadata load as `manual_text` with no
source title or sender, warnings, or skipped duplicate count. Upload bytes, temporary
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

Submissions checkpoint a source summary with received and processing timestamps
before invoking AI. Successful drafts transition to awaiting review in the
same transaction as pending storage; empty or duplicate results receive a
terminal status. A parser failure leaves a bounded `failed` activity record
with generic retry guidance. Empty results with an upstream source ID remain
retryable with that same ID. Canceled parsing records a failed status; an entry
reload waits for live submissions to finish before replacing their store. If
Home Assistant restarts during parsing, the unfinished processing record
becomes failed with interruption guidance on load. In-flight parsing records
are retained even when the completed-history limit is reached.
No submitted text, attachment, or upstream source identifier is stored in
these summaries. A submission that never reached parsing (for example, an
invalid upload) still returns its action error directly.


## Automations: pending imports available for review

The integration fires the Home Assistant event `daylight_calendar_import_pending_added`
after an event-bearing import has been durably committed to the review queue.

Example trigger:

```yaml
triggers:
  - trigger: event
    event_type: daylight_calendar_import_pending_added
```

The payload contains only `pending_id` (an opaque review identifier) and
`event_count` (number of event drafts accepted into this import). No email
content, sender, title, description, attachment metadata, or credentials
are included. Other Home Assistant automations can respond to this signal
and fetch review information through the authenticated Daylight review API.

This event is a **best-effort notification**, not a durable delivery
guarantee: an abrupt shutdown between store commit and bus publication can
lose a notification, and events are not replayed when Home Assistant
restarts. The pending queue and its sensors remain the authoritative state.
Duplicate/empty submissions do not publish a new pending-added event.


## On-demand existing-calendar checks (v0.6)

The response-enabled `daylight_calendar_import.check_pending_event` action accepts
`pending_id` and `event_id`. It observes the event's configured destination
and the independently selected conflict-observation calendars. The requested
user must be authorized to view these calendars; observation never confers
write permissions.

A successful response contains `observed_calendars` and a `matches` list
of bounded entries with `kind` (`exact_duplicate`, `possible_duplicate`, or
`conflict`), `calendar_entity` and `existing_title`. The response does not
include event descriptions, attendees, or meeting credentials.

The read is a *point-in-time advisory check*: results can change before
approval. Calendar access failures are explicit errors, **not** a signal that
the schedule is empty. This API does not yet enforce a write-time duplicate
guard or add conflict UI; those are separate scoped changes.
