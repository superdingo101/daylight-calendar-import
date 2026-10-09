# Daylight Hosted API v1

This document and the JSON Schemas in `schemas/hosted/v1/` are the canonical public client/server contract between the open-source `daylight-calendar-import` integration and the private Daylight-managed service. Server database models, model-provider payloads, prompts, and internal queue formats are not part of this contract.

The hosted parser is a network realization of the same logical boundary as the local `ParserProvider`:

```text
SourceDocument + reference datetime + time zone
                    |
                    v
              hosted parser
                    |
                    v
                ParseResult
```

The hosted parser MUST NOT create or modify calendar events. Review, deduplication, conflict detection, lifecycle state, notification policy, and calendar writes remain downstream in Home Assistant.

## Versioning

The parser endpoint is `POST /v1/parse`, and all JSON request/response documents contain `"schema_version": 1`.

The schemas use JSON Schema Draft 2020-12. The files under `schemas/hosted/v1/` are normative for structural validation. This document adds transport and semantic requirements that JSON Schema cannot express.

A breaking change to required fields, field meaning, transport mapping, or validation semantics requires a new API major version. Server implementation details may change without a version bump when the v1 observable contract is preserved. Clients MUST treat an unknown error `code` as a generic error and use the stable `retryable` field rather than assuming the known-code list is exhaustive.

## Authentication and request identity

Production hosted parsing requires a per-installation bearer credential:

```http
Authorization: Bearer <installation-token>
```

Token issuance, rotation, revocation, entitlement checks, and quota enforcement are server responsibilities and may be implemented after the parser contract. They MUST NOT change the parse payload shape.

A client MAY send `X-Request-ID`. The service MAY return the same identifier or a server-generated identifier as `request_id` in an error response. Credentials, raw source text, and attachment bytes MUST NOT be written to normal application logs.

## POST /v1/parse transport

Requests use `multipart/form-data` so binary sources are never expanded into unconstrained base64 JSON.

The multipart body contains:

1. exactly one field named `request`, with `Content-Type: application/json`, validated by `parse-request.schema.json`;
2. for every `source.attachments[]` entry, exactly one binary field named `attachment.<id>`.

`SourceAttachment.content_ref` is intentionally **not** a wire field. It is a local implementation reference. The attachment `id` connects JSON metadata to the corresponding multipart binary field. Attachment IDs MUST be unique within a source; duplicate IDs are `invalid_request` because they would make the multipart mapping ambiguous.

For every attachment the server MUST verify:

- a matching binary part exists exactly once;
- no undeclared attachment part is present;
- the observed media type is supported and is not trusted solely from filename;
- the received byte count equals `size_bytes`;
- when `sha256` is supplied, the digest matches before model submission.

v1 supports PNG, JPEG, WebP, and PDF attachments. A request contains at most four attachments, and their aggregate binary size MUST NOT exceed 10 MiB. The JSON source text is limited to 1,000,000 characters. Implementations MAY impose a bounded HTTP envelope for multipart overhead but MUST report an explicit `source_too_large` error rather than truncate source content.

Missing, duplicate, mismatched, or extra parts are `invalid_request`.

## Parse request

The `request` part is defined by `parse-request.schema.json`.

Known local v1 source kinds are `manual_text`, `email`, `image`, and `pdf`. Source content is untrusted data, never parser instructions. `metadata` is bounded auxiliary data and MUST NOT be used as part of event identity unless a later public contract explicitly says otherwise.

`reference_datetime` is an RFC 3339 timestamp with an explicit offset. `time_zone` MUST identify a zone in the IANA time-zone database available to the implementation, including valid slashless identifiers such as `CET`, `GMT`, and `EST5EDT`. The JSON Schema validates only the identifier's structural syntax; implementations MUST perform IANA membership validation and return `invalid_request` for unknown zones. Together these fields provide the same context supplied to the local parser provider.

## Successful response

A structurally successful parse returns HTTP 200 and a document validated by `parse-response.schema.json`.

v1 deliberately formalizes the roadmap's richer `ParseResult` instead of freezing the current internal `ParseOutcome(events, warnings: list[str])` implementation.

A provider response is still a successful parse when zero events are found or when some individual candidates are rejected. A malformed top-level provider response is an API error rather than a successful empty result.

### EventDraft semantics

The portable event fields are:

