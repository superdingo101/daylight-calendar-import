# Explicit P02 reconnect prerequisite

The dormant reconnect primitive uses the existing public P01 pairing contract.
It adds no endpoint, provider session, service, native control or automatic work.
The account portal reads the authenticated owner's current installation, chooses
connect versus replacement from actual server state and obtains fresh explicit
owner approval. A local checkbox, old bearer failure or stored repair intent is
never that approval. Only the verifier holder can exchange the approved pairing.

## Durable local intent

`async_start_reconnect` requires fresh affirmative processing consent, the same
configured trusted service and a trusted namespace reconciler. It rejects an
unresolved candidate instead of discarding a secret that might have activated.
It obtains a local contract receipt, then saves a fresh verifier and a closed
`ReconnectIntent` before one pairing-creation request. The intent records the
retained prior account/installation namespace when known, unchanged local stop
marker and the supported journal contract. Prior current credentials and stop
markers remain in place. The prior namespace is never renamed into the new one.

A confirmed-stop checkpoint may contain new pairing/candidate recovery material
only with a structurally consistent reconnect intent, affirmative consent and
trusted service binding. Ordinary pairing/rotation cannot overwrite such an
intent. Automatic work must remain disabled whenever an intent is present,
even if the retained prior stop marker is `none`.

A lost/cancelled creation is abandoned to expiry; an uncertain exchange is never
replayed. A fresh explicit reconnect starts another verifier and requires another
owner approval. The same polling gates, immutable expiry and durable exchange
intent apply. Approval display instructions remain transient. Checkpoint formats
without `reconnect` load with no repair authority; unknown/malformed fields fail
closed. Older code cannot read the extension: preserve files and repair forward.

## A receipt is not remote authority

The controller defaults to denying namespace transitions when no reconciler is
configured. A saved contract name is not a receipt. A reconciler must revalidate
the actual supported local store before creation, polling/exchange, candidate
confirmation and final promotion. The current `pending-v2-local-only` adapter
recognizes the actual pre-delivery production format; it does not assert that an
arbitrary future ACK journal is empty. Unknown/future formats, blocked writes or
unsupported journal state require explicit reconciliation. M4 must replace this
boundary with real durable obligation reconciliation before delivery activation.
Cloud independently rejects reconnect/replacement against any household delivery
ledger row until its M4 remapping/isolation rules exist.

## Candidate activation and namespace change

Save the candidate before confirmation. Until verified promotion, retain the
old bundle, candidate, verifier, intent and stop marker on every uncertain result.
Candidate issuance provides no household ID. HA therefore cannot claim a
pre-confirm same-household proof. After confirmation, authenticated installation
status must identify the issued new installation and a current credential. When
the prior household is known it must match. An overlap candidate or mismatched
household is retained for owner reconciliation, without local promotion or
speculative server rollback/revocation.

Successful promotion requires a freshly revalidated local receipt and one atomic
checkpoint replacing the current credential while clearing candidate, pairing,
verifier, old stop marker, automation budgets and reconnect intent. No import,
review state, calendar outcome, source identity or ACK obligation is moved. A new
installation cannot use the old namespace's journal. Confirmation still has the
existing durable two-attempt maximum; repeated recovery cannot reset it.

An explicit disconnect while repair is staged first cancels the local reconnect
intent and records an unconfirmed stop, retaining uncertain credential bundles.
It does not transparently retry a prior disconnect or resume/confirm the abandoned
repair. The owner must inspect actual remote state in the account portal.

This primitive is not M2 operational evidence. Native admin-only reconnect
controls and actual deployed owner/HA lifecycle rehearsal remain separate work.
