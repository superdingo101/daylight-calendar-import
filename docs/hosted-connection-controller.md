# P02 credential recovery and local disconnect

These modules are dormant on `integration/hosted-v1`. They do not register a
service, expose settings, select a parser or start a polling task. The accepted
[account contract](hosted-account-api.md) governs every remote request.

The controller requires one loaded `HostedCredentialStore` and the trusted-origin
`HostedLifecycleTransport`. Its result contains only connection state and an
optional credential class; never serialize the store snapshot into settings,
WebSocket responses or diagnostics.

## Recovery

Saved candidates are checked using authenticated installation status. Only a
canonical unauthorized response permits the protocol's single confirmation
attempt, followed by status binding. Validate account/installation identity and
effective expiry before committing local activation. Preserve bundles on
transport/storage uncertainty. Verified overlap credentials preserve ordinary
access until their effective deadline; overlap is not current lifecycle authority.
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
copying secrets. The decoder accepts exactly the legacy field set (defaulting to
`none`) or the extended set. Malformed/unknown data still fails closed.
Old code cannot read a newly extended checkpoint: roll back code only while
hosted remains dormant, preserve the private file and use forward repair rather
than deleting or manually editing it. Storage uncertainty stops network work.

Pairing issuance, durable rotation/exchange intent, retry scheduling, native
consent/connect/repair settings and runtime registration remain follow-on P02
work. Live M2 evidence remains separate from unit/CI success.
