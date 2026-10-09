# ADR-0007: Hosted source delivery v1 — import-first acknowledgement and at-least-once claims

**Status:** Accepted for design; implementation pending  
**Date:** 2026-10-08  
**Applies to:** `daylight-calendar-import` public H3 contract and subsequent HA adapter; private `daylight-cloud` H3 HTTP adapter and delivery ledger  
**Supersedes:** Claim-request response replay / expiring idempotency keys / pagination cursor obligations currently proposed by PR #85  
**Decision owners:** Daylight maintainers

## Context

Daylight Cloud retains source documents (including attachment descriptors and binary payloads) for Home Assistant to import. PR #85 expanded to exact claim-response replay and deeply coupled ACK recovery to the mutable lifecycle of pending events. This made the contract hard to implement, obscured the transfer-of-responsibility boundary, and generated multiple cycles of review findings. The existing private Cloud queue already offers exclusive leases, re-delivery, and tenant-scoped acknowledgements; the public HA integration already has a durable pending-import store and source-deduplication logic.

## Decisions

### 1. Import-first acknowledgement and an independent ACK journal

**Cloud ACK means:** Home Assistant has durably recorded an *eligible import outcome* (pending import with recoverable events, no-events terminal outcome, or duplicate terminal outcome) **and** durable transport ACK state. It does not mean that the user has reviewed, approved or rejected events, or that a calendar event was written. Failed, incomplete, volatile or uncertain parsing outcomes are not ACK-eligible.

The HA integration MUST atomically persist the local import outcome and a separate hosted-delivery ACK journal entry in **one logical durable commit**. If existing store abstractions cannot guarantee that atomic commit, the HA adapter must extend its durable storage transaction (or use an equivalent tested transactional boundary) before it can ACK. Do not create two independent writes and assume they are atomic. Pending imports and completed-review records may change independently *after* the commit; that must not erase or invalidate an unresolved ACK obligation.

**Minimal journal fields:** `local_config_entry_id`, `delivery_id`, `lease_token` (secret), `source_expires_at`, `source_fingerprint` (namespaced delivery identity), `source_evidence_sha256`, `attachment_descriptor_manifest_digest` / verified attachment IDs and SHA-256s, `acceptance_disposition` (`pending | no_events | duplicate`), `ack_status` (`pending | confirmed | expired`), and safe attempt metadata (`last_attempt_at`, `attempt_count`). A reference to the local pending import, if applicable, is diagnostic only after the acceptance transaction; it is not a condition of subsequent ACK retry. Do not retain raw source bytes solely for ACK retries. All journal data must be private, and lease tokens must never appear in logs or activity UI.

Before the first ACK, downloaded attachment bytes must be checked against advertised length and SHA-256 and the complete normalized source/attachment evidence recorded. The ACK journal stores immutable proof of completed validation; on *same-token* retry after a lost response, HA uses the persisted journal without re-downloading Cloud bytes (which Cloud might already have deleted). A *new lease/token* requires verifying the newly supplied immutable source against the persisted evidence and then atomically advancing the journal's current token before ACK.

Journal entries remain until confirmed ACK or source expiry; confirmed entries may be compacted once confirmation is durably recorded, subject to local dedup needs. An expired, unconfirmed entry MUST become an observable failure (not silent success), without re-ACKing a stale token. Source-deduplication history is independent of the transport journal and may have different retention.

### 2. At-least-once claims without exact response replay or cursors

Use `POST /v1/sources/claim` with a bounded `limit` (default 20, max 50). No `cursor`, `claim_request_id`, `claim_request_expires_at`, request replay marker, or exact original-page replay requirement. A claim acquires a short exclusive lease and yields zero or more eligible immutable deliveries. HA MUST disable automatic transparent retries of the claim POST. If its HTTP response is lost, HA performs a later **new claim**, not replay of the earlier POST. The unseen lease expires and the source becomes claimable again. This is intentional **at-least-once**, not exactly-once, delivery.

Retain a stable per-delivery opaque `delivery_id`, installation-scoped auth/authorization, immutable source and attachment content for the active delivery lifetime, per-lease unguessable tokens, current-token ACK checks, and ACK idempotency for previously confirmed same-token acknowledgements. A lease is at most **120 seconds** for v1 and never extends past source expiry; Cloud's internal queue already uses a 120-second lease. A client can pick smaller batches to meet processing load; it must persist its import outcome before ACK. Large/slow processing may outlast a lease and legitimately require re-lease/revalidation; if necessary later, add explicit lease extension as a separate versioned feature, not an implicit v1 guarantee.

