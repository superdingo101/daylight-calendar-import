# Changelog

## 0.9.0

v0.9.0 brings the public-beta improvements into the stable **self-hosted**
Daylight Calendar Import release. No hosted account or subscription is required.

### Highlights

- **Import and review:** Add the global **New import** flow for pasted text and
  images/PDFs (#172). Group source events in a compact review inbox, streamline
  event actions, and show destination calendars without expanding cards
  (#190, #191, #196). Calendar creation still requires explicit approval.
- **Calendar safety:** Show conflicting event and overlap times; observe the
  configured conflict calendars before approval confirmation (#167, #168).
  Guard unsupported recurring announcements (#170), isolate untrusted source
  evidence in AI Task prompts (#166), and bound source-text and routing-alias
  inputs (#169). Observation results can be incomplete and are advisory.
- **Recovery and reliability:** Surface terminal notification-delivery failures
  in Recent activity (#171), and preserve existing uncertain-write recovery.
- **Beta fixes:** Correct inferred DST offsets (#175) and reject nonexistent
  local times associated with named source time zones (#241); repair image/PDF
  imports with unsafe filenames (#189) and duplicate upload validation
  messages (#245); support explicit re-import of duplicate manual submissions
  for review (#176); keep the panel visible across settings reloads (#239);
  and prevent stale Routing tabs from silently erasing other saved aliases
  (#240).
- **Compatibility and documentation:** Add one-click HACS/Marketplace
  installation guidance (#215), CI coverage for Home Assistant 2026.11.0
  alongside the 2026.7.4 minimum (#216), and standalone Mintlify docs
  with reviewed navigation and branding (#217–#220, #222–#228).
  Expand installation, browser, provider, routing, privacy, recovery and
  HACS regression coverage (#153–#165).

### Upgrade from v0.8.0 or a v0.9.0 prerelease

Back up Home Assistant and update the existing integration through HACS or
Marketplace. Restart when required; preserve AI Task setup, allowed calendars,
routing settings and private pending-review storage. **Do not remove and re-add
the integration as an upgrade method**: doing so can delete pending history
and deduplication data while leaving calendar events in place. Inspect any
uncertain calendar write before resolving it.

Automatic recurring-event creation, automatic event approval, and hosted
Daylight services are not part of this release. AI extraction and calendar
observation still require human review. See the
[v0.9.0 release notes](docs/releases/v0.9.0.md) and
[release acceptance checklist](docs/public-beta-checklist.md).

## 0.8.0

- Submit pasted event details, images, and PDFs directly from the Daylight Imports view, including an empty inbox, without configuring IMAP.
- Show validation, processing status, recoverable errors, and links to the existing review/lifecycle flow; calendar creation still requires approval.
- Add authenticated, bounded upload transport and mobile/keyboard improvements for manual submissions.
- Preserve conferencing details and long descriptions, with deterministic extraction, date, routing, adversarial, and manual-submission acceptance coverage.
- Expand the synthetic extraction corpus and optional reference-provider metrics. Recorded provider-response tests verify integration contracts; live model accuracy remains a separate evaluation.

### Upgrade from 0.7.1

Restart Home Assistant after updating. Existing configuration, pending reviews, destinations, and lifecycle records remain in place. Manual submission uses the configured AI Task provider and allowed calendars. Image and scanned-PDF submission requires an attachment-capable provider.

## 0.6.0

- Add deterministic source-level calendar aliases, bounded directive extraction, and explicit review confirmation before any unresolved route may be approved.
- Add independent conflict-observation calendar configuration, on-demand read-only matching and advisory duplicate/overlap results in event review.
- Preflight pending approvals against their destination calendar; exact duplicates and incomplete observations block writes, while ordinary overlaps remain advisory. Authenticated approvals now require calendar read and control permission.
- Add Home Assistant pending-import and pending-event sensors plus a privacy-safe event for newly durable review work.
- Preserve bounded, per-event AI date/time assumption disclosures until the reviewer edits temporal fields.
- Keep pending calendar destinations safe across settings changes; add a native v0.6 guide and cross-layer acceptance tests.
- Add an optional, administrator-configured exact sender allowlist for Direct IMAP in the native Email settings panel. Normalize/validate addresses and enforce the policy before email normalization or AI processing. Nonmatching messages remain unread; filtering by the untrusted From header is not sender authentication and does not avoid mailbox access. Empty allowlists retain the existing allow-all behavior.

## 0.5.0

- Add optional self-hosted **Direct IMAP** ingestion, configurable from the integration's Options flow with host, port, username, password/app password, mailbox, and TLS certificate verification.
- Poll the selected mailbox immediately after setup and every five minutes with a fixed unseen + undeleted search, one non-overlapping poll at a time, complete UID enumeration, and non-destructive message fetching.
- Normalize common plain-text and HTML MIME bodies, preserve a stable source identity from a valid Message-ID when available, and fall back conservatively to exact raw-message identity.
- Stage direct JPEG, PNG, WebP, and PDF email attachments through Home Assistant local media with bounded count/size limits and best-effort cleanup.
- Reserve source identities before AI work so repeated or concurrent observations do not duplicate expensive parsing, while interrupted/transient work remains retryable.
- Correlate email ingestion through Recent activity from discovery to processing and review/no-event/duplicate/failed outcomes without storing raw email bytes or IMAP credentials in lifecycle history.
- Mark upstream mail `\\Seen` only after a durable local outcome. If acknowledgement fails after persistence **and the message remains unread so it is rediscovered**, a later poll skips repeated AI work and retries acknowledgement.
- Treat deterministic source-validation failures as durable terminal failures so unchanged poison messages do not retry forever; temporary provider, storage, media, transport, and acknowledgement failures remain retryable.
- Validate explicit read-only mailboxes and advertised `PERMANENTFLAGS` that do not allow `\\Seen`, while keeping setup validation non-destructive when servers omit those hints.
- Add deterministic cross-layer Direct IMAP coverage plus the setup, privacy, retry, and recovery guide in `docs/direct-imap.md`.

### Upgrade from 0.4.0

Restart Home Assistant after updating the integration. **Direct IMAP is disabled by default**, so upgrading from 0.4.0 does not connect to or poll any mailbox until you explicitly enable it in the integration's Options flow. Existing pending imports, event destinations, deduplication history, lifecycle history, and calendar/AI Task configuration remain in the existing storage.

Before enabling Direct IMAP, read `docs/direct-imap.md`. The first poll runs immediately and selects every unread, undeleted message already present in the configured mailbox, so a dedicated mailbox/folder is recommended for the first run. Saved IMAP credentials remain server-side; when editing an existing Direct IMAP configuration, leaving the password field blank keeps the stored credential.

v0.5 Direct IMAP requires password/app-password IMAP authentication over implicit TLS, a mailbox that supports stable UID/UIDVALIDITY identity and permits the standard `\\Seen` flag, and writable local media storage for supported attachments. OAuth-only provider flows, STARTTLS/plaintext IMAP, custom searches/flags, configurable polling intervals, sender filtering in the Home Assistant UI, multiple mailboxes/accounts, and hosted forwarding remain out of scope for v0.5.

## 0.4.0

- Complete the Home Assistant review panel with event editing, individual and bulk approve/reject, per-event results, and explicit uncertain-write recovery.
- Show recent lifecycle activity, durable outcome counts, bounded transition history, completed imports, and failed parser submissions.
- The review panel sends complete event snapshots so stale edits, decisions, and recovery are rejected, including repeated uncertain write attempts. Existing action callers may omit a snapshot for compatibility.
- Adapt the panel to mobile-sized screens and keyboard navigation.
- Keep the configured write calendar visible in the normal workflow without offering new per-event routing. Existing saved event destinations remain honored.
- Publish the hosted parser API v1 contract in `docs/hosted-api.md`, executable schemas under `schemas/hosted/v1/`, and contract tests for future clients and services.

### Upgrade from 0.3.0

Restart Home Assistant after updating the integration. Pending imports, existing event destinations, deduplication history, and configuration remain in the same storage. Lifecycle history begins with activity recorded after the update; older completed imports cannot be reconstructed. Pre-upgrade pending imports acquire history on their next approval, rejection, or recovery; editing alone does not create a transition. The first such transition is saved atomically in the same storage transaction, even when that action completes the import, without a separate migration step. Verify any previously uncertain calendar write against the actual calendar before choosing created, not created, or discard. The review panel requires an authenticated account with control permission for the configured AI Task and writable calendars.

## 0.3.0

- Normalize text, image, and PDF inputs as source documents and parse them through a provider boundary with explicit media capabilities.
- Accept uploaded PNG, JPEG, and WebP images with optional context; validate size and type, then clean up uploads and temporary media.
- Extract text from bounded PDF files in a resource-limited worker; send scanned or visual pages as temporary attachments when the AI Task entity supports them.
- Keep valid event drafts when individual AI candidates are malformed, reporting indexed warnings for the skipped candidates.
- Preserve text and attachment digests together in pending review without retaining upload bytes or temporary media references.

## 0.2.0

- Persist stable per-event IDs, statuses, and calendar destinations with migration of existing configuration and pending storage.
- List, inspect, edit, approve, or reject individual pending events; retain batch approval and rejection with per-event write checkpoints.
- Resolve uncertain calendar writes explicitly as created, not created, or discarded without automatic duplicate retries.
- Preserve source and event deduplication through edits and per-event decisions.
- Keep valid drafts when an AI response contains malformed siblings, and return indexed warnings.
- Maintain 100% branch coverage across current and minimum supported Home Assistant versions, plus the mutation-score gate.
