# Daylight Calendar Import

A Home Assistant custom integration that turns unstructured text into validated calendar event drafts using the user's existing **AI Task** provider.

This repository is intentionally separate from [Daylight Calendar Card](https://github.com/superdingo101/daylight-calendar-card). The import integration owns ingestion, parsing, and review; the card may later provide an optional shortcut.

## v0.3.0 source ingestion

The integration supports:

- UI configuration of an existing Home Assistant AI Task entity
- UI configuration of a default calendar and an allowed list of writable calendars
- `daylight_calendar_import.parse_text`: parse text and return validated drafts without changing a calendar
- `daylight_calendar_import.import_text`: parse text and create the validated events on the configured calendar
- `daylight_calendar_import.submit_text`: parse text, deduplicate it, and persist only new events for review
- `daylight_calendar_import.submit_image`: upload a PNG, JPEG, or WebP image with optional text, then queue drafts for review
- `daylight_calendar_import.submit_pdf`: upload a PDF with optional context; extract its text layer locally or use an attachment-capable parser
- `daylight_calendar_import.approve_pending`: create every event in a pending import, then remove it from review
- `daylight_calendar_import.reject_pending`: remove a pending import without creating calendar events
- `daylight_calendar_import.list_pending`: list pending import IDs, titles, counts, and uncertain-write flags
- `daylight_calendar_import.get_pending`: retrieve a pending import with its source text and event drafts
- `daylight_calendar_import.get_pending_event`: retrieve a draft using its pending import ID and stable event ID
- `daylight_calendar_import.edit_pending_event`: replace a draft or select its allowed calendar before approval, preserving its ID and checking for duplicates
- `daylight_calendar_import.reject_pending_event`: reject one draft while retaining other events in the import
- `daylight_calendar_import.approve_pending_event`: create one draft on the configured calendar, retaining the others
- `daylight_calendar_import.resolve_pending_event`: explicitly resolve a calendar write whose outcome is uncertain
- multiple events in one input
- timed and all-day events
- strict validation of AI output
- persistent deduplication by optional upstream source ID and normalized event fingerprint

The parser instructs the AI not to invent missing event data. Invalid individual events are skipped with indexed `warnings` in parse, import, and submit responses; valid events in the same response remain available. An invalid top-level AI response still fails without creating calendar events. When every event is invalid, no event is imported or queued.

The current minimum supported Home Assistant version is **2026.7.4**. CI tests that version explicitly alongside the current development test environment.

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

Existing installations migrate their configured calendar into the allowed list automatically when the integration next loads.

The **Daylight imports** sidebar panel lists imports awaiting review, their source type and time, event count, parser warnings, duplicates skipped, and any uncertain calendar write. This first panel view is read-only; use the review actions in Developer Tools to inspect or decide an individual import until the detail and decision screens are available.

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

Text actions normalize into a `SourceDocument` before parsing. The document describes source identity, kind, text, and attachment references; raw attachment bytes do not belong in pending review storage. The `ParserProvider` interface takes the normalized source and reference time. Its AI Task implementation is the configured parser. A future hosted Daylight parser can return the same `ParseOutcome` contract, allowing BYO AI and managed paid AI to coexist without changing downstream behavior.

Parser capabilities validate media type, attachment count, aggregate size, and text/attachment support before calling the provider. The AI Task adapter checks its entity's attachment feature before passing images or PDFs, while a text-layer PDF uses the text-only path.

Email/SMS ingestion, a dedicated review UI, and hosted relay services remain future work.

## Tests

```bash
pip install -r requirements_test.txt
pytest --cov --cov-branch --cov-fail-under=100
mutmut run
mutmut export-cicd-stats
python scripts/check_mutation_score.py
```

CI requires 100% statement/branch coverage for the Python integration package and also enforces the mutation-testing baseline in `mutation-baseline.json`.

The measured mutation score was raised on September 26, 2026 to **95.23%** after adding real Home Assistant lifecycle integration tests: 1,416 of 1,487 generated mutants were killed, 71 survived, and none were untested, suspicious, skipped, timed out, or interrupted. CI enforces a **95.22%** floor to avoid two-decimal rounding rejecting that exact result, and also requires zero untested, suspicious, or segfaulting mutants.
