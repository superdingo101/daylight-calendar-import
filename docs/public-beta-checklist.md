# Public beta validation

Use this checklist on an isolated Home Assistant installation and dedicated test
calendars. Record the candidate Git SHA, installed version, HA version, browser,
provider/model and date with each result. Test the minimum supported HA 2026.7.4
and the pinned HA 2026.9 line covered by CI. An unchecked item is pending, not a
passing result. Keep private sources, mailbox credentials and meeting links out
of the public evidence record.

## Installation and upgrade

- [ ] Make a full Home Assistant backup before upgrading. Keep the backup private;
  pending drafts, source context, tokens and deduplication history may be sensitive.
- [ ] Fresh install the exact candidate using the manual candidate path below,
  restart HA, then add the integration. Select an AI Task entity, default calendar
  and allowed calendars. Confirm the sidebar opens the review inbox.
- [ ] Upgrade a development copy using the same candidate ZIP and manual path.
  Restart HA. Verify version, settings and saved pending/event destinations.
- [ ] Separately verify the HACS path once a published release or prerelease with
  its release ZIP is available. Enable prereleases in HACS if applicable and
  select that exact version. Record the release tag and matching source SHA;
  installing the latest older release is not candidate validation.
- [ ] On a development copy, upgrade representative older configurations/storage.
  Preserve pending work, handled-source/event fingerprints, notification state
  and uncertain calendar-write checkpoints. Do not edit private storage while HA
  is running. Automated fixture migration is evidence for those fixtures only.
- [ ] Restart HA with pending imports. Confirm queue counts and review contents
  reload without creating calendar events or replaying a new-import automation.

### Installing an unpublished candidate

HACS cannot install an arbitrary local ZIP, a private draft release or an
unpublished commit. The packaging check builds a ZIP but does not upload it as
a downloadable Actions artifact. Before a release exists, build from a clean
checkout of the recorded candidate SHA instead:

```sh
git clone https://github.com/superdingo101/daylight-calendar-import.git daylight-beta
cd daylight-beta
git fetch origin <candidate-branch>
git checkout --detach <full-candidate-commit-SHA>
git rev-parse HEAD
git status --porcelain
python -c 'import json; print(json.load(open("custom_components/daylight_calendar_import/manifest.json"))["version"])'
python scripts/build_hacs_release.py --version <printed-version> --output daylight-calendar-import.zip
python -m zipfile -l daylight-calendar-import.zip
```

Replace the angle-bracket placeholders with the candidate branch, full SHA and
printed version. Require an empty `git status` before building. Record the SHA,
manifest version and ZIP SHA-256 (for example `sha256sum daylight-calendar-import.zip`)
with the result; the builder verifies the archive against the checked-out files.

Stop the development Home Assistant instance. For an upgrade, move its existing
`custom_components/daylight_calendar_import` directory to a backup location
**outside** `custom_components`, then create a fresh directory at that original
path. Extract the ZIP **into that directory**: its root contains `manifest.json`
and `__init__.py`, not a wrapping `custom_components` directory. Preserve the
integration entry and `.storage` files. Start HA and verify the loaded version.
Use the same archive for the fresh-install and upgrade tests. On a fresh test
installation, add the integration only after restart.

This establishes manual candidate installation/upgrade evidence, not HACS
download evidence. After a candidate is available to HACS as a published release
or prerelease with the asset attached, repeat installation/upgrade through HACS
and record that result separately. Neither a successful package build nor an
installation of a different version satisfies that HACS check.

## Manual imports without email

Leave Direct IMAP disabled throughout this section. Use an empty inbox first.
Run on desktop and a narrow mobile screen, then repeat key actions with keyboard
navigation. See [Manual imports](manual-import.md) for the supported UI journey.

- [ ] Paste an explicit single event, submit, open Review events, edit and approve.
  Confirm exactly one destination event, then submit the same source again and
  verify duplicate handling.
- [ ] Paste a multi-event schedule; review all candidates and mixed approve/reject
  results. No calendar event may appear before approval.
- [ ] Upload JPEG/PNG/WebP and text-layer PDF fixtures. With an attachment-capable
  model, also submit an image and multipage scanned PDF. Verify both PDF pages'
  events and descriptions survive. Check progress, disabled repeated submit and
  focus/navigation behavior while the request is in flight.
- [ ] Exercise empty text, unsupported files, oversized input, provider failure
  and a dropped browser connection. Verify actionable errors and retry retained
  input. Check inbox/activity before retrying an uncertain browser submission.
- [ ] Confirm a text-only provider rejects images/scanned-PDF attachments clearly
  while ordinary text and usable PDF text layers still work.

## Settings, routing and calendar safety

- [ ] Save General, Calendars, Routing, Email and Notifications settings, reload,
  then verify persistence. Test two simultaneous settings pages and stale saves.
- [ ] Confirm a regular user cannot change administrator-only settings or read
  secrets through settings responses. Calendar review requires the appropriate
  HA permissions; test with a restricted account as well as an administrator.
