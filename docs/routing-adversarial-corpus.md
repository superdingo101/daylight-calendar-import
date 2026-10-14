# Routing and adversarial extraction corpus

The synthetic `tests/fixtures/extraction/routing-*.json` cases cover school
newsletters, forwarded announcements, unknown/empty/overlong aliases, conflicting
requests, quoted-history directives, and source instructions attempting to grant
calendar write authority. Every address and event is fictional.

Each case uses the extraction corpus's recorded-provider contract. Optional
`routing_expectation` metadata specifies the destination, routing status,
confirmation requirement, and evidence text after stripping control directives.
`calendar_candidates` supplies exact duplicate, possible duplicate, overlapping
conflict, and adjacent nonconflict expectations for the newsletter case.
These expected outcomes are test/evaluation data, not provider input.

The asset directory includes an actual readable PNG poster, an image-only PDF
scan, and a PDF containing an extractable text layer. The PNG and scan carry
attachment references through recorded-provider replay. The text PDF case uses
its extracted event evidence as source text, matching local PDF extraction.
Asset integrity assertions confirm the text/scan distinction independently of
recorded AI output. This does not exercise live image OCR or PDF recognition.

`tests/test_routing_corpus.py` checks deterministic routing, normalized draft
fields, calendar duplicate/conflict classifications, source deduplication,
persisted pending status, destination persistence after restart, and confirmation
blocking for unresolved routes. Extra provider fields requesting private calendar
access or automatic approval never become draft authority. Quoted forwarded
calendar directives remain event evidence rather than overriding the sender's
explicit outer directive.

Run the targeted suite:

```sh
python -m pytest tests/test_routing_corpus.py tests/test_extraction_corpus.py
```

Recorded-response tests validate the integration's handling of known responses.
They do **not** prove that an arbitrary live model resists prompt injection or
extracts every visual event correctly. Use the optional extraction corpus
reference-provider evaluator to measure actual model behavior on these sources;
keep that nondeterministic evaluation outside required PR checks.
