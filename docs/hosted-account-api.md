# Hosted account and installation lifecycle v1 (P01)

This document extends the [Hosted API v1](hosted-api.md). The account, pairing,
credential and installation schemas under `schemas/hosted/v1/` are public
contracts, not activated endpoints. Cloud implements them in C03/C04;
Infrastructure wires consumed services in I02; HA implements the client in P02.
No provider, database, deployment or billing implementation belongs here.
The existing parser and H3 source-delivery contracts remain unchanged.

## Transport and trust boundaries

All endpoints require HTTPS. JSON requests use `Content-Type: application/json`;
all JSON has `schema_version: 1`. Reject unknown fields, duplicate keys,
non-finite numbers, unpaired surrogates and unsupported versions using the
Hosted API's strict decoding rules. Limit lifecycle JSON bodies to 16 KiB;
reject larger bodies with 413 `source_too_large`. Responses, including errors,
MUST carry `Cache-Control: no-store`. Do not redirect authenticated requests.

Human endpoints below require an authenticated, verified household owner
session. Session validation, audience/origin checks, CSRF protection for
cookie-authenticated mutations, and recent re-authentication (at most ten
minutes old) for approval/replacement and owner disconnect are server requirements. A human session
MUST NOT authorize parse/delivery routes. Installation bearer tokens MUST NOT
authorize human account routes. Browser code and browser storage never receive
installation tokens or HA's pairing verifier. A browser approval code alone
never authorizes enrollment, credential exchange or installation access.

The server derives the household from its authenticated owner, and the
installation from its bearer credential. Clients cannot submit account IDs,
owner identities, forwarding addresses, alpha grants, capability flags or
credential lifetimes. Public IDs are opaque unguessable strings, not provider
or database identifiers. Unknown/non-owned resources return the same 404
`not_found` envelope/message; authenticate before lookup. Invalid credentials
return 401 `unauthenticated`. Disabled accounts cannot activate or use
installation credentials. Human sign-in and account recovery remain available
for an owner to inspect suspended access without granting capabilities.

Never log tokens, authorization headers, pairing verifiers, approval codes,
source data or raw upstream errors. Mask diagnostics; do not put secrets in
URLs, telemetry, browser history or referrers. Apply bounded global, network,
owner and installation admission/concurrency limits before expensive work;
include anonymous pairing creation and code guesses. Rate limits use 429
`rate_limited`, `retryable: true`, and a bounded integer `Retry-After` in seconds
(1–300). Clients honor it with jitter and bounded retry counts. Reject secrets
using constant-time verification. All identifiers/secrets must be generated
with a cryptographically secure random generator.

## Owner enrollment and account status

| Endpoint | Authority | Request | Success |
|---|---|---|---|
| `POST /v1/account/enroll` | Verified human session | `account-enroll-request` | 200 `account-status` |
| `GET /v1/account` | Verified human session | No body | 200 `account-status` |
| `POST /v1/account/pairings/approve` | Recently authenticated owner | `pairing-approval-request` | 200 `pairing-approval-response` |
| `POST /v1/account/installation/disconnect` | Recently authenticated owner | `account-disconnect-request` | 200 `installation-disconnect-response` |

Schema names in tables omit `.schema.json`. Enrollment is idempotent for the
same owner: concurrent/repeated requests produce the same household. Alpha is
invitation-only, free, bounded, deliberate and revocable. The server validates
a protected invitation bound to the verified owner before granting access;
the client cannot request a grant or reset its duration/limits by reconnecting.
An uninvited owner receives 403 `enrollment_unavailable`, not a trial or bill.
Enforcement must serialize concurrent enrollment and invitation consumption.
Future self-service enrollment requires explicit server policy activation;
this contract never silently starts paid access or grants repeated trials.

`account-status` exposes only opaque `account_id`, `status` (`active` or
`suspended`), nullable `active_installation_id`, and capabilities. An owner not
yet enrolled receives 403 `enrollment_required` on GET. There is one household,
one owner, one active HA installation and one forwarding address. A server
MUST enforce the installation limit transactionally, including concurrent
pairing, activation and replacement. A staged candidate is not an active
installation. Account status and capabilities are snapshots, not authorization
proofs; each operation rechecks them. `managed_ai`, `email_intake` and
`source_delivery` are independent booleans. They do not specify billing,
quotas or delivery-only drain; those semantics are P06 work. M2 may return
false for capabilities not yet activated.

## Pair HA without disclosing its credential to the browser

