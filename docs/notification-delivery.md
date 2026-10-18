# Notification persistence

Notification requests have an independent journal inside the existing private
pending-import storage envelope. This allows lifecycle producers to commit a
notification identity and the corresponding outcome in one Home Assistant
Store save. Journal entries are independent of pending review decisions and
bounded activity history; deleting a reviewed import does not delete its request.
Terminal delivery failures also retain a generic activity warning outside the
bounded transition history, so later event decisions cannot erase the warning.

Records contain import ID, event ID, transition type and timestamp, plus the
notify entity ID, delivery status, attempt count, next retry timestamp and a
fixed failure code, optional conflict revision digest, and terminal-completion
timestamp (`terminal_at`). Notify entity IDs can identify a person or device.
Completed requests follow the [retention policy](retention.md); pending requests
and identities needed by unresolved work remain in private HA storage.
Safe messages are regenerated from the notification contract rather than persisting
source content or provider error text. The canonical transition hash identifies
a request; concurrent enqueue calls and reloads do not insert the same request
again. Old storage files without the journal load with an empty journal, with no
historical notification replay. Invalid journal identities fail loading rather
than silently dropping durable requests. Corruption preserves the original storage and
surfaces [repair guidance](storage-recovery.md) before new work starts.

External HA notify delivery is at-least-once, never exactly-once: a
crash between service completion and persisted delivery state can repeat a
submission.

## Durable delivery

Review-ready requests are now appended atomically with newly accepted imports
when the current policy explicitly permits them. Existing activity is never
replayed after consent is enabled. Each request binds to the selected notify
entity; a changed target or revoked category cancels pending delivery rather
than forwarding older requests to a new destination.

A config-entry-owned worker resumes pending requests at startup and checks them
every minute. It durably consumes an attempt before calling HA, allows at most
three attempts with a one-minute retry delay, and imposes a 15-second deadline.
A completed HA service call records `sent`, which means submission rather than
receipt on a device. Failed/exhausted delivery records contain fixed safe codes. Terminal failures
also emit a generic warning in the HA log without identifiers or provider text.
Interrupted last attempts fail closed without a fourth submission.

Provider tasks that ignore cancellation stay tracked across reloads and removed
entries. The same identity cannot be retried while its previous provider task is
still running, and aggregate task capacity is bounded. Entry teardown waits for
journal persistence to finish before a new snapshot may load; provider
cancellation itself is bounded. A crash after HA accepts a call but before its
result is saved can repeat that notification. Calendar lifecycle truth is never
changed by delivery failures.

## Calendar-created outcomes

The event-added category is independently opt-in. Its request is committed in
the same envelope as a confirmed calendar-created outcome, including an
explicit user confirmation that an uncertain write did create the event.
Write-started checkpoints, ambiguous provider exceptions, and calendar calls
whose final outcome cannot be saved never produce an event-added notification.
The delivered message contains no event title, source content or calendar name.

## Calendar failure and uncertain writes

Calendar writes that cannot pass preflight are known not to have started. Their
failure requests are saved with the failure transition, while the event stays
reviewable. Exceptions after a write checkpoint remain uncertain, never a
confirmed failure. A successful external call whose final outcome cannot be
saved also uses the existing durable uncertain checkpoint.

Uncertainty requests retain the write-attempt ID and a recovery flag in addition
to the fields listed above; these fields follow the same completed-history
retention policy, while unresolved checkpoints protect their request identities. New attempt IDs distinguish uncertainty identities even if timestamps
repeat. On startup, unresolved event checkpoints produce at most one alert per
attempt, including legacy checkpoints with an unknown attempt ID. Recovery does
not rely on prunable activity history or modify the user's review decision.


### Source-processing failures

The `parse_failed` opt-in covers durable source failures, including parser errors,
terminal attachment/source failures, and processing interrupted by a restart.
The existing `failed` lifecycle transition maps to this category. Failure and
notification request commit in the same private storage save; a failed save does
not publish either. Interrupted recovery also completes persistence and in-memory
publication before caller cancellation propagates. Repeated cleanup or reload
does not create another request for the same failure. Enabling the category does
not replay historical failures. Delivery uses a generic message without source
text, source titles, attachment details, or provider errors. Processing status
and source retry/deduplication rules remain authoritative and unchanged.


### Conflict observations

The `conflict_detected` opt-in uses the permission-checked calendar observation,
triggered automatically before approval confirmation in the review panel or
manually with **Check calendars**. Exact duplicates, possible duplicates and scheduling overlaps
are actionable review results. Empty or failed observations and obsolete drafts
produce no request. This adds no calendar polling and changes no calendar writes,
approval rules or pending lifecycle status. Approval preflight keeps its existing
failure/uncertainty notifications; it does not create conflict requests.

One request is retained per pending event revision and observed calendar scope,
across repeated/concurrent checks, delivery retries and reloads. A revision is a
SHA-256 digest of the exact pending event snapshot and sorted calendar scope;
only the digest (`conflict_revision`) is retained in the notification journal.
Edited drafts or changed scopes can alert again. Returning to an already alerted
revision stays silent, and clear checks do not reset this identity. No observed
calendar titles, times or event details enter the request or generic message.
The digest shares the journal's private retention policy; unresolved imports
protect their request identities even after delivery.
Legacy requests without the field retain their original identities.

The store revalidates the observed snapshot under its lock before saving the
request. Notification save errors leave the journal unchanged, log a generic
warning and preserve the calendar-check response. Cancellation still finishes
notification persistence before propagating. Notifications remain off by default.
