# Targeted deduplication reset

`daylight_calendar_import.reset_deduplication` explicitly removes one handled
fingerprint. Supply **exactly one** of:

- `source_id`: the original stable upstream identifier used for submission;
- `event`: a complete validated event draft (title, start, end, all_day,
  confidence, and optional location/description).

The action derives the fingerprint using the existing normalization rules.
Equivalent title case/whitespace and UTC-equivalent times address the same event;
confidence is not part of its identity. Description and location are part of it.
It returns `reset: true` when the selected handled identity was removed, or
`reset: false` if that identity was absent. It does not expose the dedupe history
or persist the raw source identifier supplied with the action.

An authenticated user needs control access to the AI Task and every allowed
calendar, matching access to private pending records. Pending imports, uncertain
calendar writes, active source claims, and calendar writes currently in flight
cannot have their identities reset. Resolve or finish that work first. Other
fingerprints, notifications, lifecycle history, and pending drafts stay intact.
Removal is atomic and survives restart; storage failures preserve the original
history. Once the storage transaction starts, cancellation waits for it to finish.

Source and event identities are independent. Resetting a source allows another
parser submission, while previously handled event fingerprints still prevent
those events from reappearing. Resetting an event leaves its source identity
handled; a submission using that same source ID is still skipped. Intentionally
reconsidering both requires two explicit targeted resets. No reset creates,
deletes, or retries a calendar write, and calendar approval retains its fresh
exact-duplicate preflight. No global history-clearing action is added.

```yaml
action: daylight_calendar_import.reset_deduplication
data:
  source_id: "the-original-source-identifier"
response_variable: reset_result
```

```yaml
action: daylight_calendar_import.reset_deduplication
data:
  event:
    title: "Practice"
    start: "2026-10-08T17:30:00-07:00"
    end: "2026-10-08T18:30:00-07:00"
    all_day: false
    confidence: 0.9
    location: "Park"
    description: "Bring water"
response_variable: reset_result
```
