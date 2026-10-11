# Notification persistence

Notification requests have an independent journal inside the existing private
pending-import storage envelope. This allows lifecycle producers to commit a
notification identity and the corresponding outcome in one Home Assistant
Store save. Journal entries are independent of pending review decisions and
bounded activity history; deleting a reviewed import does not delete its request.

Records contain only import ID, event ID, transition type and timestamp. Safe
messages are regenerated from the notification contract rather than persisting
source content or provider error text. The canonical transition hash identifies
a request; concurrent enqueue calls and reloads do not insert the same request
again. Old storage files without the journal load with an empty journal, with no
historical notification replay. Invalid journal identities fail loading rather
than silently dropping durable requests.

The foundation does not enqueue existing lifecycle outcomes, send notifications,
or retry delivery. Those changes are separate PRs. Journal entries are currently
retained; retention is a separate v0.7 reliability work item. External Home
Assistant notify delivery will be at-least-once, never exactly-once: a crash
between service completion and persisted delivery state can repeat a submission.
