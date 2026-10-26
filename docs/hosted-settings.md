# P02 native Daylight connection settings

On `integration/hosted-v1`, administrators can open **Settings → Daylight Cloud**
in the Daylight panel. Local/BYO and Direct IMAP remain independent. This does
not select a hosted parser, activate the private service or enable delivery.

Configure the reviewed API and account HTTPS origins explicitly. Configuration
records no processing consent. With automatic maintenance off (the default),
it makes no hosted request. The optional **Automatically maintain this connection**
checkbox enables [bounded maintenance](hosted-automatic-maintenance.md) only for
already-consented saved material. A changed service
never relabels private credentials; saved provenance must still match before any
saved material can be sent. Failed configuration reload keeps the old owner
closed until restart/repair, including a platform unload failure.

To start a first connection, name the installation and affirm the cloud-processing
consent checkbox. Only this explicit operation can return approval instructions.
Use its trusted account link and approval code, then manually **Check pairing
approval** after the advertised interval. Once the candidate is saved, **Check
and recover connection** verifies/confirms its identity and effective deadline.
The approval code is transient and never part of general settings, diagnostics,
config-entry options or browser persistent storage. Navigation, user/socket change
and DOM detachment clear it; late responses cannot restore old instructions.
After restart, a live saved attempt remains eligible for manual approval checks;
generic recovery does not strand it or replay exchange.

If reload reports an earlier operation is still finishing, wait for that work
to settle, then explicitly **Check and recover connection**. It can initialize
the replacement controller once the shared task set is empty; ordinary status
refresh never loads or replays a remote mutation.

The separate authenticated account URL is displayed only after validated status.
The native client does not invent a portal path or accept cross-origin links.
**Renew credentials** is an explicit single operation; candidate recovery remains
separate by default. Optional automatic maintenance performs bounded polling,
recovery and renewal, stopping for attention instead of replaying uncertain issuance.

**Disconnect** requires explicit confirmation. It durably stops local hosted work
before attempting remote revocation, retaining unconfirmed state on ambiguity.
Local imports, review state, calendar events and subscriptions are untouched.
Saved-candidate recovery differs from owner-authorized reconnect: this UI does
not clear disconnect markers, start a replacement namespace, transfer an ACK
journal or bypass owner reconciliation. Follow the account portal/repair guidance
where the controller requires it. Storage repair must preserve original files.

The admin-only WebSocket commands are:

- `daylight_calendar_import/hosted/get`: safe shared-runtime status only.
- `daylight_calendar_import/hosted/configure`: closed API/account origin bundle
  plus the explicit `automatic_lifecycle` boolean (default false).
- `daylight_calendar_import/hosted/subscribe`: admin-only entry invalidations;
  clients fetch current safe status and unsubscribe on navigation/session loss.
- `daylight_calendar_import/hosted/action`: one explicit connect/poll/reconcile/
  rotate/disconnect action. Connect requires affirmative consent and a label;
  disconnect requires affirmative confirmation. Action-specific fields cannot
  be used on another action. No bearer/verifier/owner selector is accepted.

Commands use the entry's single loaded runtime, reject half-initialized/unloading
entries and do not queue lifecycle work. A child operation cancelled by reload
receives a fixed WebSocket error rather than leaving the UI waiting; actual
caller cancellation propagates. No response/error serializes private checkpoints
or provider messages.

Python acceptance tests use actual HA admin WebSockets, entry reload and private
atomic checkpoints, with only the remote hosted boundary stubbed. Frontend tests
cover consent/confirmation, same-origin links, late responses and transient code
lifetime. These automated checks do not replace M2's protected live rehearsal.

## Owner-approved reconnect

The separate **Reconnect with owner approval** form requires renewed processing
consent and explicit replacement confirmation. It starts a fresh verifier-bound
pairing; approve the new code in the existing account portal, where the owner
reviews the actual installation and completes recent account verification. Then
use **Check pairing approval** and **Check and recover connection** deliberately.
Repair polling and confirmation are never scheduled automatically.

The runtime binds the reconnect primitive to the actual loaded pending store's
local-only namespace reconciler. Unknown/future journal formats and uncertain
storage block the transition. Local stop markers and the prior current bundle
remain until an authenticated current candidate in the prior household, when
known, is verified and atomically promoted. A local checkbox is not owner proof
and cannot authorize a nonempty ACK journal remap. Existing local imports and
calendar events are preserved; no namespace history is copied.

`hosted/action` accepts `action: reconnect` only with `processing_consent: true`,
an installation label and `confirm_reconnect: true`. These confirmation fields
cannot be used on other actions; there is no ledger-empty, owner-session, token,
account or namespace selector. General snapshots expose only safe fixed state;
one-time instructions appear only in that explicit response and clear on view or
session loss. Restart reports `reconnect_pending` and keeps manual recovery
available without recreating or exchanging an uncertain attempt.

If reconnect is requested before this entry has ever saved a connection, it
returns `first_connection_required` without writing intent or sending a request.
The UI directs the administrator to **Start new connection**, with fresh consent,
without changing service origins or entering a blocking service-repair state.
An unbound legacy stop or existing recovery material remains fail-closed.

## Guided connection page

The settings page keeps service origins, ongoing maintenance opt-in, renewal,
owner-approved replacement and disconnection under **Advanced settings &
troubleshooting**. A new installation still requires explicit processing consent
and a name before starting a connection. The portal requires its separate emailed
verification code after the 12-character HA connection code.

While the administrator keeps the page open, it checks approval at the advertised
interval and reconciles a saved candidate automatically. It also verifies saved
connection material when the page is opened after restart. This page-scoped
assistance does not enable durable background maintenance, source delivery or
parsing. It never starts pairing, replacement, renewal or disconnection itself.
Existing server deadlines, locks and uncertain-exchange safeguards remain
authoritative. Checks stop on errors, code expiration, session/navigation
changes or a bounded attempt budget; explicit recovery remains in Advanced.
When background maintenance is enabled, the page uses the existing subscription
for ordinary connections. Owner-approved reconnect remains page-driven because
the background policy deliberately excludes replacement checkpoints. Guided
checks preserve in-progress form drafts, focus and text selection.
