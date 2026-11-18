# Hosted durable parsing v2

This additive protocol is opt-in and retains v1's public request/result formats.
It must be published before Cloud enables the new endpoints. Private billing,
provider and deployment details remain outside the public protocol.

`POST /v2/parse` accepts the same bounded multipart `request` plus attachment
parts as v1, and a required UUID `Idempotency-Key`. X-Request-ID is correlation
only. The key is scoped to the authenticated account, survives credential
rotation, and binds the complete payload. An existing key with different bytes
returns HTTP409 `operation_conflict`; it never issues another provider call.

The binding digest is SHA256 of UTF8 JSON for `{request: REQUEST, attachments:
[{id,media_type,sha256}]}`. Attachment digests hash exact decoded bytes; entries
are sorted by id. JSON uses sorted object keys, ensure_ascii=false, no NaN, and
separators comma/colon with no whitespace. This defines transport normalization,
not equality of semantically similar sources. Identical keys cannot be recycled.

`GET /v2/parse/operations/{key}` returns the same operation envelope. An unknown
key returns404; only the original key and original payload may then be submitted.
`pending` and `uncertain` require recovery, never new provider attempts. A
successful zero-event result still settles one credit. Definitively failed
attempts settle no credit. A bounded replay expiry does not make the key reusable;
expired results require an explicit new parse operation. When a provider cannot
prove an ambiguous outcome, the service retains uncertainty until protected
reconciliation; elapsed time alone must not release the reservation or redispatch.

HA durably stores key, digest and original ephemeral request IDs/timestamps before
submission. It stores no source content or credential in this journal. Retrying
normalizes those fields to their original values and verifies the binding. Local
operation acceptance is recorded in the same immediate storage commit as review
items, including no-event outcomes. A recovered result cannot create duplicate
review items after ordinary deduplication history is reset. Explicit source
reprocessing uses a new operation identity. The journal is bounded at10000 keys;
full journals fail closed and require operator recovery rather than key eviction.

`GET /v2/account` publishes independent managed_ai, new_email_intake and
accepted_source_drain capabilities, plus trial/paid balance/deadline, grace,
cancellation and drain state. Quota exhaustion does not disable entitled email
or BYO parsing. After access ends, only sources accepted before the authoritative
cutoff drain until their original immutable expiry. Webhook receipts alone do
not constitute an owned accepted source; Cloud must specify the owner admission
point and retain it through receipt-to-source processing. Local review, accepted
imports, ACK recovery and BYO remain available. Trusted billing actions open the
configured account origin; provider/customer identifiers never authorize access.