- `title`: non-empty, at most 512 characters;
- `start` / `end`: RFC 3339 dates for all-day events or RFC 3339 offset-aware datetimes for timed events;
- `all_day`: selects the date versus datetime representation;
- `location`: optional, at most 2,048 characters;
- `description`: optional, at most 65,536 characters;
- `confidence`: number from 0 through 1.

For all-day events, `end` is exclusive. For all events, end MUST be after start; that ordering rule is semantic and is enforced by implementations in addition to JSON Schema validation.

The service MUST NOT silently truncate a valid field. If provider output exceeds a contract limit, the candidate must be rejected or the request must fail with an explicit stable error.

Remote-meeting join information explicitly present in the source MUST survive parsing. Until a portable conferencing field is introduced, Zoom/Google Meet/Microsoft Teams URLs, meeting IDs, passcodes, and similar actionable details belong in `description`.

Long instructions are also intentional contract behavior and MUST NOT be silently truncated within the documented limit.

### Warnings and rejected candidates

Warnings are machine-readable `DraftWarning` objects. Warning codes are stable identifiers; messages are explanatory text and MUST NOT be parsed for control flow.

`RejectedCandidate` records individual candidates that were understandable enough to isolate but were unsafe to return as `EventDraft` values. Rejecting one candidate MUST NOT discard otherwise valid candidates.

`provider_metadata` is optional and provider-specific. It MUST NOT contain credentials or source secrets, and downstream Home Assistant behavior MUST NOT depend on provider-specific keys.

## Errors

Non-success responses use `error.schema.json`.

Known v1 error codes and normal HTTP mappings are:

| HTTP | Code | Retryable meaning |
|---|---|---|
| 400 | `invalid_request` | false |
| 400 | `unsupported_source` | false |
| 401 | `unauthenticated` | false |
| 403 | `forbidden` | false |
| 403 | `entitlement_required` | false |
| 413 | `source_too_large` | false |
| 429 | `quota_exceeded` | depends on reset policy |
| 429 | `provider_rate_limited` | true |
| 502 | `provider_invalid_response` | normally true |
| 503 | `provider_unavailable` | true |
| 504 | `provider_timeout` | true |
| 500 | `configuration_error` | false |
| 500 | `internal_error` | normally true |

Provider-specific exception classes and raw provider payloads are not public API. The hosted service maps them into stable domain errors.

## Compatibility and ownership rules

- These public schemas are canonical until a separate public protocol repository is justified.
- `daylight-cloud` implements and contract-tests against these schemas; it does not own a divergent copy of EventDraft semantics.
- The Home Assistant client uses only public contract fields and opaque server identifiers.
- Cloud persistence/database fields are private implementation details.
- Managed AI routing, credentials, retries, quotas, billing, and hosted ingress do not alter downstream local review/calendar semantics.
- The free/BYO parser path remains functional without Daylight Cloud.

Fixtures in `tests/fixtures/hosted/v1/` are executable examples of the contract. Normal CI validates both the schemas and representative valid/invalid documents without contacting Daylight Cloud or a paid AI provider.


## H3: Hosted source delivery (public v1 contract)

[Accepted ADR-0007](adr/ADR-0007-hosted-delivery-v1.md) is authoritative. This contract is not an active service. It defines at-least-once source delivery; review and calendar writes remain exclusively in HA. This unshipped v1 replaces the previous proposal without compatibility shims.

### Authorization, privacy and decoding

Every request MUST use HTTPS and `Authorization: Bearer <installation-token>`. Authenticate and check hosted-ingress entitlement before resource lookup. The authenticated installation is the sole tenant selector; no installation/customer selector is accepted in the URL, query or body. Delivery, attachment and ACK resources MUST be installation-scoped. Unknown and non-owned resources receive the same HTTP 404 `not_found` envelope, including the same message (safe correlation `request_id` may vary). Invalid/revoked credentials receive 401; insufficient permission receives 403, even for previously confirmed ACKs. Credential rotation within the same installation preserves delivery identity and journal obligations; another installation's credential never authorizes them. Rebinding an HA config entry to another installation requires explicit reconciliation, never silently replaying its journal under the new account.

