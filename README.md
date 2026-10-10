# Daylight Calendar Import

[![Nightly clean mutation](https://github.com/superdingo101/daylight-calendar-import/actions/workflows/mutation-clean.yml/badge.svg?event=schedule)](https://github.com/superdingo101/daylight-calendar-import/actions/workflows/mutation-clean.yml?query=event%3Aschedule)

The [mutation testing policy](docs/mutation-ci.md) runs cached incremental checks on PRs, nightly clean verification, and an independent clean release gate.

A Home Assistant custom integration that turns unstructured text into validated calendar event drafts using the user's existing **AI Task** provider.

This repository is intentionally separate from [Daylight Calendar Card](https://github.com/superdingo101/daylight-calendar-card). The import integration owns ingestion, parsing, and review; the card may later provide an optional shortcut.

## v0.5.0 review and lifecycle

The integration supports:

- UI configuration of an existing Home Assistant AI Task entity
- UI configuration of a default calendar and an allowed list of writable calendars
- `daylight_calendar_import.parse_text`: parse text and return validated drafts without changing a calendar
- `daylight_calendar_import.import_text`: parse text and create the validated events on the configured calendar
- `daylight_calendar_import.submit_text`: parse text, deduplicate it, and persist only new events for review
- `daylight_calendar_import.submit_image`: upload a PNG, JPEG, or WebP image with optional text, then queue drafts for review
- `daylight_calendar_import.submit_pdf`: upload a PDF with optional context; extract its text layer locally or use an attachment-capable parser
- `daylight_calendar_import.approve_pending`: approve pending events after routing confirmation and a fresh destination-calendar exact-duplicate preflight for each event; a later event can fail after earlier events were created
- `daylight_calendar_import.reject_pending`: remove a pending import without creating calendar events
- `daylight_calendar_import.list_pending`: list pending import IDs, titles, counts, and uncertain-write flags
- `daylight_calendar_import.get_pending`: retrieve a pending import with its source text and event drafts
- `daylight_calendar_import.get_pending_event`: retrieve a draft using its pending import ID and stable event ID
- `daylight_calendar_import.edit_pending_event`: replace a draft or select its allowed calendar before approval, preserving its ID and checking for duplicates
- `daylight_calendar_import.reject_pending_event`: reject one draft while retaining other events in the import
- `daylight_calendar_import.approve_pending_event`: create one draft after routing confirmation and a fresh destination-calendar exact-duplicate check, retaining siblings
- `daylight_calendar_import.resolve_pending_event`: explicitly resolve a calendar write whose outcome is uncertain
- `daylight_calendar_import.list_activity` and `get_activity`: inspect recent lifecycle state and bounded transition history
- multiple events in one input
- timed and all-day events
- strict validation of AI output
- persistent deduplication by optional upstream source ID and normalized event fingerprint

For v0.6, the parser's structured-output request requires an assumptions list
for each returned candidate, including an empty list when dates and times are
explicit. Contextual time resolutions are shown only on their corresponding
pending review events; edits to the event's time/date clear stale assumptions.
This is an AI self-disclosure and **not** independent source-grounded proof that
the parser made no unsupported inference.

The parser instructs the AI not to invent missing event data. Invalid individual events are skipped with indexed `warnings` in parse, import, and submit responses; valid events in the same response remain available. An invalid top-level AI response still fails without creating calendar events. When every event is invalid, no event is imported or queued.

The current minimum supported Home Assistant version is **2026.7.4**. CI tests that version explicitly alongside the current development test environment.


## v0.5.0 Direct IMAP ingestion

v0.5.0 adds optional self-hosted Direct IMAP ingestion. Administrators can configure it from **Daylight imports → Settings → Email** (with the Home Assistant integration **Configure** flow retained as a fallback) and provide an IMAP host, port, username, password/app password, mailbox, TLS verification setting, and (in v0.6) an optional exact sender allowlist. Saved passwords remain server-side; the panel only shows whether a password is configured, and leaving the replacement field blank keeps the existing credential.

**Before enabling it, read [Direct IMAP setup and recovery](docs/direct-imap.md).** The first poll runs immediately and the fixed v0.5 search selects every unread, undeleted message already present in the configured mailbox. Transient failures before a durable local outcome are left unread for retry. Deterministic source-validation failures are recorded as durable failed outcomes so unchanged poison messages do not retry forever; Daylight then attempts to mark them read just like other durable outcomes. An acknowledgement transport failure can leave the upstream read state uncertain while preserving the local result.

The v0.5 Direct IMAP runtime uses a deliberately bounded transport policy: it polls the selected mailbox for unseen, undeleted messages immediately at setup and every five minutes, does not overlap polling cycles, and attempts to mark a message seen only after its source identity is durably handled locally. Normalized messages reuse the existing parser, pending-review store, default calendar, lifecycle history, deduplication, and temporary attachment-staging pipeline. Normalization failures, transient parser/provider failures, storage failures, and acknowledgement failures remain retryable. Deterministic source-validation errors such as empty/unsupported input, empty attachments, and hard size/count limits are terminal for that source identity and are not sent through AI repeatedly. Completed polls summarize retryable failures separately from terminal rejections; unexpected storage or poll exceptions use the generic error path instead.

v0.6 adds an optional exact sender allowlist to Email settings. Non-matching messages are skipped before parsing, remain unread and can be rediscovered on later polls. Matching the `From` header is not sender authentication and does not prevent Daylight from fetching unread messages; a dedicated mailbox/folder remains strongly recommended. Leaving successfully handled messages unread, custom IMAP searches/flags, MOVE rules, OAuth/provider-specific setup, multiple mailboxes/accounts, and hosted forwarding remain unsupported.

For a guided description of v0.6 calendar aliases, conflict checks, review-queue sensors, permissions and upgrade behavior, see [the v0.6 guide](docs/v0.6.md).

## Installation

### HACS custom repository

1. Open **HACS** in Home Assistant.
2. Open the three-dot menu in the top-right corner and choose **Custom repositories**.
3. Add:
   - **Repository:** `https://github.com/superdingo101/daylight-calendar-import`
   - **Type:** `Integration`
4. Click **Add**.
5. Find **Daylight Calendar Import** in HACS and install it.
6. Restart Home Assistant.
7. Go to **Settings → Devices & services → Add integration** and add **Daylight Calendar Import**.
8. Choose an AI Task entity, a default writable calendar, and the allowed writable calendars. Include the default in the allowed list.

Existing installations migrate their configured calendar into the allowed list automatically when the integration next loads. After setup, administrators can open **Daylight imports → Settings** to change the AI Task entity, default calendar, allowed writable calendars, or Direct IMAP settings without returning to Devices & services. The existing integration **Configure** flow remains available as a fallback and uses the same settings model. The default calendar must remain in the allowed calendar list; other allowed calendars can be added or removed explicitly.

The **Daylight imports** sidebar panel provides **Review inbox**, **Recent activity**, and administrator-only **Settings** views. The review inbox lists imports awaiting review, their source type and time, event count, parser warnings, duplicates skipped, and any uncertain calendar write. Select an import to inspect its source context, warnings, and individual event drafts. You can edit a pending event's title, ISO start/end, all-day flag, location, description, and destination calendar in the panel. The calendar selector is limited to the integration's allowed calendars and preserves each event's current destination. If a source names an unknown or conflicting calendar, Daylight retains the fallback as a preview but blocks approval until you edit the event, explicitly choose a writable destination (even if it is the fallback), and save it. **Approve all** refuses the entire batch if any event still needs routing confirmation; **Reject all** remains available. Timed values need an explicit UTC offset; all-day end dates are exclusive. Approve or reject individual events with a confirmation step; the panel refuses a decision if the event changed since review. For imports with multiple pending events, confirm Approve all or Reject all to process each event independently and see individual results. For an uncertain calendar write, check the destination calendar, then confirm whether it was created, return it to review if it was not created, or discard it. The **Recent activity** view shows completed and failed submissions, outcome totals, and recent transitions. Parse failures include retry guidance. History retains the newest 500 completed records plus active imports and the latest 32 transitions per import; source text and upload bytes are excluded from activity summaries.

In the event detail view, **Check calendars** performs an optional, on-demand read-only check of the selected destination plus configured conflict-observation calendars. It reports exact duplicates, possible duplicates, and scheduling overlaps, or an explicit incomplete/unavailable error. The results are **advisory**, are not stored, and may become outdated; the approval action independently rechecks the selected destination for exact duplicates immediately before each write. If that check is incomplete or finds an exact duplicate, approval stops and the current event stays pending. Authenticated approvals now require both read and control access to the destination calendar; otherwise the preflight rejects approval before reading any private calendar events. Trusted userless Home Assistant automations may invoke bulk `approve_pending`; the single-event `approve_pending_event` action requires an authenticated user. The exact-duplicate guard checks the start of long-running events against their full original interval without trying to read the entire calendar duration. Possible duplicates and overlaps remain advisory. This button does not create events, bypass routing confirmation, block approval, or resolve a previously uncertain calendar write. The review API and observation permission requirements are described in [the review API documentation](docs/review-api.md).

Then test `daylight_calendar_import.parse_text` from **Developer Tools → Actions**.

Use `parse_text` first while evaluating extraction quality. For the review workflow, use `submit_text` and then pass the returned pending import ID to `approve_pending` or `reject_pending`. `submit_text` accepts an optional stable `source_id`; exact source duplicates can then be skipped before invoking AI. It also filters events already pending or previously approved/rejected using normalized fingerprints. `import_text` remains the explicit immediate-import path and intentionally bypasses the pending-review deduplication pipeline.

For a photographed schedule or flyer, use `submit_image` in **Developer Tools → Actions**, choose an image file (maximum 10 MiB), and optionally provide context text and a stable source ID. The configured AI Task entity must support attachments. The upload and its temporary local media copy are removed after parsing or an error; pending review stores the context and image digest, not the image bytes.

For a PDF, use `submit_pdf` with a file up to 10 MiB and 30 pages. Selectable text is extracted locally and combined with optional context, so a text-only PDF can be parsed without sending the attachment. PDFs with scanned pages, images, or page content that cannot be safely extracted retain a temporary attachment alongside any extracted text; this requires an AI Task entity that supports attachments, and PDF interpretation still depends on that entity. Invalid, encrypted, and oversized PDFs return explicit errors. Uploads and temporary copies are cleaned after processing, and pending review stores extracted text and the attachment digest together when both exist, never PDF bytes or temporary media references.

The three pending read actions return responses from **Developer Tools → Actions**. `list_pending` returns summaries without the original source text; `get_pending` returns the full source and drafts; `get_pending_event` accepts both `pending_id` and `event_id`. Reads require an authenticated Home Assistant user with control permission for both the configured AI Task and calendar entities. Pending data remains available after a Home Assistant restart.

To correct an event, call `edit_pending_event` with `pending_id`, `event_id`, and a complete `event` mapping (title, start, end, all_day, optional location/description/confidence). It validates the replacement, rejects exact duplicates in pending or handled history, and keeps the event ID. It also requires control of both configured entities. An event with an uncertain calendar write cannot be edited until that write is resolved.

Use `reject_pending_event` with `pending_id` and `event_id` to remove only that draft. The rejection is remembered for exact event deduplication; other drafts retain their IDs and review status. The source is marked handled once the last event is resolved. A write-uncertain event requires explicit recovery before per-event rejection.

To choose a destination for an individual event, call `edit_pending_event` with the complete `event` mapping and a top-level `calendar_entity` from the allowed list. The chosen destination survives restarts and appears in pending event responses. New submissions start with the configured default; existing pending events without a saved destination continue to use the default. Approval of one event or an entire import writes each event to its selected calendar. Review reads require control of every allowed calendar, and batch approval checks control of every destination before writing. Both per-event and approve-all actions persist a write-in-flight checkpoint before calling the calendar and checkpoint the confirmed result afterward. An interrupted or ambiguous write blocks automatic retry until its outcome is resolved.

If an approval stops with an uncertain write, check the actual calendar before calling `resolve_pending_event`. Use `created` if the event exists (record it as handled), `not_created` if it definitely does not exist (restore the same draft and ID for retry), or `discard` if you want to abandon it (record it as rejected). The last two choices are different: `not_created` allows retry while `discard` suppresses the same event through deduplication. Batch rejection now refuses an import with any unresolved uncertain write.

### Manual development install

Copy `custom_components/daylight_calendar_import` into Home Assistant's `custom_components` directory and restart Home Assistant. Then add **Daylight Calendar Import** under **Settings → Devices & services**.

## Architecture

```
text, image, or PDF adapter -> SourceDocument -> ParserProvider -> EventDraft[] -> validation -> deduplication -> pending review -> calendar
```

Text actions normalize into a `SourceDocument` before parsing. The document describes source identity, kind, text, and attachment references; raw attachment bytes do not belong in pending review storage. The `ParserProvider` interface takes the normalized source and reference time. Its AI Task implementation is the configured parser. The current local implementation returns `ParseOutcome`; the public hosted wire contract formalizes the roadmap's richer `ParseResult` without changing downstream calendar ownership.

Parser capabilities validate media type, attachment count, aggregate size, and text/attachment support before calling the provider. The AI Task adapter checks its entity's attachment feature before passing images or PDFs, while a text-layer PDF uses the text-only path.

### Hosted API v1 contract

The canonical public client/server contract for the future managed parser is documented in [`docs/hosted-api.md`](docs/hosted-api.md), with executable JSON Schemas under [`schemas/hosted/v1/`](schemas/hosted/v1/). The private `daylight-cloud` service must implement and contract-test against these public schemas; review, deduplication, conflict detection, lifecycle state, notifications, and calendar writes remain local to Home Assistant.

Hosted relay services and SMS ingestion remain future work. Direct IMAP ingestion is available in v0.5.0.

## Tests

```bash
pip install -r requirements_test.txt
pytest --cov --cov-branch --cov-fail-under=100
mutmut run
mutmut export-cicd-stats
python scripts/check_mutation_score.py
```

CI requires 100% statement/branch coverage for the Python integration package. For same-repository pull requests, full mutation testing is a **final pre-merge gate** rather than a per-commit check: finish implementation and review work, get the normal CI checks green, then apply the `mutation-ready` label. That label triggers `Mutation score`; if another commit is pushed afterward, mutation testing reruns automatically against the new head. Do not merge until `Mutation score` passes on the current pull request head. Fork/external pull requests are intentionally not eligible for this label-driven final gate.

The mutation gate enforces the baseline in `mutation-baseline.json` via `scripts/check_mutation_score.py`.

The measured mutation score was raised on September 26, 2026 to **95.23%** after adding real Home Assistant lifecycle integration tests: 1,416 of 1,487 generated mutants were killed, 71 survived, and none were untested, suspicious, skipped, timed out, or interrupted. CI enforces a **95.22%** floor to avoid two-decimal rounding rejecting that exact result, and also requires zero untested, suspicious, or segfaulting mutants.