- [ ] Use `Calendar: Kids` with an explicitly configured alias. Verify its exact
  destination; unknown/conflicting aliases must require explicit resolution.
  Change a pending draft's destination within the allowed set.
- [ ] Check exact duplicates, likely duplicates and overlaps across observation
  calendars. An observation calendar does not become a writable destination.
  Unavailable observations must fail visibly, rather than return a false clear.
- [ ] Approve individually and in bulk, including mixed decisions and simultaneous
  approval requests. Verify no duplicate creation. Recheck destination/calendar
  failures after restoring availability.
- [ ] On a development instance, interrupt an external write and restart. Inspect
  the actual destination calendar before resolving the uncertain result as
  created, not created or discarded. A restart must never blindly retry it.

## Optional Direct IMAP

Use a dedicated test mailbox/folder. Enabling Direct IMAP can fetch existing
unread mail; see [mailbox privacy and recovery](direct-imap.md).

- [ ] Forward plain-text and HTML announcements, including mixed supported and
  unsupported attachments. Verify drafts, source context and attachment cleanup.
- [ ] Repeat delivery, temporarily disconnect the mailbox/provider, then restore
  it. Verify recovery without lost sources or repeated calendar creation.
- [ ] Confirm messages are marked read only after a durable local outcome. A
  retryable provider/storage failure must not acknowledge unfinished work.
- [ ] Disable Direct IMAP and verify manual paste/upload still works.

## Notifications, queue automations and privacy

- [ ] With notifications disabled, verify no notification delivery. Enable only
  selected categories and a test notify entity; exercise review-ready, calendar
  outcome, uncertain-write and failure notifications as applicable.
- [ ] Restart during a retry; verify durable recovery and bounded attempts. Notify
  delivery is at least once: a crash between external delivery and local save
  can repeat a message. It does not repeat the calendar write.
- [ ] Verify both pending sensors match active/uncertain drafts. Rename a sensor
  and adapt the example automation to its actual entity ID.
- [ ] Test the event-driven reminder in [Queue automations](v0.6.md#home-assistant-queue-automations).
  Confirm `daylight_calendar_import_pending_added` contains only `pending_id`
  and `event_count`. Confirm restart alone does not fire it.
- [ ] Export diagnostics and inspect notification messages and automation payloads
  for raw source text, addresses, attachment paths, join URLs and passcodes.
  Diagnostics are redacted; pending review and calendar descriptions intentionally
  contain event details. Recent activity may include titles and source metadata.
- [ ] Check [retention](retention.md): unresolved/uncertain work remains protected;
  completed activity and notification history are bounded. Do not expect source
  reprocessing to recover information that was never retained.

## Provider evaluation and known limitations

- [ ] Run at least one actual development/reference AI Task provider against the
  [synthetic extraction corpus](extraction-corpus.md), outside required PR CI.
  Record provider/model versions, aggregate scores and supported media. Recorded
  response replay is not live extraction evidence.
- [ ] Publish model/media limitations and any core journey that failed dogfood.
  AI may miss or misinterpret events; always review dates, offsets and details.
  Attachment capability is not a promise that every model accepts scanned PDFs.
- [ ] Retain existing limitations: optional single-mailbox password/app-password
  Direct IMAP; no Daylight hosted account needed; no automatic event approval;
  no recurring-event creation or automatic update/delete of existing events.

## Removal and reinstall

Test removal only in a disposable installation after verifying a usable backup.
Removing the integration through Devices & services deletes Daylight's persisted
queue, history and fingerprints. It does not delete already-created calendar
events. Reinstalling with empty history and resubmitting old sources can therefore
create duplicates. It is not a repair or safe upgrade method.

- [ ] Disable email and finish or explicitly resolve pending/uncertain work.
- [ ] Remove the integration, then remove its HACS package and restart. Verify
  services, sensors and the sidebar are removed, while calendar events remain.
- [ ] Reinstall/add the integration on the disposable instance and confirm clean
  setup. To recover previous data instead, restore the full known-good backup.
  For corrupted storage, follow [storage recovery](storage-recovery.md).

## Release evidence record

| Evidence | Result to record before release |
| --- | --- |
| Exact source/version and release ZIP | Candidate SHA/version, packaging checks and release artifact |
| Normal CI | Passing supported-HA, frontend, packaging, HACS and Hassfest checks |
| Code review | Clean Codex result after the final substantive commit on each PR |
| Mutation release gate | Clean proof for the final release source tree under the existing workflow |
| Real installation/upgrade | Dates, HA versions, install path and checklist results |
| Actual provider evaluation | Provider/model versions, dates, numeric scores and limitations |
| Desktop/mobile dogfood | Browser/device, completed journeys and unresolved failures |

This document starts as a procedure, not a claim that those manual checks have
already passed. Data loss, duplicate-write risk, privacy regressions and failed
core journeys block release. Hosted-service, billing and SMS/MMS work is outside
this beta milestone.