Cloud MUST NOT silently issue an ACK for a stale token after re-lease. The earlier confirmed same-token ACK must remain idempotently confirmable until the delivery's immutable `source_expires_at`, even if raw content has been removed. The client may receive an expired/stale-lease error and wait for fresh re-delivery. A lost claim response delays a source but never causes permanent data loss before its seven-day retention expiry.

### 3. Retention, identity, and tenant isolation

Cloud retains source payloads for at most **604,800 seconds (seven 24-hour days)** from durable Cloud enqueue. This deadline is immutable across retry, re-lease and ACK. The source email's `received_at` is not Cloud enqueue time. An expired source may be ingested anew only as a **new delivery with new IDs**; never reuse a spent delivery ID or silently reset its creation timestamp. Source metadata is allowlisted; attachments are fetched only using authenticated, tenant-scoped and current-lease-scoped APIs, with length and SHA-256 verification. Never expose raw upstream storage URLs or permit cross-origin redirects. No public calendar-write endpoint.

### 4. No pagination for queue claims in v1

The queue is drained with successive bounded claim requests, not navigated as a historical collection. An empty result means no currently claimable items; it does not prove permanent exhaustion. Pagination/cursors can be designed separately for a read-only administrative listing if ever needed.

## Normative state transitions

| Starting state | Event | Required next state / effect |
|---|---|---|
| Cloud ready | Claim | Leased (120s max), opaque ID and token supplied |
| Cloud leased, HA has not committed outcome | HA crash/parse failure | No ACK; Cloud re-leases after expiry |
| HA processing and successful pending/no-events/duplicate outcome | Atomic store commit | Eligible ACK journal `pending` and durable import disposition together |
| Journal `pending` with current token | ACK success | Cloud confirms; HA journal `confirmed` |
| Cloud accepted ACK; response lost | Same-token retry | Original success replays until source expiry; no source download needed |
| Lease expired and ACK not committed | New claim with rotated token | HA compares immutable source evidence, updates journal token durably, then ACKs |
| User approves/rejects pending events | Review lifecycle change | No effect on accepted transport journal or its ACK eligibility |
| Source retention expiry before accepted outcome | Cloud expiry | Source removed; expose expired/not-imported failure |
| Local unconfirmed journal reaches expiry | Journal cleanup/recovery | Mark terminal expired; do not send ACK using stale evidence |

## Explicit non-goals for v1

- Exactly-once delivery, exact claim-page replay, claim idempotency request IDs, cursor pagination, lease renewal, Cloud-side AI parsing or calendar writes.
- Raw email and attachment caching in a separate local inbox before parsing.
- Guarantee of import after the seven-day Cloud retention deadline if HA or AI parsing is unavailable.
- A public Cloud endpoint in the contract-only PR.

## Consequences and tradeoffs

**Benefits:** simple queue semantics, smaller server state, no claim replay tombstones/cursors, one durable handoff boundary, and ACK recovery independent of user review. Aligns with Cloud's existing 120-second leased queue.

**Costs:** a lost claim response may strand an unseen lease for up to 120 seconds; claims are intentionally at-least-once. Slow parsing may require redelivery. HA needs a genuine atomic acceptance+journal persistence change, plus reconciliation tests. Long AI outages can still exceed Cloud retention.

## Alternatives considered

1. **Exact claim replay with request ID and deadline** — rejected for v1 as disproportionately complex for low-volume import, not required for at-least-once safety.
2. **ACK after raw source receipt into a dedicated local inbox** — deferred because it introduces new sensitive-content storage, quotas and cleanup; consider if offline retention through long AI outages becomes a product requirement.
3. **ACK tied to ongoing pending/handled lifecycle** — rejected because user review can remove pending records independently of transport confirmation and forces transport recovery to reconstruct mutable review state.

## Required acceptance evidence

- Production HA store integration tests for atomic acceptance+journal commit (including injected save failures and crash/restart), not merely reference JSON fixtures.
- Private Cloud integration tests for claim contention, re-lease token rotation, duplicate ingestion, ACK idempotency, owner isolation, source expiry, and new ID on re-ingestion.
- A real end-to-end text-only source test: lost claim response, lost ACK response, restart after commit, completed review before ACK retry, and eventual re-lease, plus never ACK an unpersisted outcome.
- Binary attachment integrity/size/privacy tests before enabling attachment delivery.
- HACS/Hassfest, Python and HA compatibility CI; the mutation gate at merge readiness per repository instructions.

## Rollout / versioning

PR #85 is **not merged**, so rewrite its proposed H3 v1 contract instead of publishing a breaking v2. Adjust issue #84's acknowledgement text explicitly to reflect the atomic journal acceptance criterion. Keep Cloud #12 internal only; public endpoint activation is blocked until the accepted public contract, Cloud conformance, credentials, scheduled cleanup and live HA adapter tests are complete.
