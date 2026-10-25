# P02 durable lifecycle automation policy

This prerequisite defines bounded operation admission but registers no timer,
WebSocket command, parser or live service. Runtime opt-in and scheduling are a
separate consumer. Public account/credential semantics remain governed by P01.

The private credential checkpoint can contain four independent automatic retry
budgets: status recovery, pairing polling, candidate recovery and rotation. Each
record contains a phase/identity SHA-256 scope digest, attempts and a not-before
gate. It is never a bearer credential or proof of authority. Records are frozen,
closed, bounded data inside the existing private atomic envelope; none enters
general snapshots, diagnostics, URLs or browser state. Changing phases or
restarting HA does not reset another phase's budget.

Reserve and save an attempt before delegating any operation. Status/candidate/
rotation sequences permit at most six attempts; a pairing permits at most 32
checks within its original immutable expiry. Honor the controller's own polling
gate and canonical Retry-After, adding positive jitter. Other transient errors
use bounded exponential backoff. A slow reservation write cannot authorize a
request from an elapsed gate. An impossible distant gate requires human attention
rather than a practically unbounded timer. A successful authenticated status may
reset its status-recovery budget with a five-minute restart throttle.

Plans require saved consent, no local stop and matching trusted API/account
origins. Only an already-saved live pairing can be polled. Creation is never
automatic, and an uncertain/consumed exchange is never replayed. Candidate
recovery always uses the existing authenticated identity checks. Overlap tokens
cannot renew. Verified current credentials become eligible seven days before
their effective expiry; expired or unverified state first requires status
reconciliation. A saved rotation intent without a candidate blocks further
automatic issuance even after its wait gate: a human may explicitly request a
fresh issuance using the existing controller, but automation does not replay it.
Working previous credentials and journal namespaces remain intact.

Candidate confirmation now has a durable request budget: at most the original
attempt plus one protocol-defined retry. Increment and atomically save before
each confirmation POST. Loss, cancellation, malformed response and failed status
binding retain that budget and all recovery bundles. Exhaustion still permits
GET status to bind an already-active candidate; otherwise check the previous
credential before requiring repair. No error alone proves activation/revocation.

Legacy candidate bundles without request history are conservatively considered
confirmation-exhausted; a valid active status can still recover them. New issued
candidates begin with a zero count. Legacy checkpoints without automatic metadata
remain readable. Older code rejects extended shapes: preserve private files and
repair forward, without clearing counters or editing provenance. Confirmed
disconnect clears the budgets with credentials and consent; local stop prevents
all automatic work even while remote disconnection is unconfirmed.
