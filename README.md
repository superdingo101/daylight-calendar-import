# Daylight Calendar Import

A Home Assistant custom integration that turns unstructured text into validated calendar event drafts using the user's existing **AI Task** provider.

This repository is intentionally separate from [Daylight Calendar Card](https://github.com/superdingo101/daylight-calendar-card). The import integration owns ingestion/parsing; the card can later provide a richer review UI.

## Proof of concept

The first proof of concept supports:

- UI configuration of an existing Home Assistant AI Task entity
- UI configuration of a target calendar
- `daylight_calendar_import.parse_text`: parse text and return validated drafts without changing a calendar
- `daylight_calendar_import.import_text`: parse text and create the validated events on the configured calendar
- `daylight_calendar_import.submit_text`: parse text, deduplicate it, and persist only new events for review
- `daylight_calendar_import.approve_pending`: create every event in a pending import, then remove it from review
- `daylight_calendar_import.reject_pending`: remove a pending import without creating calendar events
- `daylight_calendar_import.list_pending`: list pending import IDs, titles, counts, and uncertain-write flags
- `daylight_calendar_import.get_pending`: retrieve a pending import with its source text and event drafts
- `daylight_calendar_import.get_pending_event`: retrieve a draft using its pending import ID and stable event ID
- `daylight_calendar_import.edit_pending_event`: replace a draft before approval, preserving its ID and checking for duplicates
- `daylight_calendar_import.reject_pending_event`: reject one draft while retaining other events in the import
- `daylight_calendar_import.approve_pending_event`: create one draft on the configured calendar, retaining the others
- multiple events in one input
- timed and all-day events
- strict validation of AI output
- persistent deduplication by optional upstream source ID and normalized event fingerprint

The parser deliberately fails closed. It instructs the AI not to invent missing event data, and integration-side validation rejects unsafe output.

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
8. Choose an AI Task entity and a writable calendar.

Then test `daylight_calendar_import.parse_text` from **Developer Tools → Actions**.

Use `parse_text` first while evaluating extraction quality. For the review workflow, use `submit_text` and then pass the returned pending import ID to `approve_pending` or `reject_pending`. `submit_text` accepts an optional stable `source_id`; exact source duplicates can then be skipped before invoking AI. It also filters events already pending or previously approved/rejected using normalized fingerprints. `import_text` remains the explicit immediate-import path and intentionally bypasses the pending-review deduplication pipeline.

The three pending read actions return responses from **Developer Tools → Actions**. `list_pending` returns summaries without the original source text; `get_pending` returns the full source and drafts; `get_pending_event` accepts both `pending_id` and `event_id`. Reads require an authenticated Home Assistant user with control permission for both the configured AI Task and calendar entities. Pending data remains available after a Home Assistant restart.

To correct an event, call `edit_pending_event` with `pending_id`, `event_id`, and a complete `event` mapping (title, start, end, all_day, optional location/description/confidence). It validates the replacement, rejects exact duplicates in pending or handled history, and keeps the event ID. It also requires control of both configured entities. An event with an uncertain calendar write cannot be edited until that write is resolved.

Use `reject_pending_event` with `pending_id` and `event_id` to remove only that draft. The rejection is remembered for exact event deduplication; other drafts retain their IDs and review status. The source is marked handled once the last event is resolved. A write-uncertain event requires explicit recovery before per-event rejection.

Use `approve_pending_event` with the same IDs to create only that event. Both per-event and approve-all actions persist a write-in-flight checkpoint before calling the calendar and checkpoint the confirmed result afterward. An interrupted or ambiguous write blocks automatic retry until its outcome is resolved.

### Manual development install

Copy `custom_components/daylight_calendar_import` into Home Assistant's `custom_components` directory and restart Home Assistant. Then add **Daylight Calendar Import** under **Settings → Devices & services**.

## Architecture

```
input -> parser provider -> EventDraft[] -> validation -> deduplication -> pending review -> calendar
```

The POC uses Home Assistant AI Task as the first parser provider. A future hosted Daylight parser can return the same `EventDraft` contract, allowing BYO AI and managed paid AI to coexist without changing downstream behavior.

Email/SMS ingestion, attachments, a review UI, and hosted relay services remain future work.

## Tests

```bash
pip install -r requirements_test.txt
pytest --cov --cov-branch --cov-fail-under=100
```

CI targets 100% branch/code coverage for the Python integration package.
