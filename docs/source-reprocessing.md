# Safe source reprocessing

`daylight_calendar_import.reprocess_pending` accepts a `pending_id` and returns
`pending`, `added_events`, and `duplicate_events`. Use it explicitly when the AI
configuration has changed or another extraction might find missing events.

Only a manual-text import that is still entirely awaiting review has its genuine
original source available. Email records retain private source summaries, not the
original message; uploaded images/PDFs have already been deleted. These sources,
completed imports, and imports with handled or uncertain events cannot be
reprocessed. Resubmit unavailable source material instead. Failed submissions
retain no raw source and must also be resubmitted.

The action requires an authenticated user with control access to the configured
AI Task and every allowed calendar, matching access to private pending details.
It sends retained text through the current parser and routing policy, using the
original received timestamp to resolve relative dates. It never creates calendar
events automatically.

Existing drafts, edits, destinations, stable IDs and source/event dedupe history
remain intact. Only newly extracted events that are absent from both pending and
handled event fingerprints are appended to the same import. New drafts follow
current routing policy and still require review. Empty or duplicate-only results
preserve existing drafts. The action does not reset fingerprints or retry email
polling. Original source text remains retained only as long as its pending import.

Concurrent reprocessing of the same import is refused. Unrelated operations stay
available while the parser runs. If review changes during parsing, the result is
refused instead of overwriting user work. Provider failures and cancellation
before persistence leave the original review unchanged. Once persistence starts,
its atomic update finishes before cancellation is propagated.

```yaml
action: daylight_calendar_import.reprocess_pending
data:
  pending_id: "the-pending-import-id"
response_variable: reprocessed
```
