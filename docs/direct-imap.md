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
- The selected AI Task entity must be able to handle the content you expect to receive.
- If email attachments need to be interpreted, the AI Task entity must support the relevant attachment path.
- Your mail provider must permit password/app-password IMAP authentication.
- If the provider requires OAuth-only authentication, that account is not supported by the v0.5 Direct IMAP flow.
- The configured mailbox must support stable IMAP UID identity.

TLS certificate verification is enabled by default and should normally remain enabled.

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

Retryable failures are summarized in Home Assistant logs without intentionally logging mailbox credentials or raw private message content.

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
4. Check Home Assistant logs for a Direct IMAP connection or retry warning.
5. Check **Daylight imports -> Recent activity** for a corresponding discovered, processing, failed, duplicate, `no_events`, or review-ready record.
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

Check DNS/network access, server name, port, TLS settings, firewall policy, and provider availability.

## Attachments and privacy

Email normalization reuses Daylight's existing bounded attachment pipeline.

- Raw email bytes are not stored in pending-import storage.
- Temporary supported attachments are staged only for processing and are cleaned up afterward.
- Pending review may retain normalized email text plus attachment metadata/digests needed for review and deduplication, not the original attachment bytes.
- Lifecycle summaries do not retain raw source text, upload bytes, mailbox credentials, or IMAP transport details.
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
