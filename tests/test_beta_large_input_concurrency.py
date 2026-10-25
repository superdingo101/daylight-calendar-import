"""Public beta regression scenarios for bounded inputs and simultaneous work."""

import asyncio
from contextlib import contextmanager
from dataclasses import replace

import pytest

from custom_components.daylight_calendar_import import pdfs, uploads
from custom_components.daylight_calendar_import.providers import SourceValidationError
from tests.test_pdfs import UploadHass as PdfHass, make_pdf
from tests.test_storage import FakeStoreBackend, draft, make_store
from tests.test_uploads import PNG, UploadHass


class BlockingStoreBackend(FakeStoreBackend):
    """Hold the first snapshot in flight until competing transactions start."""

    def __init__(self):
        super().__init__()
        self.saving = asyncio.Event()
        self.release = asyncio.Event()
        self.entered_saves = 0

    async def async_save(self, data):
        self.entered_saves += 1
        if self.entered_saves == 1:
            self.saving.set()
            await self.release.wait()
        await super().async_save(data)


@pytest.mark.parametrize("same_source", [False, True], ids=["independent-sources", "first-ingestion-duplicates"])
async def test_simultaneous_large_batches_and_repeated_approval_preserve_every_event(monkeypatch, same_source):
    """Independent sources survive concurrent commits; repeated clicks write once."""
    backend = BlockingStoreBackend()
    store = make_store(monkeypatch, backend)
    batches = [[replace(draft(), title=f"Batch {batch} appointment {event}")
                for event in range(24)] for batch in range(3)]
    started = 0
    all_started = asyncio.Event()
    original_add = store._async_add_transaction

    async def tracked_add(**kwargs):
        nonlocal started
        started += 1
        if started == 3:
            all_started.set()
        return await original_add(**kwargs)

    monkeypatch.setattr(store, "_async_add_transaction", tracked_add)
    source_indexes = [0, 0, 0] if same_source else [0, 1, 2]
    tasks = [asyncio.create_task(store.async_add(
        source_text=f"Newsletter {batch}", source_id=f"newsletter-{batch}", events=batches[batch]
    )) for batch in source_indexes]
    try:
        await asyncio.wait_for(backend.saving.wait(), 5)
        await asyncio.wait_for(all_started.wait(), 5)
        # Every transaction has entered while the first durable save is held.
        # Without serialization the competitors would save stale snapshots.
        assert backend.entered_saves == 1
        assert backend.saved == []
        assert all(not task.done() for task in tasks)
    finally:
        backend.release.set()
        results = await asyncio.gather(*tasks)
    accepted = [result for result in results if result.pending is not None]
    expected_batches = 1 if same_source else 3
    expected_events = expected_batches * 24
    assert len(accepted) == len(store.list()) == expected_batches
    assert sum(result.duplicate_source for result in results) == 3 - expected_batches
    assert sum(len(item.events) for item in store.list()) == expected_events
    # Simultaneous duplicate sources must not create another review batch.
    duplicates = await asyncio.gather(*(
        store.async_add(source_text="Newsletter 0", source_id="newsletter-0", events=batches[0]) for _ in range(4)
    ))
    assert all(result.pending is None and result.duplicate_source for result in duplicates)
    writes = []

    async def create(event):
        await asyncio.sleep(0)
        writes.append(event.id)

    approvals = await asyncio.gather(*(
        store.async_approve_event(result.pending.id, event.id, create)
        for result in accepted for event in result.pending.events for _ in range(2)
    ))
    assert sum(event is not None for event in approvals) == expected_events
    assert len(writes) == len(set(writes)) == expected_events
    assert store.list() == ()
    backend.load_result = backend.saved[-1]
    restored = make_store(monkeypatch, backend)
    await restored.async_load()
    assert restored.list() == ()
    for batch in sorted(set(source_indexes)):
        events = batches[batch]
        duplicate = await restored.async_add(source_text=f"Newsletter {batch}", source_id=f"newsletter-{batch}", events=events)
        assert duplicate.pending is None and duplicate.duplicate_source


@pytest.mark.parametrize("extra", [0, 1])
async def test_real_upload_byte_boundary_is_consumed_and_cleaned(monkeypatch, tmp_path, extra):
    """An exact-limit image stages; one extra byte fails before provider access."""
    original = tmp_path / "large.png"
    original.write_bytes(PNG + b"x" * (uploads.MAX_UPLOAD_BYTES - len(PNG) + extra))

    @contextmanager
    def process(_hass, _file_id):
        try:
            yield original
        finally:
            original.unlink(missing_ok=True)

    monkeypatch.setattr(uploads, "process_uploaded_file", process)
    media = tmp_path / "media"
    if extra:
        with pytest.raises(SourceValidationError) as error:
            async with uploads.async_image_source(UploadHass(media), "a" * 32):
                pytest.fail("Oversized source reached parser")
        assert error.value.code == "source_too_large"
    else:
        async with uploads.async_image_source(UploadHass(media), "a" * 32) as source:
            assert source.attachments[0].size_bytes == uploads.MAX_UPLOAD_BYTES
            assert len(list(media.iterdir())) == 1
    assert not original.exists()
    assert not list(media.glob("daylight-*"))


@pytest.mark.parametrize("extra", [0, 1])
async def test_real_pdf_worker_page_boundary_and_cleanup(monkeypatch, tmp_path, extra):
    """Exercise the subprocess with all 30 pages, then reject the 31st page."""
    original = tmp_path / "schedule.pdf"
    original.write_bytes(make_pdf("Practice November 12 2026 7pm", page_count=pdfs.MAX_PDF_PAGES + extra))

    @contextmanager
    def process(_hass, _file_id):
        try:
            yield original
        finally:
            original.unlink(missing_ok=True)

    monkeypatch.setattr(pdfs, "process_uploaded_file", process)
    if extra:
        with pytest.raises(SourceValidationError) as error:
            async with pdfs.async_pdf_source(PdfHass({}), "a" * 32):
                pytest.fail("Over-page-limit PDF reached parser")
        assert error.value.code == "too_many_pages"
    else:
        async with pdfs.async_pdf_source(PdfHass({}), "a" * 32) as source:
            assert source.text.count("Practice November 12 2026 7pm") == pdfs.MAX_PDF_PAGES
            assert source.attachments == ()
    assert not original.exists()
    assert not list(tmp_path.glob("daylight-*"))
