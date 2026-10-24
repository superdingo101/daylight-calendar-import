# P02 private credential checkpoint prerequisite

`hosted_credentials.py` provides an unactivated persistence primitive for the
future native Home Assistant connection controller. It performs no HTTP request,
adds no settings or signup UI, and changes no config entry, pending imports,
calendar events, parser selection or delivery ACK journal. The public P01
contract in [hosted-account-api.md](hosted-account-api.md) remains authoritative.

## Checkpoint contract

Create one `HostedCredentialStore(hass, entry_id)` for the controller and await
`async_load()` before any hosted operation. An absent file produces an empty
checkpoint. Existing data must have the exact supported HA envelope and closed
checkpoint/bundle fields; malformed, unknown-version, oversized and previously
renamed-corrupt files fail closed without moving, replacing or logging their
contents. Each entry has an independent storage key. Loading an instance twice
is rejected; deliberate recovery creates a new instance after the condition
below is resolved.

The immutable checkpoint contains:

- A revision and affirmative processing-consent record. This is recovery data,
  not a fresh processing authorization or a substitute for native consent UI.
  A stored `true` never reenables processing after withdrawal or disconnect;
  the controller must persist the current processing/disconnect intent separately
  while retaining any recovery material needed for uncertain remote operations.
- An optional secret pairing verifier, saved before pairing creation.
- An optional current credential with its previously established account ID,
  installation ID and expiry. Its `(account_id, installation_id)` tuple is its
  journal namespace; it cannot identify a different installation's journal.
- An optional candidate token, installation ID, token expiry and confirmation
  expiry, saved before confirmation. P01 does not provide an account ID at
  issuance, so a candidate has no established account or journal namespace.

`async_checkpoint(expected, proposed)` requires the exact last snapshot and its
unchanged revision. It increments the revision itself, checks the durable file
against that snapshot under the shared per-entry lock, and atomically saves
before publishing a replacement snapshot. A stale caller or another instance
with stale disk state receives `HostedCredentialConflict`, without overwriting
newer data. Staging a candidate cannot change or remove the current credential.
The caller must retain the old bundle on every uncertain network result.

Credential deadlines accept the public transport's RFC3339 spelling, including
lowercase `t`/`z` and long fractional seconds within the bounded response size.
They are normalized to aware ISO datetime strings for bounded storage. Precision
beyond microseconds is truncated toward an earlier deadline; this never extends
credential or confirmation authority. Dataclass representations hide tokens,
verifiers and nested recovery bundles. The controller must never serialize a
checkpoint into browser-facing state, logs, diagnostics or config-entry options.

## Private immediate writes and uncertainty

Files use Home Assistant's private atomic writer (`private=True`,
`atomic_writes=True`), including owner-only file permissions. This is HA-local
plaintext credential storage, not a separate encryption system. Host access and
backups must protect it as they protect other Home Assistant credentials.
The dedicated Store subclass propagates write failures and writes immediately:
the generic HA Store may otherwise defer during shutdown or log a failed write
and return successfully. Neither behavior establishes a durable checkpoint.

IO/decoding failures raise a fixed `HostedCredentialStorageError` without raw
exceptions or private values. The affected instance blocks further snapshots,
loads and writes. No error establishes activation, revocation or disconnect.

Cancellation has two distinct recovery paths:

1. **The calling operation is cancelled.** Cancellation returns promptly and
   blocks that instance. Its HA-tracked persistence task continues owning the
   per-entry lock until the write actually settles. A new instance's load waits
   behind that task, then reads the resulting file. No delayed result is
   published by the blocked original instance.
2. **The persistence task or executor Future is cancelled.** An OS writer thread
   can still complete after its Future is cancelled, including during HA's final
   shutdown stage. The entry is therefore blocked across every instance for the
   remainder of that HA process. Recovery requires a full Home Assistant process
   restart and fresh disk load. Creating another Store instance does not bypass
   this protection. Other entries remain independent.

## Work still required before activation

This primitive does not authenticate server results, advance a lifecycle,
schedule renewal, decide which token works, or reconcile disconnected journals.
The next controller must verify candidate identity and server deadlines, obtain
and save consent/verifier before starting, checkpoint issuance before confirming,
and prove the account/installation binding before promotion. The controller now durably binds credentials to the configured trusted service/API
origins. Changing those origins requires explicit disconnect/re-pair and
reconciliation; never send a saved token to a newly selected origin merely
because its account/installation namespace matches. The storage primitive itself makes no network request. The same
installation can retain its journal namespace on rotation; replacement and
reconnect require explicit reconciliation of prior namespaces. No checkpoint
alone permits transferring or replaying ACK obligations.

Lost creation/exchange responses, candidate/previous credential recovery,
one-time confirmation retry, overlap restrictions and uncertain disconnect must
follow P01. Native settings, secure controller integration, repair, consent
withdrawal, masking, restart and live deployed lifecycle rehearsal remain M2
gates. Local/BYO and Direct IMAP remain available throughout this future work.

Tests use the actual HA atomic private disk writer, not only the default test
fixture's in-memory Store. They cover restart, file permissions, candidate and
previous bundles, entry separation, disk revision races, malformed original
bytes/envelopes, real delayed-writer cancellation, fail-closed recovery and
secret-free representations/errors.

The checkpoint now optionally contains a closed `service_binding` bundle with
canonical `api_origin` and `account_origin` fields. The lifecycle controller saves
it with first-pairing consent/verifier and enforces it before saved material is
sent. Older shapes load as unbound and require explicit reconciliation before
network use; they are never automatically labelled with a new destination.
See [controller service binding](hosted-connection-controller.md#trusted-service-binding).
