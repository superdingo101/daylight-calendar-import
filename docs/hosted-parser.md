# Hosted parser adapter

`DaylightHostedParserProvider` is the public H1/P03 parser boundary for the
canonical [Hosted API v1](hosted-api.md). This work is dormant: it does not select
the provider, change setup, activate a runtime, or submit content on startup.
A later runtime/UI PR owns those actions on `integration/hosted-v1`.

## Runtime contract

The entry owner supplies an immutable `HostedServiceBinding`, a synchronous
callback returning its loaded `CredentialCheckpoint`, and an `active()` callback
returning exactly `True` only while that same operation/runtime is authorized.
The callback must reject unloading, unverified credentials, pending lifecycle
work and a changed runtime generation. The adapter never opens a second
credential store or reaches through private runtime/controller attributes.

Before local attachment reads, before the POST, and before returning a result,
the adapter checks consent, exact service binding, the local disconnect marker,
absence of reconnect intent, and a current unexpired bound credential. It uses
only that credential. Candidate credentials never grant parsing authority.
Staging a renewal candidate can retain a still-working current credential;
changing the current credential, disconnecting or replacing the runtime during
the operation rejects the result. No credential appears in representations,
normal logs, source metadata, or user-facing error text.

The provider implements the existing `ParserProvider` protocol. Its
`async_parse(source, reference_datetime=..., time_zone=...)` returns
`ParseOutcome`, preserving domain validation and the local recurrence guard.
It does not perform routing, review persistence, calendar writes or fallback
selection.

## Source and transport limits

The provider accepts normalized text, PNG/JPEG/WebP images and PDFs. Canonical
limits are one million text characters, four attachments and 10 MiB aggregate
attachment bytes. Temporal context includes an explicit-offset RFC 3339
reference and a real IANA zone, including valid slashless identifiers.

Attachments can be read only from the integration's existing temporary
`media-source://media_source/<configured-alias>/daylight-...` references.
Files must have the staged image/PDF basename format, be regular files opened
without following a final-component symlink, and match the declared size,
content type and optional SHA-256. Arbitrary paths, remote URLs, encoded path
tricks and non-staged media files are rejected. The existing upload/IMAP owner
continues to own removal of those temporary files.

Every request is one multipart POST to the locally configured HTTPS origin.
There is one JSON `request` part and one `attachment.<id>` binary part per
attachment. Local content references, routing metadata and upstream source IDs
are excluded. Request JSON is bounded to 5 MiB; the source/attachment bounds are
enforced before submission. Redirects and compressed responses are refused.
The transport uses the Home Assistant-owned HTTP session and does not close it.

Parser responses have a separate 64 MiB maximum and a 120-second total deadline,
including reading the body. The larger response maximum accommodates v1's
bounded event descriptions and per-event warnings; it does not loosen the
lifecycle client's 16 KiB limit. Runtime integration must also bound concurrent
operations rather than treating a body-size bound as a concurrency limit.
Strict JSON decoding rejects duplicate keys, non-finite numbers, invalid UTF-8
and unpaired surrogates. Exact decimal decoding avoids rounding a fractional
confidence or rejection index into a valid value.

## Results and failures

The complete canonical `ParseResult` is validated before admitting drafts.
Unknown/missing fields, unsupported versions, oversized arrays/strings, invalid
dates, invalid confidence or an unsafe time range fail the response. Valid
partial results retain their returned events; global, per-event and rejected
candidate warnings become local review warnings. Raw rejected summaries and
provider-specific metadata are not retained or used for behavior. Meeting
links and descriptions within the public limits are preserved through the
existing domain normalization. A valid zero-event response is successful.
A recurrence warning or explicit repeated schedule in the source withholds
all new drafts under the existing local recurrence rule.

Public v1 has no typed `assumptions` or `uses_reference_time_zone` fields. The
adapter preserves explicitly supplied event offsets, including DST transitions
and events in another zone; it does not infer those BYO-only flags from warning
message text or provider metadata. Per-event warnings remain complete indexed
review warnings rather than being truncated into the smaller local temporal
assumption field. Richer temporal-inference metadata requires its own public
contract and Cloud implementation before an adapter can interpret it.

`HostedParseError` contains a safe code, HTTP status when known, retryability,
optional bounded `Retry-After`, and an `uncertain` flag. Remote messages and raw
response bytes are excluded from the error. Canonical unknown error codes are
preserved for generic handling. Timeout/connection failures indicate uncertain
outcome. A canceled request propagates cancellation and closes its response.

The transport never retries a POST. `X-Request-ID` is correlation only;
`retryable` is not idempotency or replay authority. After a timeout or lost
response, runtime/UI work must explain the uncertainty and require an explicit
new attempt until the later public idempotency contract is implemented.

## Validation scope

Dedicated parser and transport tests cover canonical text/image/PDF requests,
partial/long-description/remote-meeting fixtures, all-day and no-event results,
recurrence, credential/consent/service/disconnect/expiry gates, authority changes,
staged-file integrity/privacy, strict JSON/envelope limits, errors, cancellation,
and an actual loopback multipart request. These tests do not demonstrate a live
provider call, deployed Cloud parse permission, or the normal HA review flow.
Those remain the separately reviewed P03 activation/C05 end-to-end work.
