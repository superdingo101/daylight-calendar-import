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

Processing failures before a durable local outcome are not acknowledged, so the message remains unread and eligible for retry. Once Daylight has durably handled the source, it **attempts** to apply the IMAP `\\Seen` flag.

That means the mailbox's read/unread state is part of the v0.5 transport contract, not merely a visual preference. An acknowledgement transport failure can be outcome-uncertain—the server may or may not have applied `\\Seen` before the error was observed—but the local durable result is kept either way.

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
- Emails with direct supported attachments require writable Home Assistant local media storage because Daylight stages those files there temporarily for AI Task processing. In v0.5, staging uses the first configured local media directory.

### What email content is processable

An email must normalize to at least one usable parser input:

- nonblank `text/plain` or `text/html` body text from the **outer message**; or
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
| No usable `text/plain`/`text/html` body and no direct supported attachment | Rejected before the AI call | Resend with usable outer body text or a direct supported attachment, or remove the original from discovery |
| Content exists only in unsupported body/attachment MIME types (for example `text/calendar` without a usable plain/HTML body) | No usable parser input | Resend usable text/HTML or a supported attachment, or remove the original |
| Usable content exists only inside an attached `message/*` email | Nested content is ignored; the outer source may be empty | Extract/resend the nested content directly, or remove the original |
| Direct supported attachment but AI Task lacks attachment support | Processing fails before the AI call | Use an attachment-capable AI Task entity, remove/resend the supported part, or remove the original |
| Zero-byte direct supported attachment | Rejected as an empty attachment | Replace/resend the attachment, or remove the original |
| Malformed/undecodable supported MIME attachment or MIME structure | Normalization/staging/processing can fail before AI completes | Correct/resend the message, or remove the original if the same malformed source keeps retrying |
| More than 4 direct supported attachments | Rejected as too many attachments | Reduce/split/resend, or remove the original |
| More than 10 MiB decoded direct attachment data | Rejected as too large | Reduce/split/resend, or remove the original |
| Raw RFC message over ~14.3 MiB | Rejected before attachment traversal, including bytes in nested/unsupported parts | Reduce/resend the whole message, or remove the original |
| No configured local media directory, or the first configured media directory is not usable/writable, while direct supported attachments are present | Attachment staging fails | Restore/configure the selected local media storage, resend without supported attachments, or remove the original |

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

Daylight validates the implicit-TLS connection, authentication, mailbox selection, and that the selected mailbox returns a valid IMAP UIDVALIDITY value before saving an enabled configuration. It does **not** preflight message content, AI attachment capability, or local-media writability; those content/environment failures are discovered during polling and follow the retry behavior documented above.

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

`unread email -> discovered -> processing -> review_ready -> attempt to mark email seen`

The pending import is persisted before Daylight acknowledges the message upstream.

### Valid email with no calendar events

A successful parse that finds no reviewable event is a valid terminal result:

`unread email -> discovered -> processing -> no_events -> attempt to mark email seen`

Daylight records the source as durably handled so it is not sent to the AI parser over and over.

### Source already handled locally

If an unread message corresponds to a source that Daylight already knows is durable, Daylight skips expensive reprocessing and retries only the upstream acknowledgement.

This is especially important after an IMAP acknowledgement failure or a restart between local persistence and `\\Seen`.

### How Direct IMAP identifies duplicates

Daylight's source identity is not the IMAP UID. It uses a single valid RFC `Message-ID` when one is available; otherwise it falls back to an identity derived from the exact raw message bytes.

What matters operationally:

- Two distinct emails that reuse the same valid `Message-ID` can be treated as the same source. If the first is already durable, the later message skips AI and Daylight only attempts acknowledgement. Marking it unread does not force reparsing; resend it with a fresh `Message-ID` to make it a new source. Normal event-level deduplication still applies after parsing.
- Without a usable `Message-ID`, differently encoded wire messages can be treated as different sources even if they look equivalent to a person.
- IMAP UID/UIDVALIDITY are used to fetch and acknowledge the mailbox message safely; they are not the long-lived Daylight deduplication identity.
- Completed-source deduplication history is bounded, so sufficiently old completed sources can eventually age out and be processed again if rediscovered. Pending imports remain durable while pending.

## Retry and recovery behavior

| Situation | Local result | Upstream message | Next poll |
| --- | --- | --- | --- |
| Connection/search failure | No new durable result | Unchanged | Retry connection/search |
| Message normalization failure | Lifecycle failure recorded | Left unread | Retry message |
| AI/parser failure | Lifecycle failure recorded; source claim released | Left unread | Retry parsing |
| Local storage/processing failure | No false success | Left unread | Retry processing |
| Successful parse with events | Pending review persisted | `\\Seen` attempted after persistence | No reprocessing; if acknowledgement fails and the message remains unread, retry acknowledgement |
| Successful parse with zero events | `no_events` persisted | `\\Seen` attempted after persistence | No reprocessing; if rediscovered, retry acknowledgement |
| Duplicate durable source | Existing durable result retained | `\\Seen` attempted | No AI call; retry acknowledgement if rediscovered |
| Local success but IMAP acknowledgement reports failure | Local result remains durable | Upstream read state can be uncertain | If the message is still unread and rediscovered, detect duplicate, skip AI, and retry acknowledgement |

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
5. Check **Daylight imports -> Recent activity** for a corresponding discovered, processing, failed, duplicate, `no_events`, or review-ready record. Completed activity history is bounded, so sufficiently old completed records may no longer appear there.
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

- Processable normalized email body text and supported staged attachments are passed to the **configured Home Assistant AI Task entity/provider** for event extraction. That provider's own privacy, retention, network, and billing behavior therefore applies to the email content it receives. Because v0.5 has no sender allowlist, any processable unread message in the configured mailbox can reach that AI Task provider.
- Supported attachment bytes are temporarily written to the selected Home Assistant local media directory so AI Task can access them. Treat that media storage as sensitive while processing is in flight.
- Raw email bytes are not stored in pending-import storage.
- Temporary supported attachments are staged only for processing. Cleanup is best-effort: a deletion failure or an abrupt Home Assistant shutdown can leave a staged `daylight-email-*` file behind in the selected media directory. Cleanup failures are logged when Daylight observes them; after an unclean shutdown there may be no cleanup log, so stale `daylight-email-*` files can be removed manually after confirming no import is using them.
- Pending review may retain the normalized email body text, the normalized email **Subject** as the source title, and for direct supported attachments the original attachment filename when present (otherwise the media type) plus a SHA-256 digest as review context. It does not retain the original attachment bytes. Subjects and filenames can themselves be sensitive local data. Attachment digests are **not** the source/event deduplication key; a newly forwarded message with the same attachment can still invoke AI again.
- Lifecycle activity records retain the normalized email **Subject** as the source title. They do not retain the raw email body/source text, upload bytes, mailbox credentials, or IMAP transport details. Treat email subjects as potentially sensitive local data.
- Source deduplication stores a fingerprint rather than the original `Message-ID` or raw-wire fallback identity.
- Saved IMAP passwords remain in Home Assistant configuration-entry storage and are not repopulated into the browser when editing Options.

## Intentionally unsupported in v0.5

The first Direct IMAP release deliberately does **not** expose:

- sender allowlists or sender filtering;
- arbitrary IMAP search expressions;
- "leave successfully handled mail unread";
- configurable polling intervals;
- MOVE rules or custom IMAP flags;
- OAuth/provider-specific authentication flows;
- STARTTLS or plaintext IMAP connections (v0.5 uses implicit TLS);
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
