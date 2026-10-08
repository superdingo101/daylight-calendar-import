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


## H3: Hosted source delivery protocol (public v1 contract)

The endpoints in this section define the **canonical public wire contract** for a future, opt-in Daylight-hosted inbox. They are **not active services** merely because this document and the schemas are published. A live client requires a configured hosted installation (H2 authentication) and a separate HA sync adapter; server endpoints, hosted ingress, delivery ledger and tokens belong **only** in private `daylight-cloud`. Resources, Railway/PostgreSQL, DNS, queues and monitoring belong in `daylight-infrastructure`. The public `daylight-calendar-import` repository owns this contract, fixtures, client adapter and local review/dedupe/checkpoint/ACK sequence; the free/BYO Direct IMAP path remains fully independent. No hosted endpoint creates, edits or deletes calendar events.

### Authorization and versioning

Every request MUST use HTTPS with the installation credential in `Authorization: Bearer <installation-token>`; no installation ID or customer identifier is accepted as a tenant selector in a query, URL or body. Cloud MUST authenticate the installation, check hosted-ingress entitlement as applicable and scope every cursor, delivery, attachment and ACK lookup to that installation **before** inspecting the resource. A non-owner MUST receive the same non-enumerating 404 response as an unknown identifier. Authentication failure is 401; a valid token without permission is 403. Revoked credentials never work, including for duplicate ACK requests.

JSON responses and the ACK request use `"schema_version": 1`; malformed or unsupported versions are HTTP 400 `invalid_request`. The schemas are in `schemas/hosted/v1/delivery-*.schema.json`. Errors use the **existing v1** `error.schema.json` envelope. All timestamps MUST be explicit-offset RFC 3339 values; servers should send UTC `Z`. UTF-8 JSON with duplicate member keys, non-finite numeric values or unpaired Unicode surrogates MUST be rejected before canonical evidence hashing. Ids, lease tokens and cursors are opaque, unguessable, length-bounded strings; no server database keys, installation IDs, internal S3 paths, passwords or model metadata are wire fields. Tokens and source/attachment bodies MUST NOT appear in normal HTTP/application logs or telemetry; `X-Request-ID` is allowed for safe correlation. All H3 JSON and binary responses (including error envelopes) MUST use `Cache-Control: no-store`; clients MUST NOT cache lease tokens or private source payloads in shared HTTP caches.

### POST /v1/sources/claim — explicitly claim a bounded page

```http
POST /v1/sources/claim
Authorization: Bearer <installation-token>
Content-Type: application/json
Accept: application/json

{"schema_version":1,"limit":20,"cursor":"<opaque-cursor>"}
```

- The request is a JSON object validated by `delivery-claim-request.schema.json`. `schema_version` is required and equals 1; optional `limit` is an integer from 1–50 (default 20), and optional `cursor` is opaque. Duplicate JSON keys, non-integer or out-of-range limits, unknown keys and unsupported versions are `invalid_request`. The first claim omits `cursor`.
- `cursor` is optional, an opaque, installation-bound, tamper-resistant page continuation token (at most 1,024 characters). An invalid/expired/mismatched cursor is HTTP 400 `invalid_cursor`, with `retryable: false`. Clients restart enumeration at the **first page**, never treat the cursor as a persistent checkpoint, and never transform it.
- HTTP 200 uses `delivery-page.schema.json`: `deliveries` contains at most `limit` entries with **unique `delivery_id` and unique `source_id` values within each page**; `next_cursor` is either a continuation or `null`. An empty page with `next_cursor: null` is valid. The server MUST NOT return one source twice in a single page. These are mandatory semantic uniqueness checks in addition to the structural JSON Schema.
- Each claimed item provides stable `delivery_id` and `source_id`, a **new, cryptographically unpredictable `lease_token` per successful re-lease**, `lease_expires_at`, `source_expires_at` and a bounded `source`. **The entire normalized `source` object, including text, title, received time, metadata, every attachment descriptor (ID, filename, size, digest and media type), and its attachment bytes MUST remain immutable for the lifetime of that delivery, including across retries, lease expiry and new claim tokens.** Re-leasing may change only claim-specific fields such as `lease_token` and `lease_expires_at`; the source/delivery IDs and `source_expires_at` MUST remain stable. **Within an installation, a `delivery_id` or `source_id` MUST never be reassigned to different immutable source content, including after acknowledgement, source expiry or retention deletion.** Re-enqueueing an upstream reference with changed source content MUST use new identifiers or fail explicitly, not revive an expired row with new payload under old IDs. Stable redelivery of the *same* logical source MUST retain its identifiers. This identity guarantee is required even after Home Assistant compacts its stored history to only a deduplication fingerprint. A client MUST durably retain two distinct local proofs with every ACK-eligible checkpoint: an installation-namespaced `source_fingerprint` for identity **and** an independent `source_evidence_sha256` digest of the *entire normalized source JSON* (title, text, timestamp, metadata and all attachment descriptors, including size and SHA-256). The evidence digest is SHA-256 over UTF-8 JSON serialized with lexicographically sorted object keys, no insignificant whitespace, UTF-8 string characters instead of ASCII `\\u` escapes, and shortest decimal integer forms; the allowed source schema contains no floating-point fields. A client recovering any pending or terminal checkpoint MUST recompute this digest from the redelivered source and compare it before reusing the checkpoint or ACKing. It MUST revalidate fetched attachment bytes against the advertised size and SHA-256, as applicable. A missing/mismatched evidence digest is an explicit integrity failure and **never** ACK-eligible. Both proofs survive HA restart until confirmed ACK or source expiry; the ordinary deduplication fingerprint alone cannot prove that source contents were unchanged. These proofs are **local durable checkpoint metadata, not public wire fields**. Server issuance of a lease does **not** mean the HA side has persisted the source.
- Claiming is **state-changing** and therefore deliberately uses `POST`, not safe `GET`. Claims are **not idempotent**: a lost response may hide already-claimed sources until their lease expires. Clients MUST disable automatic HTTP/proxy retries for this request and MUST NOT immediately replay a timed-out claim as though it were safe; after ambiguous outcomes, back off or wait until the bounded lease expires before beginning a new first-page claim. A page claim leases available/expired items atomically, with exclusive leases, and MUST NOT expose actively leased or acknowledged/expired sources. A cursor traverses a bounded claim window, without promising exactly-once delivery or a permanent snapshot. A crashed client or expired lease becomes eligible for redelivery on a **new first-page claim**; each new polling cycle MUST omit `cursor`, and clients SHOULD avoid concurrent claims for the same installation.
- Servers MUST bound the number and total size of responses and enforce retention, quotas and rate limiting without silently truncating source content. Clients MUST reject over-limit pages and unknown schema versions and MUST NOT silently treat malformed responses as empty queues.

