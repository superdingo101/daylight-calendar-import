# Changelog

## Unreleased (v0.6.0)

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
