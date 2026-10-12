# Local retention

Daylight applies conservative retention to the private pending-import storage
envelope at startup and during every successful storage transaction. An idle
integration makes no periodic cleanup writes; old completed history disappears
on the next transaction or reload. Cleanup uses the same atomic Home Assistant
Store save as lifecycle and notification changes. Failed saves leave the prior
in-memory and durable snapshot intact; cancellation waits for the retention
transaction to finish before propagating.

| Stored data | Retention |
| --- | --- |
| Completed lifecycle summaries and source metadata | Pruned after 30 days from the last lifecycle transition or beyond the newest 500 completed summaries |
| Transitions in an import summary | Latest 32 transitions, as before |
| Terminal notification requests | Pruned after 30 days from terminal completion or beyond the newest 1,000 completed requests by completion time |
| Pending imports, including uncertain calendar writes | Until the user resolves them; excluded from completed-history limits |
| Live discovered/processing source checkpoints and source claims awaiting release | Until their existing lifecycle resolves them; excluded from completed-history limits |
| Notification requests with a still-running provider task | Until that task finishes, even if delivery was recorded as terminal |
| Pending notification requests, including retries | Until delivery reaches a terminal state under existing retry/admission rules; excluded from completed-history limits |
| Source/event deduplication fingerprints | Existing newest 10,000 fingerprints in each history; no new time expiry |

All lifecycle summaries and notification identities belonging to unresolved
imports, live/releasing source claims, pending deliveries, or still-running
provider submissions are protected,
including already-sent or failed requests. Protection prevents cleanup from
manufacturing another alert for the same unresolved uncertainty checkpoint or
previously observed conflict revision. Protected data can exceed the completed
history count limits; retention never discards active work to meet a quota.
Once the work resolves, existing age and count limits apply on the next save.

Notification records retain a terminal-completion timestamp (`terminal_at`)
alongside their existing private fields. A request pending for longer than 30
days receives a full retention window when it finally completes. Legacy terminal
records without that timestamp receive one at their first successful retention
save; their notification identities do not change. Later terminal bookkeeping
does not extend the completion window. Unparseable or timezone-free legacy
activity timestamps do not authorize age deletion; the completed-summary count
limit still applies.

Activity summaries can include source subject/title, an event title, status,
counts and transition coordinates. They exclude source bodies and attachment
bytes. Pending review can retain sensitive source context and parsed drafts
until the user resolves the import. Notification identities, target entity IDs,
retry state, fixed failure codes and digests remain private and follow the limits
above. This policy changes no external provider or mailbox retention, performs
no reprocessing, and adds no manual reset or source archive. Deduplication and
transport acknowledgement lifecycles remain independent of display history.
