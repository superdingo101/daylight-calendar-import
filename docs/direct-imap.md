# Direct IMAP setup and recovery

Direct IMAP is the self-hosted email-ingestion path planned for Daylight Calendar Import v0.5. It lets Home Assistant poll one IMAP mailbox and send eligible messages through the existing Daylight review pipeline.

This guide documents the intentionally bounded v0.5 behavior, how to enable it safely, and how recovery works when a poll, parse, storage operation, or upstream acknowledgement fails.

## What v0.5 Direct IMAP does

For one configured mailbox, Daylight:

1. connects with the configured IMAP host, port, username, password/app password, mailbox, and TLS-verification setting;
2. immediately polls when the integration loads;
3. polls again every five minutes;
4. searches for messages that are both **unseen** and **not deleted**;
5. fetches each matching message without marking it seen during fetch;
6. normalizes the message into the existing Daylight source-document model;
7. runs the existing AI parser, lifecycle tracking, deduplication, pending-review storage, default-calendar selection, and attachment staging;
8. marks the upstream message **seen only after Daylight has a durable local outcome**.

Only one poll runs at a time for the configured integration entry.

A poll processes matching UIDs sequentially. If a large unread backlog makes one poll run longer than five minutes, scheduled ticks that occur while that poll is still active are skipped rather than starting an overlapping poll. The next later tick can continue with whatever remains unread.

## Important behavior before you enable it

### Existing unread mail is eligible immediately

Enabling Direct IMAP does **not** mean "process only mail received from now on."

The first poll runs immediately, and the fixed v0.5 search selects every message in the configured mailbox that is currently unread and not deleted. If the mailbox already contains an unread backlog, those messages are eligible on the first poll.

For the safest first setup, use a dedicated mailbox or folder, or otherwise make sure the unread messages already present are messages you are comfortable sending through Daylight.

### Daylight marks successfully handled messages read

A message remains unread while Daylight still needs to retry it. Once Daylight has durably handled the source, it applies the IMAP `\\Seen` flag.

That means the mailbox's read/unread state is part of the v0.5 transport contract, not merely a visual preference.

### Do not manually mark a failed message read if you want Daylight to retry it

The v0.5 search is fixed to unseen, undeleted messages. If parsing or another retryable operation fails, the message is intentionally left unread so a later poll can try again.

If you manually mark that message read first, Daylight will no longer discover it. To make it eligible again, mark it unread and make sure it has not been deleted.

## Requirements

Before enabling Direct IMAP:

- Daylight Calendar Import must already be configured in Home Assistant.
- Your mail provider must permit password/app-password IMAP authentication. OAuth-only accounts are not supported by the v0.5 Direct IMAP flow.
- The configured mailbox must provide IMAP UID/UIDVALIDITY identity.
- Direct IMAP v0.5 uses an **implicit TLS IMAP connection** (the `IMAP4_SSL` style, normally port 993). STARTTLS and plaintext IMAP are not supported by this flow.
- TLS certificate verification is enabled by default. Disabling **Verify TLS certificate** keeps the connection encrypted but disables certificate validation and hostname checking; use that only when you deliberately trust the server/network.
- The selected AI Task entity must support the content that will actually reach the parser.
- Emails with direct supported attachments require at least one usable Home Assistant local media directory because Daylight stages those files there temporarily for AI Task processing.

### What email content is processable

An email must normalize to at least one usable parser input:

- nonblank body text from the **outer message**; or
- at least one direct supported leaf attachment: JPEG, PNG, WebP, or PDF.

Daylight intentionally does **not** descend into attached/encapsulated `message/*` parts such as `message/rfc822`. Body text and files nested inside an attached email do not become parser input.

Direct supported inline images, such as logos, are attachment input too. If any direct supported attachment is present, the selected AI Task entity must advertise attachment support even when you only care about the email body.

The v0.5 bounds are:

- at most **4 direct supported attachment parts**;
- each direct supported attachment must contain at least **1 decoded byte**;
- at most **10 MiB total decoded data** across the direct supported attachments; and
- about **14.3 MiB maximum for the entire raw RFC message**.

The raw-message cap applies to the whole wire message before MIME traversal. Bytes in unsupported parts and attached `message/*` content therefore still count toward the ~14.3 MiB raw cap even though nested files do not count toward the four-part or 10 MiB decoded-attachment limits.

### Content/configuration conditions that will keep retrying

The fixed v0.5 search deliberately leaves a source unread whenever processing is not durable. Some conditions will therefore repeat on later polls until the source or environment changes:

| Condition | What happens | Recovery |
| --- | --- | --- |
| Blank body and no direct supported attachment | Rejected before the AI call | Resend with usable outer body text or a direct supported attachment, or remove the original from discovery |
| Only unsupported attachment types | No usable parser input | Resend usable content, or mark read/delete/remove the original |
| Usable content exists only inside an attached `message/*` email | Nested content is ignored; the outer source may be empty | Extract/resend the nested content directly, or remove the original |
| Direct supported attachment but AI Task lacks attachment support | Processing fails before the AI call | Use an attachment-capable AI Task entity, remove/resend the supported part, or remove the original |
| Zero-byte direct supported attachment | Rejected as an empty attachment | Replace/resend the attachment, or remove the original |
| More than 4 direct supported attachments | Rejected as too many attachments | Reduce/split/resend, or remove the original |
| More than 10 MiB decoded direct attachment data | Rejected as too large | Reduce/split/resend, or remove the original |
| Raw RFC message over ~14.3 MiB | Rejected before attachment traversal, including bytes in nested/unsupported parts | Reduce/resend the whole message, or remove the original |
| No usable/writable Home Assistant local media directory while direct supported attachments are present | Attachment staging fails | Restore/configure writable local media storage, resend without supported attachments, or remove the original |

Here, “remove the original from discovery” means mark it read, delete it, or otherwise move/remove it from the configured mailbox's fixed unseen/undeleted search.

## Configure Direct IMAP

Open the existing **Daylight Calendar Import** integration in Home Assistant and open its **Options** flow.

Enable **Direct IMAP email ingestion**, then provide:

| Setting | Meaning |
| --- | --- |
| IMAP host | Mail server hostname |
| IMAP port | IMAP TLS port; defaults to 993 |
| Username | Mailbox login username |
| Password or app password | Credential used for IMAP login |
| Mailbox | Mailbox/folder to poll; defaults to `INBOX` |
| Verify TLS certificate | Validate the server certificate; enabled by default |

Daylight validates the connection, authentication, mailbox selection, and IMAP UID support before saving an enabled configuration.

### Editing an existing configuration

When you reopen the options form, Daylight does **not** send the saved password back to the browser as a suggested value.

Leave the password field blank to continue using the credential already stored by Home Assistant. Enter a new password only when you want to replace it.

### Disabling and re-enabling

Disabling Direct IMAP stops the polling runtime but preserves the saved connection settings, including the stored credential.

When you later re-enable it, leaving the password field blank reuses that saved credential.

As soon as it is re-enabled and the integration reloads, the immediate poll runs again.

## What happens to a message

### Normal successful event extraction

For a message that produces reviewable calendar events:

`unread email -> discovered -> processing -> review_ready -> mark email seen`

The pending import is persisted before Daylight acknowledges the message upstream.

### Valid email with no calendar events

A successful parse that finds no reviewable event is a valid terminal result:

`unread email -> discovered -> processing -> no_events -> mark email seen`

Daylight records the source as durably handled so it is not sent to the AI parser over and over.

### Source already handled locally

If an unread message corresponds to a source that Daylight already knows is durable, Daylight skips expensive reprocessing and retries only the upstream acknowledgement.

This is especially important after an IMAP acknowledgement failure or a restart between local persistence and `\\Seen`.

### How Direct IMAP identifies duplicates

Daylight's **source identity is not the IMAP UID**. For a normalized email it uses:

1. exactly one valid, defect-free RFC `Message-ID`, when present; otherwise
2. a SHA-256-based fallback identity derived from the **exact raw RFC wire bytes**.

The opaque source identity is hashed again into Daylight's stored source fingerprint; the original `Message-ID` or raw-message hash string is not stored as the deduplication record.

Operational consequences:

- Two distinct emails that reuse the same valid `Message-ID` are treated as the same source. If the first one is already durable, the later one skips AI and is acknowledged as a duplicate. Marking it unread again does not force reparsing; resend the content with a fresh `Message-ID`.
- If a message has no single valid `Message-ID` (including malformed or multiple Message-ID headers), exact raw wire bytes determine its fallback identity. Semantically identical messages with different wire bytes can therefore be treated as different sources.
- IMAP UID and UIDVALIDITY are used to safely fetch/acknowledge a mailbox message, but they are not the long-lived Daylight deduplication identity.
- Completed source fingerprints are kept in a bounded history of the newest **10,000** handled sources. Pending imports remain durable while pending, but a sufficiently old completed source can eventually age out of dedup history and be processed again if rediscovered.

## Retry and recovery behavior

| Situation | Local result | Upstream message | Next poll |
| --- | --- | --- | --- |
| Connection/search failure | No new durable result | Unchanged | Retry connection/search |
| Message normalization failure | Lifecycle failure recorded | Left unread | Retry message |
| AI/parser failure | Lifecycle failure recorded; source claim released | Left unread | Retry parsing |
| Local storage/processing failure | No false success | Left unread | Retry processing |
| Successful parse with events | Pending review persisted | Marked seen | No reprocessing |
| Successful parse with zero events | `no_events` persisted | Marked seen | No reprocessing |
| Duplicate durable source | Existing durable result retained | Marked seen | No AI call |
| Local success but IMAP `STORE` fails | Local result remains durable | Usually still unread | Detect duplicate, skip AI, retry acknowledgement |

