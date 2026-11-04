# M3 live managed-parsing acceptance

This is a manual, synthetic-only rehearsal of the deployed path from the
Home Assistant **New import** interface to durable local review. Automated
fixture tests and this procedure are not evidence of a live run. Run only in
the disposable internal HA instance with the reviewed development Cloud service.
A valid grant and model calls require a separately approved operator session;
this document does not authorize a Railway apply, issue a grant, or make calls.
Never use household email, credentials, or customer attachments as fixtures.

## Preparation and input integrity

Record the exact HA, Cloud and infrastructure commits, deployed Cloud revision,
environment/deployment identifiers and UTC time in a protected operator record.
Verify the deployed migration ledger and /ready using the infrastructure
runbook, and retain the existing enrolled test installation. Confirm managed
parsing is selected, the exact installation has an unexpired short-lived grant,
and the reviewed model-call count and spend ceiling cover the cases. A grant is
not a quota. Stop if any prerequisite, target or amount is uncertain.

Use a checkout of the exact HA commit. The following are Git blob SHA-1
checksums, including the Git blob header, not raw file SHA-1 values. From the
repository root, run `git hash-object -- <path>` for each row and compare the
complete hash. Stop on mismatch. Never paste fixture JSON's
`provider_response` or `expected` into the import form: these are simulated
test expectations, not observations of the live model.

| Case | Input path under tests/fixtures/extraction/ | Git blob checksum |
| --- | --- | --- |
| text_basic | explicit-practice.json | 81cf6f0389b984b1a130dd3d82fd3e9e87077daa |
| text_zero | no-events-newsletter.json | 900b6cc7a4d1a28fd4464e48549e543b0f682f13 |
| text_all_day | dates-all-day-and-multiday.json | 6277bde732b9cbd834c8fbbda6aa16ae035305cb |
| text_meeting | meeting-join-details.json | df0b6f4d4a445be3b032669db3ea738100574a6a |
| text_dst | dates-dst-fall-back.json | 2c0d1322cae0757035e7ee878b3b71532a050516 |
| text_mixed | email-school-newsletter.json | cf7176f028a968e906d4f307a96fc341a0562aa7 |
| image | assets/media-screenshot.png | 3998be4cc9b5af8fe46a03518134b8044d104d97 |
| text PDF | assets/media-multipage-text.pdf | ab421fcce9663ed751d0933219d665b841864560 |
| scanned PDF | assets/media-multipage-scan.pdf | 73119d8a40bc7f3bcffe1fc1de0c31665791240a |

For the text cases paste only the decoded `source_text` value from the
respective JSON. For media cases upload the file through New import; use no
supporting text. These fixtures contain explicit historical 2026 dates and
Pacific time; preserve the original dates when judging results, and record HA's
configured time zone. The text fixtures' `reference_datetime` is an automated
test input, not a control exposed by this manual UI. Do not alter the HA clock.

The three media assets describe the same two synthetic events. Existing dedup
state may produce a duplicate outcome rather than new drafts. Use an isolated
fresh test instance/state for each media case or document and deliberately
use the UI's re-import option where available. Never reset real dedup state.
A successful duplicate response alone does not demonstrate fresh extraction.

## Execute and observe

For each case, submit exactly once and record the UTC time, case label and
outcome category. If a request times out or the browser disconnects, inspect
the review inbox and recent activity first. A provider call may have completed;
do not automatically retry or assume a request ID makes a replay idempotent.

| Case | Acceptance observation |
| --- | --- |
| text_basic | One soccer practice draft, October 15, 2026, 5–6 pm Pacific; location and instruction preserved. |
| text_zero | Successful no-events outcome; no draft, phantom event, or automatic retry. |
| text_all_day | Two all-day drafts: October 16 and October 17–19 inclusive; verify the displayed dates and exclusive end semantics where exposed. |
| text_meeting | One October 15 meeting with the synthetic join URL, meeting ID, passcode and dial instructions preserved in the draft. |
| text_dst | One November 1 shift spanning first 1:30 am PDT to second 1:30 am PST. Verify offsets when available and any required human review; do not accept an ambiguous or zero-duration result silently. |
| text_mixed | Six drafts for the listed timed and all-day events; no events from general announcements. Check inclusive multi-day dates and the budget link. |
| image | Two drafts for science night October 22 and garden morning October 24, with times, locations and instructions visible in the asset. |
| text PDF | The same two expected drafts. Note whether HA extracted text locally; a successful import does not by itself prove that PDF bytes reached the model. |
| scanned PDF | The same two expected drafts from the scanned pages. Record any missing or altered fields; a successful result alone does not prove which bytes the provider received. |

For each nonempty result inspect dates, times, all-day boundaries, descriptions,
locations and all returned drafts. Record discrepancies, including ambiguity or
missing fields; don't silently correct them and call extraction successful.
Reload HA and verify unapproved drafts remain in the review inbox. Check the
synthetic destination calendar before and after; submission must not create
calendar events. Do not approve the drafts during this check.

Separately verify the reviewed authorization-negative cases without granting
access or generating a valid model invocation: invalid credentials fail, and
an ungranted or revoked installation cannot invoke the model. Use existing
isolated test procedures for stale-session and invalid-model-output failures;
do not disrupt the working session or inject faults into a shared deployment.
After the hosted cases, follow the existing BYO setup and verify a local
synthetic parse still reaches review. Direct IMAP remains paused while hosted
parsing is selected.

## Close and retain evidence

Revoke the exact test grant and restore the runtime gate using the reviewed
infrastructure procedure. Verify the enrolled capability is unavailable and
record the plan, apply, no-drift and revocation outcomes privately. Do not
remove local drafts or alter working credentials merely to close the test.

Keep an evidence row for each case: revision identifiers, UTC time, label,
observed result category, expected-versus-observed field discrepancies,
persistence, no-calendar-write observation and any error category. Record
provider/model and elapsed time if available, but do not record tokens,
credentials, source bodies, attachment bytes, raw provider responses, or
private identifiers in ordinary logs or public issues. Model quality and cost
require separate measured evidence; a successful rehearsal is not M3 closure.
