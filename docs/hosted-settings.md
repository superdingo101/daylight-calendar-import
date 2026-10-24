# P02 native Daylight connection settings

On `integration/hosted-v1`, administrators can open **Settings → Daylight Cloud**
in the Daylight panel. Local/BYO and Direct IMAP remain independent. This does
not select a hosted parser, activate the private service or enable delivery.

Configure the reviewed API and account HTTPS origins explicitly. Configuration
records no processing consent and makes no hosted request. A changed service
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
separate. Automatic polling/renewal scheduling is a later bounded consumer.

**Disconnect** requires explicit confirmation. It durably stops local hosted work
before attempting remote revocation, retaining unconfirmed state on ambiguity.
Local imports, review state, calendar events and subscriptions are untouched.
Saved-candidate recovery differs from owner-authorized reconnect: this UI does
not clear disconnect markers, start a replacement namespace, transfer an ACK
journal or bypass owner reconciliation. Follow the account portal/repair guidance
where the controller requires it. Storage repair must preserve original files.

The admin-only WebSocket commands are:

- `daylight_calendar_import/hosted/get`: safe shared-runtime status only.
- `daylight_calendar_import/hosted/configure`: closed API/account origin bundle.
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