A polled `source` is transport-normalized data with `kind: "email"`, `received_at`, optional `title` and `text`, `attachments` and a metadata allowlist of optional `sender` only. At least nonempty text or an attachment is required. No raw SMTP headers, mailbox credentials, internal upstream IDs, AI model output or cloud delivery internals are exposed. Attachment descriptors contain ID, supported media type, byte count, SHA-256 and optional filename; **no download URL**, signed object-storage URL or local file reference is returned. **Attachment IDs MUST be unique within each delivered source**, even if the remaining descriptor fields differ. The sum of the advertised attachment sizes MUST be at most 10 MiB. These are mandatory semantic checks beyond JSON Schema (which cannot enforce uniqueness by a single selected object property); Cloud must reject ambiguous payloads before enqueue, and the HA adapter must reject such responses without ACK. The client maps this to its local `SourceDocument`/`SourceAttachment` contracts and resolves source routing aliases **locally**. **Local durable deduplication MUST use the stable `delivery_id` qualified by the local hosted config-entry/installation namespace as `SourceDocument.upstream_source_id`** (for example `hosted:<local-config-entry-id>:<delivery_id>`), not an unqualified `source_id`. This protects against collisions across hosted installations managed in the same HA store and preserves the same local fingerprint across redelivery. The `source_id` is opaque source-correlation metadata; its non-reassignment rule above remains mandatory for Cloud. Neither identifier grants a calendar destination or other authority.

### GET /v1/sources/{delivery_id}/attachments/{attachment_id}

An attachment may be downloaded only using the installation bearer credential **plus the currently issued lease token** in the `X-Daylight-Lease-Token` HTTP header. Both opaque path IDs must belong to the installation-scoped delivery, and the token must match its active unexpired claim. Neither a delivery ID nor a lease token alone authorizes a different installation. Use the exact same path-safe `attachment_id` advertised in the descriptor (URL percent-encoded as needed); any unknown delivery/attachment is 404, and a stale/expired lease is HTTP 409 `lease_not_current`. No cross-origin redirects or attacker-controlled download hosts are permitted.

Successful HTTP 200 returns **raw binary** with matching `Content-Type` and `Content-Length`, not JSON/base64. Enforce the advertised `size_bytes` and `sha256` on the client before entering the local review pipeline; a mismatch is an incomplete/failed ingestion, **never** a reason to ACK. Supported media are PNG, JPEG, WebP and PDF, at most 4 files and 10 MiB total per delivered source. Fetching may be repeated within a lease. No attachment-fetch endpoint creates a calendar event.

### POST /v1/sources/{delivery_id}/ack — durable checkpoint only

```http
POST /v1/sources/<delivery_id>/ack
Authorization: Bearer <installation-token>
Content-Type: application/json

{"schema_version":1,"lease_token":"<token-from-current-claim>"}
```

The request MUST validate against `delivery-ack-request.schema.json`. HTTP 200 returns `delivery-ack-response.schema.json` with `status: "acknowledged"`, the stable `delivery_id`, and stable `acknowledged_at`.

