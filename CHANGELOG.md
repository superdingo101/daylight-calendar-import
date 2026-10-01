# Changelog

## 0.4.0

- Complete the Home Assistant review panel with event editing, individual and bulk approve/reject, per-event results, and explicit uncertain-write recovery.
- Show recent lifecycle activity, durable outcome counts, bounded transition history, completed imports, and failed parser submissions.
- The review panel sends complete event snapshots so stale edits, decisions, and recovery are rejected, including repeated uncertain write attempts. Existing action callers may omit a snapshot for compatibility.
- Adapt the panel to mobile-sized screens and keyboard navigation.
- Keep the configured write calendar visible in the normal workflow without offering new per-event routing. Existing saved event destinations remain honored.
- Publish the hosted parser API v1 contract in `docs/hosted-api.md`, executable schemas under `schemas/hosted/v1/`, and contract tests for future clients and services.

### Upgrade from 0.3.0

Restart Home Assistant after updating the integration. Pending imports, existing event destinations, deduplication history, and configuration remain in the same storage. Lifecycle history begins with activity recorded after the update; older completed imports cannot be reconstructed. Pre-upgrade pending imports acquire history on their next approval, rejection, or recovery; editing alone does not create a transition. The first such transition is saved atomically in the same storage transaction, even when that action completes the import, without a separate migration step. Verify any previously uncertain calendar write against the actual calendar before choosing created, not created, or discard. The review panel requires an authenticated account with control permission for the configured AI Task and writable calendars.

## 0.3.0

- Normalize text, image, and PDF inputs as source documents and parse them through a provider boundary with explicit media capabilities.
- Accept uploaded PNG, JPEG, and WebP images with optional context; validate size and type, then clean up uploads and temporary media.
- Extract text from bounded PDF files in a resource-limited worker; send scanned or visual pages as temporary attachments when the AI Task entity supports them.
- Keep valid event drafts when individual AI candidates are malformed, reporting indexed warnings for the skipped candidates.
- Preserve text and attachment digests together in pending review without retaining upload bytes or temporary media references.

## 0.2.0

- Persist stable per-event IDs, statuses, and calendar destinations with migration of existing configuration and pending storage.
- List, inspect, edit, approve, or reject individual pending events; retain batch approval and rejection with per-event write checkpoints.
- Resolve uncertain calendar writes explicitly as created, not created, or discarded without automatic duplicate retries.
- Preserve source and event deduplication through edits and per-event decisions.
- Keep valid drafts when an AI response contains malformed siblings, and return indexed warnings.
- Maintain 100% branch coverage across current and minimum supported Home Assistant versions, plus the mutation-score gate.
