# Bounded hosted connection maintenance

In the native Daylight Cloud settings, configure trusted HTTPS API/account
origins. New service configurations enable bounded lifecycle maintenance by
default. The former opt-in checkbox is no longer shown. This is separate from
processing consent: the policy still requires a service-bound saved checkpoint
with affirmative consent before any remote operation. Maintenance cannot
create a connection, transfer credentials to another service, select a parser,
submit a source or clear a local stop. Starting a connection and approving it
in the owner portal remain explicit actions.

A legacy installation with persisted `hosted_automatic_lifecycle: false`
keeps that choice until its administrator deliberately uses the one-time
**Enable automatic renewals** action. The on-page guided flow continues to
help with pending approvals while that old opt-out remains saved. The backend preserves explicit false for older callers. An omitted maintenance
field defaults to true only on first configuration; later omitted fields preserve
the existing saved policy.

After successful entry setup, a single HA-owned one-shot timer checks the durable
policy. It polls an already-saved approval attempt, recovers saved candidate or
current credentials, and renews a verified current credential seven days before
its effective expiry. Work shares the exact same entry task owner as manual
controls. There are no queues, competing controllers or browser mutation loops.
The private budgets described in [the policy](hosted-automation-policy.md)
reserve attempts before network work, honor server polling and Retry-After gates,
survive reload/restart, and stop at safe limits. Uncertain issuance never causes
automatic reissuance. Overlap credentials cannot renew.

Budget exhaustion, uncertain issuance and terminal repair states stop automatic
work and create an entry-scoped HA Repairs issue. Check and recover the existing
connection or use the owner portal; do not delete private files or clear journal
provenance. Successful verified recovery clears the issue unless an independent
rotation/candidate budget still requires attention. Intentional disconnect or
opting out clears the corresponding issue. Local stop blocks further work even
if remote disconnection is unconfirmed.

Pause/unload cancels timers before platform teardown; shutdown and successful
unload cancel the owned task with the existing bounded cleanup. A request that
ignores cancellation stays owned across reload, preventing overlap. A deferred
replacement runtime requires explicit recovery after that operation settles.
Local/BYO setup and ingestion remain independent of hosted failures.

The open administrator UI subscribes to private, entry-only invalidations. It
fetches the current safe snapshot on change, rather than trusting an old runtime
payload. Notifications carry no credentials, approval codes or account links.
Navigation, socket/session changes and legacy opt-out unsubscribe; late responses cannot
restore old entry state. Approval codes remain transient and disappear on
completion or view/session loss. Socket readiness reloads safe status only.

Manual failures also persist their retry gates before scheduling resumes. Renewal
failures carry the same deadline into status recovery, so changing phases cannot
repeat a rate-limited endpoint early. These gates survive restart without
resetting any independent attempt budget. An expired pairing or a saved uncertain
exchange stops with attention and an HA Repairs issue; it never silently remains
in a waiting state or repeats the exchange.

Transient approval-poll failures retain the current live approval instructions
in the open view while the bounded retry waits. Completion, expiry, uncertain
exchange and view/session loss still clear them. Status invalidations preserve
unsaved service origins, the maintenance checkbox and other in-progress controls;
an unchanged snapshot does not rebuild the forms.