Retryable per-message failures that complete a poll are summarized in Home Assistant logs. A storage or other unexpected exception that aborts the poll is logged through the generic `Email poll failed` exception path instead of the aggregate counters. The runtime does not intentionally include mailbox credentials or raw private message content in either logging path.

## Home Assistant restart recovery

Direct IMAP is designed so a restart does not require the mailbox and local database to complete in one atomic transaction.

### Restart before a durable local result

Interrupted `discovered` or `processing` lifecycle records are recovered as failed. Because the upstream message was never acknowledged as seen, it remains eligible for a later poll.

### Restart after local persistence but before `\\Seen`

The pending import or other durable source outcome survives the restart.

If the email is still unread, the next poll discovers it again, recognizes the durable source identity, skips the parser, and retries only the acknowledgement. This prevents an acknowledgement outage from causing repeated AI work or duplicate pending events.

## Manual recovery checklist

If you expected a message to import but it did not:

1. Confirm Direct IMAP is still enabled in the integration Options.
2. Confirm the message is in the configured mailbox.
3. Confirm the message is **unread** and not deleted.
4. Check Home Assistant logs for a Direct IMAP connection/retry warning **and** for the generic `Email poll failed` error used when an unexpected storage or processing exception aborts the poll.
5. Check **Daylight imports -> Recent activity** for a corresponding discovered, processing, failed, duplicate, `no_events`, or review-ready record. Completed activity history is bounded (currently the newest **500** completed records), so an older completed source may no longer appear there.
6. If the message failed before becoming durable and was manually marked read, mark it unread again to make it eligible for the next poll.
7. If a pending import already exists for the message, do not delete/re-forward the email merely to force another AI pass; review the existing pending import.
8. If local handling succeeded but the email stayed unread, leave it unread. Daylight should recognize the durable source and retry only the IMAP acknowledgement.

## Configuration errors

The Options flow reports these high-level errors:

### Invalid authentication

The IMAP server rejected the username or password.

Verify the username and credential. Some providers require an app password rather than the normal account password.

### Invalid mailbox

The account authenticated, but the configured mailbox could not be selected.

Use the mailbox/folder name expected by that IMAP server.

### Invalid email configuration

One or more settings are structurally invalid.

Check required fields, port, mailbox name, and other entered values.

### Cannot connect

Daylight could not complete the IMAP connection/validation sequence.

Check DNS/network access, server name, port, implicit-TLS compatibility, TLS settings, firewall policy, provider availability, and whether the selected mailbox returns a valid UIDVALIDITY value. Servers that require STARTTLS rather than implicit TLS are not supported by v0.5 Direct IMAP.

## Attachments and privacy

Email normalization reuses Daylight's existing bounded attachment pipeline.

- Raw email bytes are not stored in pending-import storage.
- Temporary supported attachments are staged only for processing. Cleanup is best-effort: Daylight attempts every staged-file deletion and logs cleanup failures without turning an otherwise durable message back into a retry. If cleanup fails, the staged file can remain in Home Assistant's media directory and may require manual removal using the logged path.
- Pending review may retain the normalized email body text, the normalized email **Subject** as the source title, plus attachment metadata and SHA-256 digests as review context, not the original attachment bytes. Those attachment digests are **not** the source/event deduplication key; a newly forwarded message with the same attachment can still invoke AI again.
- Lifecycle activity records retain the normalized email **Subject** as the source title. They do not retain the raw email body/source text, upload bytes, mailbox credentials, or IMAP transport details. Treat email subjects as potentially sensitive local data.
- Source deduplication persists a one-way Daylight fingerprint of the source identity rather than the original `Message-ID` or raw-wire fallback identifier.
- Saved IMAP passwords remain in Home Assistant configuration storage and are not repopulated into the browser when editing Options.

## Intentionally unsupported in v0.5

The first Direct IMAP release deliberately does **not** expose:

- sender allowlists or sender filtering;
- arbitrary IMAP search expressions;
- "leave successfully handled mail unread";
- configurable polling intervals;
- MOVE rules or custom IMAP flags;
- OAuth/provider-specific authentication flows;
- multiple IMAP mailboxes/accounts for one integration entry;
- hosted email forwarding/relay.

Several of those features require a transport checkpoint independent of the read/unread flag. They should not be implemented as simple UI toggles on top of the v0.5 `UNSEEN` design.

## Recommended first-enable procedure

For a low-risk first run:

1. Create or choose a dedicated test mailbox/folder if practical.
2. Put one known unread test email in it.
3. Enable Direct IMAP in Daylight Options.
4. Confirm the message appears in Daylight review or lifecycle history.
5. Confirm the message becomes read only after the local result exists.
6. Test a message with no calendar event and confirm it becomes a `no_events` lifecycle result rather than repeatedly parsing.
7. Restart Home Assistant and verify polling resumes.
8. Disable and re-enable Direct IMAP, leaving the password blank, and verify the saved credential is reused.

After that smoke test, point the integration at the mailbox/folder you intend to use operationally.
