# Hosted parsing into local review

This flow is part of the isolated `integration/hosted-v1` development build. It
uses the public [Hosted API v1](hosted-api.md) and the
[hosted parser adapter](hosted-parser.md). It does not publish a hosted HACS
release or establish deployed M3 acceptance.

## Provider selection and normal import

Select **Daylight Cloud** in native General settings and connect through the
Daylight Cloud settings tab with processing consent. A fresh hosted entry needs
no local AI Task entity. Switching modes preserves saved BYO AI, calendars,
Direct IMAP settings and local review data. Selecting Cloud never silently uses
a retained BYO AI Task when the connection or managed access is unavailable.

Use **New import** to paste text or upload an image/PDF. The selected provider
returns drafts to the same durable local review pipeline used by BYO. Manual
import is available with an empty inbox and without email setup. Source routing,
deduplication, conflict checks, review decisions and calendar writes remain in HA.
Cloud receives the canonical bounded source request, not local upload paths or
calendar authority. Valid zero-event output records a successful no-event outcome;
malformed output records a failure rather than an empty success.

A fresh hosted entry can parse before configuring a writable calendar. Its
drafts are explicitly unresolved and cannot be approved. Add the first writable
calendar in native settings, then explicitly choose each draft's destination in
review. Adding a default does not silently resolve the stored drafts. Previously
assigned destinations retain the existing protection against removing or
implicitly changing their calendars.

The existing `parse_text` service remains parse-only. The legacy `import_text`
service is an explicit request to create local calendar events and retains that
behavior, requiring a configured writable calendar and the existing permission
checks. Normal paste/upload uses `submit_text`/`submit_image`/`submit_pdf` and
requires review before a calendar write.

## Connection, cancellation and recovery

A parse uses a private session from the loaded connection runtime. Saved material
alone is insufficient: the current credential must have been verified by that
runtime. Disconnect, replacement, lifecycle work or unload invalidates an earlier
session even if the runtime later resumes. The adapter verifies consent, current
credential and trusted service before preparation, before the request and after
the response. Local acceptance checks the same session after acquiring the real
pending-store transaction lock, so a stale result cannot become pending while
waiting for that lock.

Once a valid local durable transaction starts, its existing shielded-save and
uncertain-save recovery rules still apply. Cancellation or a later disconnect does
not undo accepted local review data or interrupt an uncertain storage commit.

Authentication failure directs the user to repair the Daylight connection. A
verified connection with unavailable managed access directs them to check access;
reconnecting alone does not grant that capability. Rate limits require waiting.
Unknown outcomes direct the user to activity/review before a deliberate retry.
The client does not automatically replay the parse POST, and `X-Request-ID` is
correlation rather than an idempotency guarantee. Raw service/provider messages,
source bodies, attachments and credentials are not added to general logs or
settings snapshots.

The public v1 result preserves explicit timestamp offsets and bounded warnings.
It does not carry the BYO provider's typed `uses_reference_time_zone` or assumptions
fields. The adapter retains canonical event warnings with their event context; it
does not infer temporal flags from warning text or private provider metadata. A
richer typed temporal contract requires its own public contract change.

## Direct IMAP limitation in this development stage

Direct IMAP remains a complete BYO feature. While Cloud parsing is selected, its
polling runtime is paused before mailbox/network or model work. The native Email
tab discloses this pause; saved connection settings remain intact. An explicit
switch back to BYO resumes the existing Direct IMAP behavior with those settings.
Cloud paste/upload continues to work while IMAP is paused.

This restriction is deliberate: Direct IMAP automatically retries failed source
processing, while v1 has no durable managed-parse reconciliation for a lost
response. The integration must not silently repeat an uncertain managed POST or
acknowledge failed parsing. Hosted automatic email processing, source delivery
and durable parse idempotency need their later contracts and implementation.
This change introduces no email delivery/ACK protocol or new source-attempt
ledger.

## Verification scope

Automated acceptance exercises normal HA service dispatch, the real runtime
parser factory and hosted adapter, canonical text/image/text-PDF/scanned-PDF
requests, and real local storage with only the external HTTP boundary simulated.
It checks unresolved review, restart persistence, first-calendar configuration,
explicit destination/approval, no BYO fallback, connection errors, generation
fences and storage-lock races. Separate adapter tests exercise strict wire and
socket behavior. Browser tests check hosted repair/access/uncertainty guidance
and native IMAP pause disclosure.

These are isolated automated results. Actual dev managed-provider receipt and
local-review observation, staging deployment and cost measurements remain their
own M3 evidence gates.
