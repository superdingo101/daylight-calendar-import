# Daylight Calendar Import

A Home Assistant custom integration that turns unstructured text into validated calendar event drafts using the user's existing **AI Task** provider.

This repository is intentionally separate from [Daylight Calendar Card](https://github.com/superdingo101/daylight-calendar-card). The import integration owns ingestion/parsing; the card can later provide a richer review UI.

## Proof of concept

The first proof of concept supports:

- UI configuration of an existing Home Assistant AI Task entity
- UI configuration of a target calendar
- `daylight_calendar_import.parse_text`: parse text and return validated drafts without changing a calendar
- `daylight_calendar_import.import_text`: parse text and create the validated events on the configured calendar
- multiple events in one input
- timed and all-day events
- strict validation of AI output

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

Use `parse_text` first while evaluating extraction quality. `import_text` creates returned events immediately and exists only to prove the end-to-end calendar path.

### Manual development install

Copy `custom_components/daylight_calendar_import` into Home Assistant's `custom_components` directory and restart Home Assistant. Then add **Daylight Calendar Import** under **Settings → Devices & services**.

## Architecture

```
input -> parser provider -> EventDraft[] -> validation -> calendar
```

The POC uses Home Assistant AI Task as the first parser provider. A future hosted Daylight parser can return the same `EventDraft` contract, allowing BYO AI and managed paid AI to coexist without changing downstream behavior.

Email/SMS ingestion, attachments, pending-review storage, deduplication, and hosted relay services are intentionally out of scope for this first PR.

## Tests

```bash
pip install -r requirements_test.txt
pytest --cov --cov-branch --cov-fail-under=100
```

CI targets 100% branch/code coverage for the Python integration package.
