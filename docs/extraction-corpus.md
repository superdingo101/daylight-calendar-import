# Extraction corpus

The synthetic fixtures in `tests/fixtures/extraction` contain source text, a fixed
reference datetime and Home Assistant timezone, a recorded AI Task response, and
an independently specified expected normalized outcome. They contain no personal
email, credentials, or production data. Dates are intentionally fixed.

Ordinary pytest replays responses through the real provider and parser boundary.
It verifies transport, context, validation and normalized fields; it does **not**
measure whether a real AI extracts the right events from these sources.

Each JSON case uses its filename as `id`, `source_text`, `reference_datetime`
(with offset), `time_zone`, `provider_response`, and `expected` containing `events`,
`warnings`, and `event_assumptions`. Expected event objects include every
`EventDraft.as_dict()` field. Optional `source_kind` and `attachments` support
image/PDF inputs. Each attachment has `name`, `media_type`, and `path` relative to
the fixture directory. Additional scenario metadata can be consumed by focused
routing, deduplication, or conflict tests.

## Optional reference-provider evaluation

Run an explicit adapter against a configured provider outside CI:

```sh
python scripts/evaluate_extraction_corpus.py --timeout 120 -- python /path/to/provider_adapter.py
```

An adapter receives one case as JSON on stdin (plus absolute `fixture_directory`)
and prints the actual structured AI Task result `{ "events": [...] }` as JSON to
stdout. It should use the same production extraction instructions and context,
submit source text and attachment bytes to the chosen provider, and get credentials
from its own environment. Do not echo fixture `provider_response`: that exercises
the adapter protocol only, not extraction quality. The adapter must not expose
credentials on stdout/stderr. The evaluator does not save provider content.

The report includes each case's event count and exact per-field correctness totals;
missing and extra events penalize every field. Event order follows source order.
Confidence is excluded because calibration varies by provider. String fields are
trimmed like production normalization; timestamps and assumptions are exact, so
semantically equivalent formatting or reordered events require manual inspection.
A no-event case contributes to event-count accuracy, not empty field denominators.
Provider failures/timeouts stop the run rather than becoming false successes.
This evaluation is intentionally optional and never gates deterministic CI. Record
provider/model/version and run date separately when comparing reports. No live
provider is called during normal tests.

## Temporal and multi-event cases

The `dates-*` corpus fixes the clock and includes a 24-event workshop schedule,
single and inclusive multi-day closures (exclusive calendar ends), relative dates
with contextual assumptions, explicit foreign offsets, both DST transitions,
overnight events, unresolved numeric dates, missing year/end information, and
cancellation/rescheduling notices. Unknown year/end/ambiguous dates produce no
recorded draft. A cancellation-only notice produces no new draft; a replacement
announcement produces a draft for review, never a calendar modification.

These are explicit expected extraction policies for reference-provider evaluation,
not a claim that recorded replay proves a live model follows them. Independent
fixture tests check event completeness, one-hour elapsed DST spans, exclusive
all-day ends, and contextual disclosures. CI checks the real parser boundary's
handling of these recorded provider outputs without calling a model.
