# P02 entry-owned lifecycle runtime

The hosted integration branch loads one `HostedConnectionRuntime` per HA entry.
The normal local pending-import store retains it as `hosted_runtime`; the existing
settings reload/unload lifecycle owns its admission and teardown. It does not
replace the pending-import store or change parser/calendar/IMAP behavior.

`hosted_service` is an optional closed options bundle containing explicit trusted
`api_origin` and `account_origin` HTTPS origins. No service is inferred. With no
bundle, setup makes no credential read or network request. Valid configuration
loads one private credential store and controller with the HA-owned HTTP session.
Credential loading has a five-second budget, including waiting for an earlier
private write lock. Load/configuration/storage failures produce hosted-only safe repair state rather
than disabling the entry's local/BYO or Direct IMAP paths. No HTTP operation runs
on setup, restart or reload. Authenticated recovery remains an explicit action.

An explicit consumer can invoke connect, check approval, reconcile, rotate or
disconnect. One action is admitted at a time; excess calls fail with a fixed busy
code rather than queueing. The controller retains its consent, intent, origin,
identity and journal checks. No mutation is retried. Cancellation preserves
uncertainty; the runtime does not infer disconnect or credential authority from
an interrupted operation.

General snapshots contain only entry ID, configured origins, fixed connection
state, verified credential class/deadline and an optional authenticated account
URL. They never contain a credential checkpoint, tokens, verifier, approval code
or instructions. The authenticated account URL comes from the public status
contract and has already passed the transport's trusted-origin check; clients
must not invent a private account route. Explicit connect may return transient
pairing instructions to its caller, separate from general status.

Admission starts paused and opens only after entry setup and sensor forwarding
succeed. Unload pauses admission before platform teardown. A failed platform unload may
resume the same runtime unless the existing settings guard keeps admission
closed. Successful unload/setup rollback stops it, cancels its active operation
and waits at most one second. HA retains ownership of a cancellation-resistant
operation in an entry-specific task set until it really ends; reload refuses to
load another credential controller while that operation can still mutate state.
Once the task set is empty, an explicit reconcile action may initialize the
replacement controller and verify saved state. It is admitted through the same
single-operation gate; it never replays issuance/exchange or grants authority
from a local deadline. Setup and status reads still make no request. Storage
failures during deferred load remain fail-closed. The credential store separately
serializes pending/uncertain disk writes.

Native admin-only settings/API, bounded automatic pairing/renewal scheduling and
owner-authorized reconnect remain separate consumers. This runtime does not
register services or hosted settings, select hosted parsing, start delivery
polling, provision resources, clear disconnect markers, rebind a service or
transfer another installation's ACK journal. M2 still requires live evidence.
