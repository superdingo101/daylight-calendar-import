# P02 credential recovery and local disconnect

These modules are dormant on `integration/hosted-v1`. They do not register a
service, expose settings, select a parser or start a polling task. The accepted
[account contract](hosted-account-api.md) governs every remote request.

The controller requires one loaded `HostedCredentialStore` and the trusted-origin
`HostedLifecycleTransport`. Its result contains only connection state and an
optional credential class and verified effective expiry. Never serialize the store
snapshot into settings, WebSocket responses or diagnostics.

## Recovery

Saved candidates are checked using authenticated installation status. Only a
canonical unauthorized response permits the protocol's single confirmation
attempt, followed by status binding. Validate account/installation identity and
effective expiry before committing local activation. Preserve bundles on
transport/storage uncertainty. Verified overlap credentials preserve ordinary
access until their effective deadline; overlap is not current lifecycle authority.
Every authorized recovery result exposes the verified effective expiry as
`credential_expires_at`, including retention of a previous overlap credential
while a failed candidate remains staged. This status snapshot is not an extension
of server authority. Retaining a previous credential leaves its private recovery
bundles unchanged.
A candidate for a different installation is blocked pending explicit durable
journal reconciliation. This primitive cannot authorize namespace replacement.

## Disconnect

An explicit disconnect first commits `local_disconnect: unconfirmed`, stopping
local hosted work, before any network request. It verifies the current binding
and credential class, then attempts the bearer disconnect once. A matching
successful response permits the second checkpoint to remove consent, credentials
and verifier and record `local_disconnect: confirmed`.

| Durable marker | Recovery behavior |
|---|---|
| `none` | Verify saved credentials normally |
| `unconfirmed` | Keep hosted work stopped; no automatic disconnect or confirmation replay |
| `confirmed` | Keep hosted work stopped; remote success was recorded |

A lost response, cancellation, failed final checkpoint, 401/403, overlap token,
or missing bearer cannot prove remote disconnection. Retain recovery bundles
and require owner reconciliation in the account portal. Repeating local
`async_disconnect` does not repeat its POST. The controller never touches local
imports, review state, calendars or subscriptions. A future reconnect consumer
must read actual owner state and explicitly reconcile old namespaces; changing
this marker alone is not sufficient authorization to resume work.

## Persistence and rollback

The marker extends the existing private checkpoint envelope without moving or
copying secrets. The decoder accepts the earlier field sets (defaulting missing
optional markers) or the extended set. Malformed/unknown data still fails closed.
Old code cannot read a newly extended checkpoint: roll back code only while
hosted remains dormant, preserve the private file and use forward repair rather
than deleting or manually editing it. Storage uncertainty stops network work.

Pairing issuance, durable exchange intent, retry scheduling, native
consent/connect/repair settings and runtime registration remain follow-on P02
work. Live M2 evidence remains separate from unit/CI success.

## Explicit rotation and renewal intent

`async_rotate` is a dormant one-attempt operation, without a scheduler or native
settings registration. It first checks saved consent, the local stop marker and
absence of unresolved pairing/candidate recovery. A fresh status must match the
saved household/installation and identify a current credential. Overlap tokens
retain ordinary access and their effective expiry, but require owner repair for
lifecycle authority.

Before issuance, the private checkpoint saves `rotation_not_before`. A request
failure, cancellation, malformed candidate, or failed candidate write preserves
this gate and the previous credential across restart. No issuance POST is
transparently replayed. The 645-second scheduling gate covers the transport's
15-second request budget, the protocol's 600-second maximum staged lifetime,
and 30 seconds of scheduling skew. A slow intent checkpoint cannot consume that
margin and then issue with a stale gate. Once the gate elapses, only a new
explicit invocation with fresh server authority may attempt issuance; server
`rotation_pending` still rejects a candidate that remains live. Clock skew never
grants server authority.

A returned same-installation candidate is saved before reporting
`candidate_saved`. The existing reconciliation operation verifies/activates it;
rotation itself never confirms. Promotion clears the gate, and confirmed
disconnect removes it with all private bundles. Recovery continues to expose an
authorized previous connection while an uncertain issuance waits. An existing
candidate is retained for reconciliation instead of being overwritten by another
rotation.

The decoder accepts the earlier checkpoint shapes. Older code rejects the new
field, so preserve private files and repair forward rather than downgrading or
removing recovery material. This change neither activates hosted processing nor
changes local imports, review state, calendar writes, or journal namespaces.
