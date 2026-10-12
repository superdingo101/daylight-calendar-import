# Storage recovery

Daylight fails closed when its private pending-import storage cannot be decoded
or does not match a safe supported schema. Setup stops before exposing a new
review queue, parsing sources or scheduling notification delivery. Home
Assistant shows **Daylight import storage needs repair** under **Settings →
System → Repairs**. The message contains no source content or decoder error
values. Providers already running before a reload can still finish externally.

The integration preserves the original file, valid rows alongside malformed
rows, pending/uncertain event checkpoints, deduplication history and notification
requests. It never silently drops a bad row, creates an empty replacement, or
turns corruption into a confirmed calendar outcome. All collections are checked
before the new snapshot is installed; a previously loaded valid snapshot remains
unchanged if a later load fails, and writes are then blocked. Valid legacy v1
storage is still migrated using the existing uncertainty rules. Known optional
date/time disclosures retain their existing safe fallback; they cannot make
otherwise valid calendar decisions inaccessible.

1. Stop Home Assistant and make a private copy of
   `.storage/daylight_calendar_import.pending_imports` and any matching
   `.corrupt.*` file. These files can contain sensitive source context, parsed
   drafts, entity IDs and deduplication evidence.
2. Restore a known-good Home Assistant backup, or carefully repair the original
   file without discarding unresolved imports, delivery requests or fingerprints.
   Malformed JSON is preserved in place. If an earlier HA version already
   renamed it to `.corrupt.*`, Daylight refuses to create a replacement while
   the original is missing; restore a valid original from the preserved data.
3. Restart Home Assistant or reload Daylight after restoring valid storage. The
   repair issue clears after a successful validated load.
4. For every uncertain calendar write, check the destination calendar before
   explicitly recording its outcome through the existing review controls.

Do not delete the file, recreate the integration, or resend sources as a repair
shortcut: losing the write checkpoints or handled-source/event fingerprints can
create duplicate calendar events. Unsupported versions also stop safely; after
a downgrade, restore a compatible integration version rather than editing the
version number. Files unreadable because of disk or permission problems require
those problems to be corrected first. There is no automated reset or partial
salvage UI, and this policy does not promise recovery of destroyed bytes.