**The HA client MUST NOT ACK until a recoverable local source disposition is durably committed: a pending import or a terminal `no_events`/`duplicate` disposition containing both the namespaced source fingerprint and independent complete-source evidence SHA-256, or a re-delivery verified against that same durable checkpoint.** The proofs are required for **all** ACK-eligible dispositions. A successful zero-event parse does not create a pending import, but can be ACKed once the local `no_events` outcome and both proofs are durably saved. A duplicate outcome is likewise eligible after durable source dedupe completion. A volatile fetch, attachment download, started AI request, successful parse **before** persistence, or retryable/failed parse without an eligible durable disposition does not constitute this checkpoint. On uncertain local storage outcomes, do not ACK; recover/reinspect local store before retry. Retain the same source/delivery identity for local dedupe and lifecycle correlation. The client can retry an ambiguous ACK response without re-running an already committed AI parse **only after recomputing and comparing the saved complete-source evidence digest and verifying downloaded attachment bytes match their descriptors**. A failed parse without a durable successful or terminal `no_events`/`duplicate` disposition is **not** ACK-eligible; it remains retryable (or eventually terminal by explicit source expiry), not silently consumed. The cloud is authoritative only for transport delivery/ack status; local HA remains authoritative for review, lifecycle, routing and calendar writes.

Server ACK is atomic and installation-scoped: it succeeds **only** for the currently active, unexpired claim token for that delivery, except that a retry with the token of the **same already-confirmed ACK** returns the same HTTP 200 acknowledgement **strictly before `source_expires_at`**, even if that lease has elapsed and raw source bytes have been deleted. The Cloud service MUST retain an installation-scoped minimal ACK tombstone (delivery identity, a verifier of the confirmed lease token, original `acknowledged_at` and `source_expires_at`) until that expiry. These tombstones MUST NOT authorize fetching attachments or other deliveries; repeated ACK returns the **original** `acknowledged_at`. **At or after `source_expires_at`, every ACK, including a previously confirmed token, MUST return HTTP 404 `not_found`.** A client can recognize its own previously observed expiry using its persisted `source_expires_at`. Revoked installation credentials never gain idempotency access. An expired or replaced token MUST NOT ACK a re-leased source; return HTTP 409 `lease_not_current` and preserve the current lease. Expired, purged, unknown and non-owned identifiers all receive the same non-enumerating 404 `not_found` after authentication and installation scoping. The server MUST check expiry before replaying any confirmed ACK, whether or not it retains a minimal tombstone. This is an at-least-once protocol; duplicate polling/retries MUST NOT create duplicate local pending imports or calendar writes. The token is a per-claim ACK capability, not a blanket installation/source-fetch capability.

### Failure, retention and compatibility

| HTTP | `error.code` | `retryable` | Required client action |
|---|---|---|---|
| 400 | `invalid_request` / `invalid_cursor` | false | Fix malformed request / restart from first page for bad cursor |
| 401 | `unauthenticated` | false | Re-authenticate or disconnect the hosted account |
| 403 | `forbidden` / `entitlement_required` | false | Do not retry until access restored |
| 404 | `not_found` | false | Unknown, expired, purged or non-owned ID; inspect locally remembered expiry, never enumerate others |
| 409 | `lease_not_current` | true | Re-poll, recover locally and use a *new* lease token; never ACK old claim |
| 413 | `source_too_large` | false | Reject explicitly, do not silently truncate |
| 429 | `quota_exceeded` / `rate_limited` | true when transient | Back off using Retry-After if provided |
| 500/502/503/504 | `internal_error` / `provider_unavailable` / `provider_timeout` | true when transient | Retry with bounded backoff |

The service MUST retain unacknowledged source data until ACK or an explicit **bounded** `source_expires_at` TTL and MUST expose that expiry with each claim; expiration can occur despite retry and is a terminal observable failure, not silent success. ACK makes **source bodies and attachment bytes** eligible for early privacy cleanup, but **the minimal ACK tombstone MUST survive at least through `source_expires_at`**. ACK does not promise immediate deletion. A scheduler/purge for bounded expiration is a **private cloud activation prerequisite**, not defined or implemented by these public schemas. Client credential rotation/revocation must not leak or resurrect another installation's source. Cloud and HA contract tests MUST cover independent tenants, ID non-reassignment and namespaced local dedupe, claim/re-lease, stale ACK rejection, same-lease ACK idempotency after raw-source cleanup and strictly before expiry, and uniform post-expiry 404, cursor isolation, retention/expiry, 0/50+ pages, duplicate `source_id` and `delivery_id` per page, attachment integrity, durable `no_events` and duplicate outcomes, and recovery after persistence or network failure. Public fixtures in `tests/fixtures/hosted/v1/` are normative examples; private cloud tests MUST consume or compare against them before enabling HTTP endpoints.

**Activation gate:** this PR defines only the public protocol. Private `daylight-cloud` H3 endpoints, server-side auth, queues/leases/ACK state and hosted MIME normalization belong in the cloud repository and MUST stay inactive until contract-review acceptance, H2 auth and verified retention cleanup. An opt-in HA poll/ack adapter, secure token configuration and local durable checkpoint integration are follow-on implementation in `daylight-calendar-import` after the server implements the accepted contract. No billing, managed AI, hosted receiver, Railway/Postgres provisioning or private server code is added here.