Before starting, HA obtains cloud-processing consent and durably stores a new
secret verifier (32 random bytes encoded as unpadded base64url: 43 characters).
Its S256 challenge is unpadded base64url(SHA-256(ASCII(verifier))), 43 characters.
The challenge is public; the verifier is secret. HA never sends the verifier
to the browser. Server comparisons MUST recompute the challenge.

| Endpoint | Authority | Request | Success |
|---|---|---|---|
| `POST /v1/pairings` | Anonymous, bounded admission | `pairing-start-request` | 201 `pairing-start-response` |
| `GET /v1/pairings/{pairing_id}` | `X-Daylight-Pairing-Verifier` header | No body | 200 `pairing-status-response` |
| `POST /v1/pairings/{pairing_id}/exchange` | Same verifier header | `credential-action-request` | 200 `credential-issued-response` |

A pairing lasts at most 600 seconds from creation, with immutable server expiry.
The start response contains `pairing_id`, a distinct approval `user_code`,
`verification_uri`, `expires_at`, and `poll_interval_seconds` (5–30). The code is
12 random uppercase letters/digits, independent of the verifier and pairing
ID. Codes are unique among live pairings; the service never treats code secrecy
as ownership. `verification_uri` is an HTTPS account-page URL on the configured
trusted account origin; it has no query or fragment or embedded credentials.
HA must validate this origin against its configured trusted service before
opening it, never follow an arbitrary returned URL. The user opens that page
and enters the code manually. No secrets are carried by a link.

