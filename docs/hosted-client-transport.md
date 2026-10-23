# P02 installation transport prerequisite

`hosted_transport.py` is the public integration's unactivated P01 installation
transport. It has no Cloud/provider implementation, customer signup, scheduled
poller, config-entry writes, calendar side effects or network calls at import.
Local/BYO and Direct IMAP behavior is unchanged.

The caller supplies an HA-owned aiohttp session and separately configured trusted
HTTPS API/account origins. API paths are fixed. Pairing verification and account
links must match the configured account origin; they cannot select API endpoints.
HTTPS origins normalize omitted and explicit default port 443, preserving bracketed
IPv6 hosts and distinct nondefault ports. IPv6 literals use their canonical
address representation when comparing origins, so expanded and compressed forms
identify the same origin. Responses are bounded to 16 KiB and
15 seconds, with at most 32 headers and 4 KiB header-field/status-line limits.
Both supported HA versions pin aiohttp 3.14.3, which supports these request limits.
Automatic decompression is disabled, requests advertise `Accept-Encoding: identity`,
and any response `Content-Encoding` (including `identity`) is rejected before
reading a body. Thus compressed payloads cannot expand before the byte limit.
Responses require JSON and `no-store`, and
reject redirects, duplicate keys, non-finite constants, invalid UTF-8/surrogates,
unknown response fields and unsupported schema versions. Error objects expose
only canonical code, HTTP status, retryability and bounded `Retry-After`, never
remote message text or request secrets. Malformed JSON, encoding or schema errors
retain the received HTTP status; failures before headers have no status.
The session remains HA-owned.

No operation retries automatically. A retryable error indicates a condition, not
permission to replay an exchange, rotation or disconnect POST. The future
lifecycle controller must implement P01's operation-specific recovery.

Before calling `start_pairing`, obtain processing consent and durably save the
new verifier. Never put verifiers or installation credentials in browser-visible
state, URLs, diagnostics or logs. The challenge is safe to send; the verifier
must remain local. Poll no faster than the advertised interval.

The remaining P02 controller/settings PRs must persist candidates before
confirmation, retain previous credentials on uncertainty, validate account and
installation bindings, enforce server deadlines and renewal scheduling, and
reconcile disconnected journal namespaces explicitly. Status responses are
snapshots, not authorization. A failed confirmation or disconnect does not
establish remote state. Only after those pieces land should native settings
activate this transport. Real HA secure-storage/restart and deployed lifecycle
rehearsals remain M2 exit evidence.

The transport deliberately omits human account endpoints. Owner login,
enrollment, approval and owner disconnect belong to the separate account portal;
the browser must never receive HA's credential/verifier.

Forwarding addresses are opaque account-status data validated against P01's
published schema, not an SMTP-send validation rule. Its current JSON Schema
`email` format checker requires an `@` in a string; the schema also permits null
and limits strings to 254 characters. The transport preserves schema-valid
quoted whitespace, internationalized text and minimal addresses without applying
a narrower address regex or a new RFC validity policy. Future UI/mail consumers
must handle that value safely for their own context.

Account links also follow the canonical URI syntax: ASCII host/path characters,
valid hexadecimal percent escapes, and a lowercase `https://` response prefix.
RFC3339 timestamp offsets reject hour/minute overflow rather than normalizing it.
JSON numeric literals use exact decimal interpretation before schema validation:
`1.0`/`5.0` become their integral values, while fractional values are never rounded
into a supported version or polling interval. Normalization is limited to the
current lifecycle contract's small integral range (0–30), rejecting huge exponents
before any integer allocation.
