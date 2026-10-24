# Opt-in hosted connection maintenance

In the native Daylight Cloud settings, configure trusted HTTPS API/account
origins and optionally enable **Automatically maintain this connection**. It is
off by default and separate from processing consent. Enabling it cannot create a
connection, transfer credentials to another service, select a parser, submit a
source or clear a local stop. Starting a connection and approving it in the owner
portal remain explicit actions.

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
Navigation, socket/session changes and opt-out unsubscribe; late responses cannot
restore old entry state. Approval codes remain transient and disappear on
completion or view/session loss. Socket readiness reloads safe status only.