H3 JSON uses `schema_version: 1` and the `delivery-*.schema.json` schemas. Errors use the existing `error.schema.json`. Reject duplicate JSON keys, non-finite numbers, unpaired Unicode surrogates, unknown fields and unsupported versions before evidence hashing. Timestamps are explicit-offset RFC 3339; servers should emit UTC `Z`. IDs and lease tokens are opaque, unguessable, bounded strings. Never expose internal database IDs, upstream mailbox identifiers, storage URLs, credentials or model metadata. Credentials, lease tokens and source/attachment bodies MUST NOT appear in normal logs, telemetry, diagnostics or activity UI. All JSON and binary responses, including errors, MUST have `Cache-Control: no-store`; shared caches MUST NOT retain private payloads.

### POST /v1/sources/claim

```http
POST /v1/sources/claim
Authorization: Bearer <installation-token>
Content-Type: application/json

{"schema_version":1,"limit":20}
```

The request accepts only required `schema_version` and optional integer `limit` (default **20**, range **1–50**). HTTP 200 returns `{"schema_version":1,"deliveries":[...]}`. Each batch has at most the requested limit and unique `delivery_id` and `source_id` values. A zero-item result means no currently claimable work, not permanent exhaustion. Drain the queue with successive bounded claims; there is no cursor, client claim-request ID, request deadline or exact claim-response replay.

Claims atomically acquire exclusive bounded leases, with a fresh cryptographically unpredictable token per re-lease. The server measures the grant using its authoritative current clock after acquiring necessary locks, rechecks eligibility/retention then commits the leases together. A lease expires strictly after that grant and no later than **120 seconds** after it or `source_expires_at`, whichever is earlier. The server MAY decline a near-expiry claim rather than issue a shortened lease. Concurrent claims MUST NOT acquire the same active delivery. A crash before commit leaves no lease; a crash or lost response after commit leaves an unseen lease that expires normally.

HA MUST disable transparent automatic retries of this POST. After an ambiguous response loss, make a later new claim; do not attempt to reproduce the lost batch. Back off on transient failures. Unseen deliveries become eligible again after their leases expire, unless retention has ended. Slow parsing may outlast a lease: persist a successful eligible outcome, then reconcile redelivery rather than ACK a stale token. Clients may choose smaller batches. There is no lease renewal in v1.

Every delivery includes stable `delivery_id`, `source_id`, `source_expires_at`, the current `lease_token`, `lease_expires_at`, and `source`. The entire normalized source JSON and attachment bytes MUST remain immutable throughout that delivery, including re-leases. Only lease fields change. IDs MUST never be reassigned to other content, even after expiry/cleanup. Duplicate upstream ingestion MUST NOT change content or extend retention of an existing delivery; changed content requires new IDs or an explicit rejection. Re-ingestion after expiry is a new delivery with fresh IDs and a new enqueue timestamp, never revival of an expired row.

Source expiry is immutable and strictly after durable Cloud enqueue, at most **604,800 seconds** later. Email `received_at` is not enqueue time. Cloud MUST reject out-of-range retention and MUST NOT claim expired sources. HA MUST reject without ACK an already-expired delivery or one whose expiry exceeds seven days from claim receipt plus at most five minutes of bounded clock skew. This allowance does not extend Cloud retention. HA MUST reject malformed, over-limit or unknown-version responses, never interpret them as an empty queue.

### Delivered source and local identity

A source is normalized email data: `kind: "email"`, `received_at`, optional title/text, attachments and metadata containing optional `sender` only. At least nonempty text or an attachment is required. No raw headers, mailbox credentials, upstream IDs, model output, signed download URL or local file reference is exposed. Attachment IDs MUST be unique within the source; descriptors contain supported media type, byte count, SHA-256 and optional filename. Aggregate attachment size MUST NOT exceed 10 MiB (at most four files). Cloud validates this before enqueue; HA validates before acceptance. Source routing aliases are resolved locally.

HA uses `hosted:<local-config-entry-id>:<delivery_id>` as `SourceDocument.upstream_source_id` for namespaced deduplication. `source_id` is correlation metadata only; neither ID grants calendar authority. Persist the namespaced source fingerprint separately from the SHA-256 of the complete canonical normalized source. A fingerprint alone proves neither successful acceptance nor content integrity.

### Canonical source evidence serialization

Canonicalization operates on the strictly decoded, schema-validated `source` object, preserving absent versus explicit null fields, array order and string contents without Unicode normalization. Object keys are sorted recursively by Unicode scalar value (not locale or UTF-16 order); no whitespace is emitted outside strings. Objects/arrays use ordinary JSON delimiters, booleans use `true`/`false`, and null uses `null`. Integer-valued numbers use base-10 digits with a minus sign only for negative values, no leading zeros, fraction or exponent; zero (including negative zero) is `0`. Thus a wire size of `1024.0` canonicalizes as `1024`.

