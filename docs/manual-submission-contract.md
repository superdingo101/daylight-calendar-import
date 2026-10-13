# Manual submission service contract

`submit_text`, `submit_image`, and `submit_pdf` use the existing durable pending-review pipeline. They return the same response fields and never create calendar events. Calendar creation requires a later approval.

| Field | Meaning |
| --- | --- |
| `status` | `pending_review` when new drafts are available, `duplicate` when all submitted material was already handled, or `no_events` when extraction succeeded without reviewable drafts. |
| `activity_id` | ID for the durable activity record created for this attempt. `null` when a known source ID was skipped before parsing and no new record was created. |
| `pending` | Pending import including its ID and drafts, or `null`. |
| `duplicate` | Whether any source or event was deduplicated. Can be true alongside `pending_review` when some new drafts remain. |
| `duplicate_source` | Whether the stable upstream source ID was already handled. |
| `duplicate_events` | Number of event drafts skipped as duplicates. |
| `warnings` | Extraction and routing warnings, including warnings requiring calendar confirmation. |

Text must contain non-whitespace content. Optional attachment context may be empty. Existing source-ID and Home Assistant upload-ID validation remain in force. AI Task control permission is checked before source deduplication or upload access; the normal pending-review read and calendar-write permissions apply separately.

Validation, provider, authorization, and storage errors remain Home Assistant service errors. A rejected or lost request must not be automatically resubmitted: refresh pending imports and activity first, because processing may have completed despite the client losing its response. An empty extraction is recorded as `no_events`, rather than a parsing failure. Manual no-event source IDs remain retryable so corrected text or provider settings can be submitted again.
