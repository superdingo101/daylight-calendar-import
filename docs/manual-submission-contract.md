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

Manual submissions may explicitly set `reimport_duplicates: true` (default `false`) to queue previously seen **event drafts** for human review again. The image/PDF and pasted-text interfaces offer **Re-import for review** when no new events remain after deduplication. This deliberately sends a fresh source ID; repeated submissions using an already-handled `source_id` still return `duplicate_source` for idempotency, even with the flag. Duplicates *within the same submitted batch* are still filtered. Historical fingerprints and review decisions remain intact, and approval-time exact calendar duplicate checks are **not** bypassed. This override is only available on manual submissions, not IMAP polling.

Text must contain non-whitespace content. Optional attachment context may be empty. Existing source-ID and Home Assistant upload-ID validation remain in force. AI Task control permission is checked before source deduplication or upload access; the normal pending-review read and calendar-write permissions apply separately.

Overlapping requests with the same source ID are rejected after authorization and before parsing or upload access. The rejected retry creates no new activity record and leaves the original request processing. Wait and check pending imports and activity before explicitly retrying; a different source ID may be processed independently.

Validation, provider, authorization, and storage errors remain Home Assistant service errors. A rejected or lost request must not be automatically resubmitted: refresh pending imports and activity first, because processing may have completed despite the client losing its response. An empty extraction is recorded as `no_events`, rather than a parsing failure. Manual no-event source IDs remain retryable so corrected text or provider settings can be submitted again.