Strings and keys use double quotes. Escape quotation mark and backslash as `\"` and `\\`; escape U+0008, U+0009, U+000A, U+000C and U+000D as `\b`, `\t`, `\n`, `\f` and `\r`. Every other U+0000–U+001F character uses exactly six ASCII characters `\u00xx`, with lowercase hexadecimal digits. Emit every other Unicode scalar literally in UTF-8, including `/`, U+007F, U+2028, U+2029 and non-ASCII characters. Never escape `/`, emit a BOM, or escape other scalars as `\u` sequences. Unpaired surrogates are invalid. These rules uniquely determine the bytes; the canonical vector in `delivery-evidence-canonical-vector.json` includes quotes, backslashes, slashes, all control characters and non-ASCII scalars.

### GET /v1/sources/{delivery_id}/attachments/{attachment_id}

An attachment may be downloaded only using the installation bearer credential **plus the currently issued lease token** in the `X-Daylight-Lease-Token` HTTP header. Both opaque path IDs must belong to the installation-scoped delivery, and the token must match its active unexpired claim. Neither a delivery ID nor a lease token alone authorizes a different installation. Use the exact same path-safe `attachment_id` advertised in the descriptor (URL percent-encoded as needed); any unknown delivery/attachment is 404, and a stale/expired lease is HTTP 409 `lease_not_current`. No cross-origin redirects or attacker-controlled download hosts are permitted.

Successful HTTP 200 returns **raw binary** with matching `Content-Type` and `Content-Length`, not JSON/base64. Enforce the advertised `size_bytes` and `sha256` on the client before entering the local review pipeline; a mismatch is an incomplete/failed ingestion, **never** a reason to ACK. Supported media are PNG, JPEG, WebP and PDF, at most 4 files and 10 MiB total per delivered source. Fetching may be repeated within a lease. No attachment-fetch endpoint creates a calendar event.

### POST /v1/sources/{delivery_id}/ack

```http
POST /v1/sources/<delivery_id>/ack
Authorization: Bearer <installation-token>
Content-Type: application/json

{"schema_version":1,"lease_token":"<persisted-current-token>"}
```

HTTP 200 uses `delivery-ack-response.schema.json`: `status: "acknowledged"`, stable `delivery_id` and original `acknowledged_at`. Cloud neither receives nor inspects HA import IDs or review state.

**HA MUST atomically commit an eligible durable import outcome and an independent private ACK journal entry in one logical durable transaction before ACK.** Eligible outcomes are `pending` with recoverable events, successful `no_events`, and successful `duplicate`. Failed, incomplete, volatile or uncertain outcomes are ineligible. The existing bounded seen-source history also records terminal failures; it MUST NOT alone authorize a hosted duplicate outcome or ACK. Duplicate acceptance requires evidence of a prior successful durable import for the same immutable delivery. No ACK after merely downloading, starting parsing, or parsing successfully before saving. On uncertain storage completion, recover the transaction before ACK. Two separate file writes are not an atomic transaction.

The journal is keyed by `(local_config_entry_id, delivery_id)` and durably contains:

- raw opaque delivery ID and local entry namespace, namespaced source fingerprint, complete `source_evidence_sha256`, immutable source expiry;
- original attachment descriptors, canonical `{"attachments":[...]}` digest, and verified attachment IDs, byte counts and SHA-256s (explicit empty descriptors/manifest for text-only sources);
- secret lease token, immutable `acceptance_disposition` (`pending | no_events | duplicate`), `ack_status` (`pending | confirmed | expired`), safe attempt count and last-attempt time.

Before acceptance, HA verifies every attachment's byte length, SHA-256 and supported content type against its descriptor. Verification must be complete before recording the journal. Store no raw source bytes solely for ACK retries. Journal persistence and integrity validation must reject incomplete/corrupt records and mismatched namespaces. A pending-import reference is diagnostic only after acceptance. Review approval/rejection, removal of pending events, and dedup/history compaction MUST NOT delete or invalidate an unresolved journal obligation. There is no transport `handled` disposition or pending-to-handled recovery transition.

