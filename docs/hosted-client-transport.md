# P02 installation transport prerequisite

`hosted_transport.py` is the public integration's unactivated P01 installation
transport. It has no Cloud/provider implementation, customer signup, scheduled
poller, config-entry writes, calendar side effects or network calls at import.
Local/BYO and Direct IMAP behavior is unchanged.

The caller supplies an HA-owned aiohttp session and separately configured trusted
HTTPS API/account origins. API paths are fixed. Pairing verification and account
links must match the configured account origin; they cannot select API endpoints.
Responses are bounded to 16 KiB and 15 seconds, require JSON and `no-store`, and
reject redirects, duplicate keys, non-finite constants, invalid UTF-8/surrogates,
unknown response fields and unsupported schema versions. Error objects expose
only canonical code, HTTP status, retryability and bounded `Retry-After`, never
remote message text or request secrets. The session remains HA-owned.

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
