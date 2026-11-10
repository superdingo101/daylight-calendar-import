# AI Task provider compatibility for public beta

Daylight uses a Home Assistant **AI Task entity**, not a conversation agent.
Choose an entity that supports generating structured data. Set it in Daylight
Settings → General. A provider's name alone does not establish model or media
compatibility; record the integration, model and version used in each live run.

| Input | Required capability | Beta behavior |
| --- | --- | --- |
| Pasted text or email text | Structured data generation | Uses the production extraction instructions and event schema. |
| Text-layer PDF | Structured data generation | Local bounded PDF extraction supplies text; attachment support is unnecessary when local extraction succeeds. |
| JPEG, PNG or WebP | AI Task attachment support and a model that can read the image | Rejected before generation when the entity lacks attachment support. |
| Scanned PDF | AI Task attachment support and a model/provider accepting PDFs | Attachment support is a coarse HA flag; it does not guarantee that a particular model accepts PDF files. Verify this separately. |
| Other file types | Unsupported | Rejected before provider invocation. |

Daylight retains valid events from a partially malformed response and shows
warnings for rejected candidates. An invalid top-level response is a failed
submission. Typed authentication, rate-limit, connectivity and timeout failures
use privacy-safe categories with [recovery guidance](provider-errors.md).
Submission and extraction never approve events or create calendar entries.

## What automated tests establish

`tests/test_provider_compatibility.py` replays every synthetic extraction case
through Home Assistant's real AI Task entity dispatch, chat session and local
media resolution. The development entity returns the recorded response; no
`async_generate_data` replacement bypasses HA. Tests compare all normalized event
fields, warnings and assumption disclosures, and verify attachment bytes, MIME
types and the exact production instructions. A text-only entity separately
rejects image/PDF input before generation.

This is deterministic adapter compatibility, **not a live model evaluation**.
Other focused suites cover malformed responses, partial extraction, cancellation,
timeouts and privacy-safe error classification. Neither these tests nor a
provider's attachment flag proves real OCR or extraction quality.

## Required live-provider evaluation

Run outside required PR CI, using a development installation and only the
committed synthetic corpus. AI usage may incur the provider's normal charges.
No personal mailbox or household calendar is needed.

1. Record Home Assistant version, provider integration/version, AI Task entity,
   model/version and run date. Do not record tokens, request bodies or responses.
2. Supply an adapter using the configured provider and the production
   `PROMPT_TEMPLATE` and `EVENTS_STRUCTURE` from `parser.py`. The adapter protocol
   is specified in [Extraction corpus](extraction-corpus.md). Preserve fixture
   reference time and time zone; never feed expected or recorded responses to
   the provider.
3. Run from the repository root:

   ```sh
   python scripts/evaluate_extraction_corpus.py --timeout 120 -- python /path/to/provider_adapter.py
   ```

4. Record aggregate counts and field/date/meeting-detail scores. Inspect missed
   and spurious events, ambiguous dates and lost meeting details locally. The
   scorer uses conservative exact title/field matching; paraphrasing can require
   human adjudication. Keep credentials and returned source/meeting content out
   of issues, logs and committed evaluation records.
5. Test text-only operation separately from attachment-capable operation. Record
   any unsupported image/PDF cases as limitations. A failed full-corpus run is
   not a passing evaluation. For image fidelity scenarios, use the actual
   committed image/PDF files, not their inspection transcripts.
6. Exercise authentication failure, unavailable provider and recovery in a
   development HA instance. A retry must still produce pending review and no
   automatic calendar write.

## Evaluation record

| Evidence | Status |
| --- | --- |
| Deterministic HA adapter/corpus tests | Implemented in the compatibility test suite; CI records the exact tested SHA. |
| Actual model/provider corpus run | **Pending**: requires an explicitly configured development/reference provider. |
| Provider/model-specific media support | **Unverified** until recorded by the live run. |

No provider/model combination is certified by this document. Before public-beta
release, replace the pending entries with actual dates, versions, numeric results
and known limitations. Do not mark a fixture replay as a live result.

### Unsupported recurrence

AI Task extraction requests an `unsupported_recurrence` boolean for recurrence
found in the source title (including email subjects), body or attachments.
Nonempty titles and bodies are supplied together as source evidence to both the
provider and independent text guard. An email subject consumed entirely as a
calendar routing directive is removed from this evidence, just like routing
directives in the body; ordinary subjects remain event evidence.
Recurring schedules are not expanded
into calendar writes. A true flag produces one `unsupported_recurrence` warning
in the existing import activity/review result and withholds all candidates from
that source. Submit independent one-off events separately or create the repeating
schedule manually in the destination calendar. This conservative whole-source
policy prevents ambiguous recurring candidates from being written as one-offs;
it also withholds independent events mixed into a recurring announcement.

An independent text guard recognizes explicit English recurrence expressions
(including “every Thursday”, “repeats weekly” and `RRULE:`), even if the AI
incorrectly clears its flag. Generic “Weekly newsletter” headings and
explicit lists of independent dates remain supported. This is not a universal
recurrence classifier: other languages, unusual phrasing and recurrence visible
only in images/PDFs still depend on the provider reporting it. Older recorded
responses without the flag remain readable through the offline parsing helper.
The live provider boundary rejects missing or non-boolean flags as
`provider_invalid_response`, including attachment-only responses.
Validate a real model against recurring announcements before relying on it.