After a lost ACK response or restart, retry the same token from the durable journal without downloading source bytes again or requiring a surviving pending import. A new claim/token requires comparing the freshly supplied source, identity, expiry and descriptors against persisted evidence, validating any needed attachment bytes, then atomically advancing the journal token before ACK. Reuse the accepted local outcome rather than parsing/writing duplicate events. Reject changed evidence without ACK. Serialise acceptance/reconciliation for a delivery so concurrent workers cannot overwrite newer tokens or duplicate acceptance.

Cloud ACK is atomic and installation-scoped. An unconfirmed ACK succeeds only with the current active unexpired token, evaluated using a fresh clock after required locks. Expired/replaced tokens return 409 `lease_not_current` without disturbing the current lease. A retry of the same already-confirmed token returns the original success strictly before source expiry, even after lease expiry or payload cleanup. Cloud retains a minimal tenant-scoped ACK tombstone (delivery ID, confirmed-token verifier, original acknowledgement timestamp and immutable expiry) through that deadline. Tombstones do not authorize attachment fetches. At or after source expiry, all ACKs return the uniform 404 `not_found`, including confirmed retries; check expiry before returning idempotent success.

HA durably marks confirmation before compacting the journal, subject to independent dedup needs. An unconfirmed journal reaching source expiry MUST become an observable terminal failure; never silently mark confirmed or ACK a stale expired entry. Loss of the ACK response can leave confirmation uncertain at retention expiry; surface that uncertainty without undoing an accepted local import. Transport confirmation never means user approval or calendar write.

### Failures and implementation ownership

| HTTP | Code | Client action |
|---|---|---|
| 400 | `invalid_request` | Fix invalid input; do not retry unchanged |
| 401/403 | `unauthenticated` / `forbidden` / `entitlement_required` | Restore authorized access before retry |
| 404 | `not_found` | Check known expiry; do not enumerate resources |
| 409 | `lease_not_current` | Await fresh delivery and durably reconcile its token |
| 413 | `source_too_large` | Reject explicitly; never truncate |
| 429 | `quota_exceeded` / `rate_limited` | Bounded backoff when retryable; honor Retry-After |
| 500/502/503/504 | existing v1 domain errors | Bounded backoff when retryable; ambiguous claims become later new claims |

ACK permits early payload cleanup; it does not promise immediate deletion. Unacknowledged bytes must remain available until immutable expiry, and cleanup must enforce the seven-day retention bound. A scheduled, verified private retention worker is an activation prerequisite. Credentials must not leak or resurrect another installation's sources.

| Work | Owner and acceptance evidence |
|---|---|
| Public ADR, schemas, normative examples and reference semantics | This PR; normal CI, exhaustive review and final mutation gate |
| Atomic HA outcome + journal storage | Follow-on `daylight-calendar-import` PR; real Store save-failure/crash/restart tests, review completion and compaction independence |
| Opt-in HA poll/fetch/ACK adapter | Follow-on public PR; secure configuration, integrity checks, token reconciliation and expiry observability |
| Queue corrections and HTTP auth/claim/ACK/attachment service | Private `daylight-cloud`; PostgreSQL contention, lost responses, stale tokens, expiry, owner isolation and fresh IDs after expiry |
| Verified retention scheduling and text/binary end-to-end conformance | Private Cloud with HA; mandatory before activation |
| PostgreSQL, sealed credentials, DNS/email and monitoring | `daylight-infrastructure` |

Cloud #12 remains internal only. Its expired-source upsert must allocate fresh identity/creation time; confirmed ACK retry must enforce expiry even before purge, and ACK admission must use a current lock-time clock. Its internal default/batch limit and 1 MiB payload cap must be reconciled by the future HTTP adapter with the public limits without silent truncation. Do not copy its private payload format into this contract.

Public reference examples validate schema and protocol consistency, not absent production persistence/server behavior. The future journal suite belongs in its implementation PR, not fabricated saved/recovered dictionaries here. Before activation require real text-only end-to-end tests for lost claim/ACK responses, restart after commit, review completion before retry and eventual re-lease, never ACKing an unpersisted outcome; then binary integrity/size/privacy tests. Near-expiry/offline or prolonged parser outages can exceed retention; v1 does not guarantee import beyond seven days. Exactly-once, claim replay, cursors, raw local inbox and lease renewal are explicitly deferred.

**No hosted endpoint, HA runtime adapter or private Cloud service is implemented or activated by this PR. BYO/IMAP remains independent.**
