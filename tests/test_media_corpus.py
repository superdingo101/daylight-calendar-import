"""Real synthetic media exercise ingestion; recorded responses do not test OCR."""

import asyncio
from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image
from pypdf import PdfReader
import pytest

from custom_components.daylight_calendar_import import parser, pdfs, providers, uploads

ROOT = Path(__file__).parent / "fixtures" / "extraction"
CASES = [json.loads(path.read_text()) for path in sorted(ROOT.glob("media-*.json"))]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_media_gold_covers_both_events_and_source_details(case):
    """Keep the source content contract independent of the recorded AI response."""
    expected = case["expected"]["events"]
    assert [event["title"] for event in expected] == [
        "Family science night", "Garden volunteer morning",
    ]
    assert [(event["start"], event["end"]) for event in expected] == [
        ("2026-10-22T18:00:00-07:00", "2026-10-22T20:00:00-07:00"),
        ("2026-10-24T09:00:00-07:00", "2026-10-24T11:00:00-07:00"),
    ]
    assert [event["location"] for event in expected] == [
        "Willow School hall", "Cedar Community Garden",
    ]
    assert [event["description"] for event in expected] == [
        "Bring a notebook.", "Bring gloves and water.",
    ]
    assert all(event["all_day"] is False for event in expected)
    assert case["expected"]["warnings"] == []
    assert case["expected"]["event_assumptions"] == [[], []]


@pytest.mark.parametrize(("name", "format", "size"), [
    ("media-screenshot.png", "PNG", (1060, 1000)),
    ("media-camera-photo.jpg", "JPEG", (1200, 1100)),
    ("media-perspective.png", "PNG", (1200, 1100)),
    ("media-low-contrast.png", "PNG", (1000, 900)),
])
def test_image_evidence_is_decodable_and_keeps_visual_variant(name, format, size):
    with Image.open(ROOT / "assets" / name) as image:
        image.load()
        assert image.format == format
        assert image.size == size
        low, high = image.convert("L").getextrema()
        if "low-contrast" in name:
            assert 0 < high - low < 55
        else:
            assert high - low > 150
        if "perspective" in name:
            # The top edge is slanted and the page widens towards its base.
            assert image.getpixel((150, 200)) == image.getpixel((0, 0))
            assert image.getpixel((150, 850)) != image.getpixel((0, 0))


@pytest.mark.parametrize("variant", ["scan", "text"])
def test_multipage_pdf_evidence_has_an_event_on_each_page(variant):
    case = next(case for case in CASES if case["id"] == f"media-multipage-{variant}")
    reader = PdfReader(ROOT / "assets" / f"media-multipage-{variant}.pdf")
    assert len(reader.pages) == 2
    for page, transcript in zip(reader.pages, case["source_transcript"], strict=True):
        if variant == "scan":
            assert page.extract_text().strip() == ""
            assert len(page.images) == 1
            assert page.images[0].image.size == (1000, 900)
        else:
            assert page.extract_text().splitlines() == transcript
            assert len(page.images) == 0


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
async def test_media_ingestion_reaches_recorded_parser_and_cleans_up(case, monkeypatch, tmp_path):
    """Use the actual image/PDF bytes and PDF worker before recorded replay."""
    attachment = next(iter(case.get("attachments", [])), None)
    relative = attachment["path"] if attachment else case["local_pdf_path"]
    data = (ROOT / relative).read_bytes()
    uploaded = tmp_path / Path(relative).name
    uploaded.write_bytes(data)
    module = pdfs if case["source_kind"] == "pdf" else uploads

    @contextmanager
    def process(_hass, file_id):
        assert file_id == "corpus-upload"
        try:
            yield uploaded
        finally:
            uploaded.unlink(missing_ok=True)

    monkeypatch.setattr(module, "process_uploaded_file", process)
    generate = AsyncMock(return_value=SimpleNamespace(data=case["provider_response"]))
    monkeypatch.setattr(providers.ai_task, "async_generate_data", generate)
    component = SimpleNamespace(get_entity=lambda _id: SimpleNamespace(
        available=True, supported_features=providers.AITaskEntityFeature.SUPPORT_ATTACHMENTS,
    ))

    async def executor(func, *args):
        return await asyncio.to_thread(func, *args)

    media = tmp_path / "media"
    hass = SimpleNamespace(
        config=SimpleNamespace(media_dirs={"local": str(media)}, time_zone=case["time_zone"]),
        data={providers.DATA_COMPONENT: component}, async_add_executor_job=executor,
    )
    prepare = pdfs.async_pdf_source if module is pdfs else uploads.async_image_source
    context = case["source_text"] if attachment else ""
    prepared = prepare(hass, "corpus-upload", context) if module is pdfs else prepare(hass, "corpus-upload")
    async with prepared as source:
        assert not uploaded.exists()
        assert source.kind.value == case["source_kind"]
        assert source.text == (case["source_text"] or None)
        assert len(source.attachments) == (1 if attachment else 0)
        if attachment:
            staged = list(media.iterdir())
            assert len(staged) == 1
            assert staged[0].read_bytes() == data
            assert source.attachments[0].media_type == attachment["media_type"]
        else:
            assert not media.exists()
        outcome = await parser.async_parse_source(
            hass, source=source, ai_task_entity="ai_task.corpus",
            reference_datetime=datetime.fromisoformat(case["reference_datetime"]),
        )
        assert [event.as_dict() for event in outcome.events] == case["expected"]["events"]
        assert outcome.warnings == case["expected"]["warnings"]
        assert [list(notes) for notes in outcome.event_assumptions] == case["expected"]["event_assumptions"]
        generate.assert_awaited_once()
        request = generate.await_args.kwargs
        assert case["source_text"] in request["instructions"]
        if attachment:
            assert request["attachments"] == [{
                "media_content_id": source.attachments[0].content_ref,
                "media_content_type": attachment["media_type"],
            }]
        else:
            assert "attachments" not in request
    assert not list(media.glob("daylight-*"))
