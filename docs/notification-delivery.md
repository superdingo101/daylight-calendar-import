# Notification persistence

Notification requests have an independent journal inside the existing private
pending-import storage envelope. This allows lifecycle producers to commit a
notification identity and the corresponding outcome in one Home Assistant
Store save. Journal entries are independent of pending review decisions and
bounded activity history; deleting a reviewed import does not delete its request.

Records contain import ID, event ID, transition type and timestamp, plus the
notify entity ID, delivery status, attempt count, next retry timestamp and a
fixed failure code. Notify entity IDs can identify a person or device. These
fields, including terminal requests, are currently retained indefinitely in
private HA storage; the separate retention work item will bound that history.
Safe messages are regenerated from the notification contract rather than persisting
source content or provider error text. The canonical transition hash identifies
a request; concurrent enqueue calls and reloads do not insert the same request
again. Old storage files without the journal load with an empty journal, with no
historical notification replay. Invalid journal identities fail loading rather
than silently dropping durable requests.

Journal entries are currently retained; retention is a separate v0.7 reliability
work item. External HA notify delivery is at-least-once, never exactly-once: a
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
