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
calendars, editable draft fields and, when the parser reports temporal context,
a bounded `date_time_assumptions` list belonging to that specific event.
Those disclosures survive deduplication and persistence, and are cleared if
the event's start/end or all-day state is edited. Older events have no
assumption list. This optional, advisory-only field is deliberately excluded
from optimistic-concurrency event comparison: older API clients may omit it
from `expected_event` without falsely invalidating an otherwise unchanged
review snapshot. A write-uncertain event must be resolved
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

### Explicit calendar routing confirmation

An unknown or contradictory `Calendar:` directive retains the configured
fallback destination for display, but the newly queued events carry a
`routing_unresolved: true` flag. They **cannot** be approved with either
`approve_pending_event` or `approve_pending` until each destination is
explicitly selected in the event editor. Bulk approval checks every event
before any calendar side effect, so a mixed-resolution batch fails as a unit.

The editor renders an unselected calendar placeholder for unresolved events.
Saving unrelated draft edits is insufficient: the reviewer must select a
writable destination, including when confirming the displayed fallback. The
`edit_pending_event` action saves the selected `calendar_entity` in the same
durable transaction that clears the unresolved flag. API callers must send a
non-null allowed calendar explicitly to confirm; omitting `calendar_entity`
leaves the flag set. A stale-state `expected_event` snapshot from
`get_pending` must retain `routing_unresolved` when present. For storage created before this field existed, Daylight recognizes the two
historical unresolved/conflicting routing warnings on the parent import and
migrates its unmarked events to require explicit confirmation. Legacy imports
without either warning continue to default to resolved. A deliberate calendar
confirmation is persisted as `routing_unresolved: false` so repeated restarts
cannot re-flag a confirmed event merely because the parent warning remains.

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
and the independently selected conflict-observation calendars. The action uses the **same reviewer access check** as `get_pending_event`:
the caller must have Home Assistant `POLICY_CONTROL` on the configured AI
Task and **every calendar in the integration's allowed writable list**.
Additionally, the observer requires `POLICY_READ` on the destination and each
configured conflict calendar. Read-only access to observation calendars alone
does not grant access to the action; the existing reviewer permissions are
intentionally unchanged. Observing calendars never performs a write or grants
new permissions.

A successful response contains `observed_calendars` and a `matches` list
of bounded entries with `kind` (`exact_duplicate`, `possible_duplicate`, or
`conflict`), `calendar_entity` and `existing_title`. The response does not
include event descriptions, attendees, or meeting credentials.

To prevent expensive or misleading partial observations, the action rejects
drafts spanning more than 90 elapsed days, scopes exceeding 16 distinct
calendars (up to 15 configured conflict calendars plus the selected destination), provider responses containing more than 500 events combined, or provider
event titles longer than 512 characters.
These limits raise explicit incomplete-observation errors; matching results
are never silently truncated. Provider calls use the bounded draft window,
although providers may themselves allocate an over-limit response before
Daylight validates its size.

The read is a *point-in-time advisory check*: results can change before
approval. The action accepts an optional `expected_event` from
`get_pending_event` to reject an already-stale review snapshot, including
`write_uncertain` events with their original write-attempt identifiers for
read-only inspection. This does not permit approval without explicit
uncertain-write recovery. The action always
rechecks the event after the asynchronous provider reads. If the draft,
destination, status or identity changed—or it was approved/rejected—while
observation was in progress, the action fails with a refresh error instead of
returning matches for the old draft. Calendar access failures are explicit errors, **not** a signal that
the schedule is empty. This API does not enforce a write-time duplicate guard. The review
panel uses these results to display an optional, on-demand duplicate/conflict
check; the results are advisory, not a condition for approval. Write-time
duplicate protection is a separate scoped change.
