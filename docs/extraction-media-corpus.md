# Image and PDF corpus

The `media-*` fixtures add six synthetic variants of the same two-event community
bulletin: a desktop screenshot, a camera-photo-like JPEG with a tilted paper,
shadow and soft focus, a perspective-distorted PNG, a faded low-contrast PNG,
a two-page scanned PDF, and a two-page text-layer PDF. These are generated
illustrations of documents, not personal screenshots or photographs. All event
text remains legible. Fixed October 2026 dates, explicit PDT times, locations and
preparation notes make the expected extraction unambiguous.

The image assets were drawn with Pillow using DejaVu Sans; the perspective image
uses a projective transform and the faded image maps luminance to 182–230. The
scanned PDF embeds one rendered page per event. The text PDF uses Helvetica text
operators, one event per page. Committed assets require no generation tools or
font installation during tests. Each JSON fixture includes a human-readable
`source_transcript` for inspecting the corresponding page content.

All six cases join the ordinary recorded provider/parser replay and optional
provider evaluator automatically. The text PDF fixture supplies the exact locally
extracted text without an attachment, matching production ingestion. The other
five carry actual attachment files. Image cases have no accompanying text, and
the scanned PDF has only a generic import request, so an optional live adapter
must inspect the media to extract events.

`tests/test_media_corpus.py` additionally opens all images/PDFs, checks the visual
variants and page contents, feeds actual uploaded bytes through image/PDF staging
and the isolated PDF worker, and checks attachment transport, exact normalized
recorded results and temporary-file cleanup. The two events have independently
asserted titles, timestamps, locations and descriptions. Both PDF pages must be
represented; no source context supplies the scanned pages' missing text.
The two scanned page images are intentionally immutable synthetic evidence,
visually checked against their transcripts. Per-page SHA-256 checks pin decoded
RGB pixels and dimensions in page order, independently of PDF metadata or
compression. Replacing a page with blank, unrelated or duplicate content fails
even when a recorded provider response still returns both expected events.
An intentional artwork change requires inspecting both pages and updating their
canonical digests alongside the transcripts and expected events as appropriate.

These deterministic tests validate ingestion and response handling. A recorded
response cannot establish OCR or live-model accuracy for distortion, poor contrast,
or photographs. Use the optional live evaluation described in
[Extraction corpus](extraction-corpus.md) for that measurement.