The portal clearly identifies the proposed HA label as untrusted text, the
signed-in household, connect versus replacement, and loss of old installation
access. Approval requires explicit owner consent; do not approve automatically
on sign-in or link navigation. `pairing-approval-request` binds the user code,
`intent` (`connect` or `replace`), and `expected_installation_id` (null for
connect, the owner's current installation ID for replace). The portal obtains
that ID from the owner's authenticated account status. Approval atomically
binds the pairing to that owner, household, intent and expected prior identity.
An approved/exchanged pairing cannot be rebound or altered. A repeat identical
approval by that owner is harmless and returns the original pairing ID; any
changed binding returns 409 `pairing_conflict`. Other owners receive uniform
404, without learning the first owner or household. `connect` when already
connected is 409 `installation_exists`; stale replacement identity is 409
`installation_changed`. Never let an owner select another household's ID.

HA polls no faster than the advertised interval; states are `pending`,
`approved` and `exchanged`. An expired pairing returns 410 `pairing_expired`
with the correct verifier, including after exchange. Wrong/missing verifiers
return uniform 404 (before disclosing state/expiry). Polling an approved
pairing does not consume it. Exchange before approval is 409 `pairing_pending`.
Exchange atomically consumes the authorization exactly once, returning one
staged credential and its candidate installation identity. Repeated exchange
is 409 `pairing_consumed`, even with the correct verifier; no token replay.
Creation/approval/expiry alone never changes a working installation.

A lost creation response is abandoned to expiry. A lost exchange response
requires a fresh pairing and approval; it MUST NOT revoke or replace the old
installation. Do not transparently retry an exchange POST. Pending credentials
expire without activation, and cleanup removes abandoned secrets. Concurrent
exchanges issue at most one candidate. Pairing state stores no source content.

## Verify the candidate before activation

`credential-issued-response` includes `installation_id`, secret
`installation_token`, `expires_at`, and `confirmation_expires_at`. Tokens have
at least 256 bits of entropy. Token expiry is at most 90 days from issuance;
confirmation expires no later than 600 seconds after issuance or token expiry,
whichever is earlier. Servers store verifiers rather than plaintext active
tokens. The candidate token authorizes only the confirmation endpoint below;
it cannot parse, claim, ACK, read account status, rotate or disconnect.

HA saves the candidate securely before confirmation, retaining the old working
credential until activation is established. Confirm using the **candidate**
bearer token, proving possession over HTTPS. The API origin is HA's configured
trusted API origin, never one supplied by a pairing response.

| Endpoint | Authority | Request | Success |
|---|---|---|---|
| `POST /v1/installation/confirm` | Candidate bearer | `credential-action-request` | 200 `credential-confirmed-response` |
| `GET /v1/installation` | Active bearer | No body | 200 `installation-status-response` |
| `POST /v1/installation/credentials` | Active bearer | `credential-action-request` | 200 `credential-issued-response` |
| `POST /v1/installation/disconnect` | Active bearer | `credential-action-request` | 200 `installation-disconnect-response` |

Activation is atomic, after rechecking owner/account access, candidate expiry,
and the expected current installation under the same transaction. For connect,
the account must still have no active installation. For replacement, the
approved expected ID must still be current. At most one concurrent candidate
can win. A conflict returns 409 `installation_changed` without revoking the
winner or the old installation. Account suspension/revocation between approval
and confirmation fails closed. A credential issued under a revoked alpha
grant cannot later activate merely because that grant was restored; invalidate
pending authorizations when access is revoked.

A replacement uses a **new** installation ID. Only successful candidate
confirmation revokes all old-installation credentials and switches the active
installation. The household and forwarding address survive. The new ID never
authorizes the old ID's leases, ACKs or journal. The service atomically releases
old unconfirmed deliveries for fresh delivery under the new installation with
fresh delivery/source IDs and lease tokens, retaining immutable content and
original source expiry; never extend retention. Do not transfer confirmed ACK
tombstones or resurrect acknowledged payloads. HA explicitly reconciles the
old namespace before reconnecting: unresolved ACK obligations become observable
as disconnected/uncertain; existing imports/review stay local and untouched.
Cross-installation redelivery may require human reconciliation of already
accepted local imports; do not promise exactly-once calendar effects. Replacement
must not silently replay the old journal using the new token. Cloud must not
activate replacement against a nonempty delivery ledger until these remapping
and isolation guarantees are implemented/tested (C04 with C07/P02 integration).

After a lost confirmation response, HA uses its saved candidate on GET
installation status. An active result establishes that the candidate activated;
`credential_status: "current"` identifies a credential eligible for lifecycle
operations, subject to fresh authorization. A still-staged
candidate may receive either 401 or 403 from this active-bearer-only status route.
On either response, HA may retry confirmation once using the saved candidate.
If that still fails with 401/403, check the retained previous credential on GET
installation status before requiring human repair. A candidate may have expired
without activating while the previous connection remains fully usable.

An active previous credential must match its previously bound account and
installation IDs. Resume that authorized connection and explicitly show that the
previous connection was retained; never apply the failed candidate's binding or
replay a journal under another namespace. If a credential is `overlap`, it can
continue ordinary authorized operations until its effective expiry but cannot
perform bearer lifecycle changes. Owner sign-in can authorize a fresh pairing
to obtain a new current credential through HA's verifier-bound exchange; it
never retrieves an existing secret or gives a token to the browser. Owner
disconnect remains available without a working installation credential. Do not
strand authorized ordinary work while restoring current access.
Only if neither saved credential establishes an authorized matching connection
must HA stop hosted work and require fresh pairing/repair. On transient network
or server failures retain both secure bundles, back off, and keep confirmation
uncertain; do not declare either disconnected or activated from an error alone.

Confirming the same still-current active token is idempotent and returns its
original activation result, even after the candidate confirmation deadline.
A revoked/expired token never regains authority through confirmation. The
original token expiry is not extended by confirmation or retry. Never discard
the previous credential merely because a request timed out. No rollback
automatically reactivates revoked credentials.

## Rotation, expiry, disconnect and recovery

An active token may request a staged renewal/rotation using POST credentials.
It keeps the **same installation ID**, source identities and journal namespace.
Only one unconfirmed rotation candidate per installation is allowed: another
issuance receives 409 `rotation_pending` until confirmation/expiry. A lost
issuance response leaves the active token working; wait for that candidate to
expire before issuing another. No transparent issuance retries or plaintext
secret recovery endpoint. HA renews before expiry and checks returned server
deadlines, using bounded clock-skew tolerance only for scheduling; authority
always follows the server clock.

Confirmation proves the replacement token works before retiring the issuing
token. The old token remains valid for an overlap of at most 600 seconds,
bounded by its original expiry; the new token's 90-day maximum starts at its
issuance. During overlap the old token may continue ordinary authorized
parse/delivery operations, but MUST NOT issue another rotation or
disconnect/confirm so a stale client cannot revoke the new credential. Among
installation credentials, only the current one may issue rotations or
bearer-authorized disconnects; staged candidates may confirm themselves as above.
At confirmation lock time recheck that the issuing token remains current and
unexpired, the installation is still active, and the candidate is unexpired.
A disconnected/replaced installation or expired/revoked issuer cannot activate
a pending rotation. Confirmation and disconnect serialize. Retain only bounded
verification state for overlap/idempotent confirmation; never plaintext tokens.

Disconnect atomically revokes all active/staged/overlap credentials and disables
the installation's future API access. It does not delete local imports, review
state or calendar events; it does not cancel a subscription or delete an
account. The operation has an idempotent effect, but a repeat using a now-revoked
token receives 401. After a lost response, **401/403 does not prove server
disconnection**: the token may instead have expired, been superseded, or lost
permission before the first request committed. HA stops local hosted work and
shows remote disconnection as unconfirmed until the owner reconciles it. Invalid
tokens cannot disconnect any resource.

The owner signs in, reads GET account status, and uses the owner disconnect
endpoint with the explicitly selected `expected_installation_id`. That endpoint
authenticates ownership before lookup and transactionally revokes all credentials
only if this ID is the current active installation. A retry for the same owned,
already-disconnected ID returns the same 200 disconnected result without touching
any other installation. If another installation has since become active, return
409 `installation_changed`; require fresh explicit owner consent before selecting
that new identity. Unknown/non-owned IDs return uniform 404. A successful owner
disconnect or an authenticated account status with no active installation
confirms no active remote connection; a changed active ID requires explicit
reconciliation, not automatic revocation. HA records local disconnection
separately from remote confirmation. Reconnect always reads owner account status
and chooses connect versus replace from actual server state, never from a local
disconnected flag. The owner can recover through a newly approved pairing. The
account retains its forwarding address; disconnected deliveries remain bounded
by their original expiry, and resuming them uses replacement reconciliation,
never renewal of their lifetime. `active_installation_id` becomes null; a fresh
connect also reconciles every earlier disconnected installation namespace in that household
using the replacement rules above. Do not create customer intake in M2.

Expired/lost credentials require human sign-in and fresh pairing approval.
An expired bearer, an email header, a public installation ID or a lost browser
session is never an account-recovery credential. Human account recovery must
re-verify owner identity through the selected identity service. Reconnect,
replacement and reinstall never allocate another household grant or forwarding
address automatically. Revoking an account invalidates all of its candidates,
credentials and pairing authorizations. Local/BYO parsing and Direct IMAP remain
usable throughout outages, disconnect and suspended hosted access.

`installation-status-response` contains account/installation IDs, account status
(always `active`), capabilities, `credential_status` (`current` or `overlap`),
token expiry, nullable forwarding address and trusted HTTPS `account_url` on the configured account origin (no query/fragment/credentials).
An otherwise valid bearer for a suspended account receives 403 `forbidden`
before any installation status is returned; the human owner uses GET account
status to distinguish suspension and repair access. Invalid/expired/revoked
bearers receive 401. It contains no source bodies, human session, provider IDs,
invitation details, token value or private configuration. The address is nullable until provisioned;
HA must not invent one. `credential_expires_at` is the presenting token's
effective authorization deadline, bounded by its original expiry and any
overlap deadline. Status is a snapshot, never a grant of lifecycle authority
or an extension of credential expiry.

## Lifecycle errors and conformance

Errors use the existing `error.schema.json`; messages contain no secrets or
resource-existence hints. New codes are additive. Clients tolerate unknown codes
and use `retryable`, not message text, for decisions.

| HTTP | Code | Retryable | Meaning |
|---|---|---|---|
| 400 | `invalid_request` | false | Invalid schema/transport |
| 401 | `unauthenticated` | false | Invalid, expired or revoked authority |
| 403 | `forbidden` | false | Wrong credential class or insufficient authority |
| 403 | `enrollment_required` / `enrollment_unavailable` | false | Human enrollment needed/unavailable |
| 404 | `not_found` | false | Unknown or non-owned pairing/resource |
| 409 | `pairing_pending` | true | Poll at bounded interval; do not retry exchange automatically |
| 409 | `pairing_consumed` / `pairing_conflict` | false | Fresh pairing required |
| 409 | `installation_exists` / `installation_changed` | false | Re-read owner status and explicitly reconcile |
| 409 | `rotation_pending` | true | Wait for candidate expiry before new issuance |
| 410 | `pairing_expired` | false | Fresh pairing required |
| 429 | `rate_limited` | true | Honor Retry-After |
| 503 | `service_unavailable` | true | Bounded backoff; reconcile ambiguous mutations |

The public fixtures/schema tests and reference lifecycle tests demonstrate
contract consistency only. Private implementation acceptance additionally
requires real database contention, single-use approval/exchange, owner isolation,
CSRF/session verification, credential-response loss, expiry/rotation/disconnect
races and restart persistence. P02 must test real HA secure storage, candidate
recovery and explicit journal reconciliation. M2 exit additionally requires
live passwordless mail and deployed connect/restart/rotate/disconnect/reconnect.
