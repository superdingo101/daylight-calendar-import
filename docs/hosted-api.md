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
