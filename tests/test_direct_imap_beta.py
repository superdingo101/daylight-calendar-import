"""Beta reliability journeys through IMAP, MIME, runtime, and durable storage.

The protocol and persistence doubles are shared with the existing pipeline suite;
production normalization, attachment staging, polling, and processing remain real.
No mailbox credentials or live provider are required.
"""

from __future__ import annotations

import asyncio
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.daylight_calendar_import.email_attachments import (
    async_stage_email_attachments,
)
from custom_components.daylight_calendar_import.email_polling import (
    async_poll_email_source,
)
from custom_components.daylight_calendar_import.email_runtime import EmailPollingRuntime
from custom_components.daylight_calendar_import.parser import ParseOutcome
from tests.test_direct_imap_pipeline import (
    FakeImapClient,
    PipelineEnvironment,
    ScriptedFactory,
    _draft,
    _message,
    _snapshot_has_durable_source,
    _source,
)


def _forwarded_message() -> tuple[bytes, dict[str, bytes]]:
    message = EmailMessage()
    message["Message-ID"] = "<forwarded-beta@example.test>"
    message["Subject"] = "Fwd: School calendar"
    message["From"] = "Parent <parent@example.test>"
    message.set_content(
        "Please import these dates.\n\n---------- Forwarded message ---------\n"
        "From: School <school@example.test>\nSports day Friday at 5pm.\n"
    )
    # Opaque MIME payloads: provider decoding is covered by provider tests.
    media = {
        "flyer.png": b"\x89PNG\r\n\x1a\nflyer",
        "schedule.pdf": b"%PDF-1.7\nschedule",
    }
    message.add_attachment(
        media["flyer.png"], maintype="image", subtype="png", filename="flyer.png",
    )
    message.add_attachment(
        media["schedule.pdf"], maintype="application", subtype="pdf",
        filename="schedule.pdf",
    )
    message.add_attachment(
        b"unsupported attachment", maintype="application", subtype="octet-stream",
        filename="notes.bin",
    )
    return message.as_bytes(), media


@pytest.mark.parametrize("provider_fails_first", [False, True])
async def test_forwarded_mixed_media_cleans_files_and_retries_without_early_ack(
    monkeypatch, tmp_path, provider_fails_first,
) -> None:
    raw, media = _forwarded_message()
    parser_calls = []

    async def parse(_hass, *, source, **_kwargs):
        parser_calls.append(source)
        assert "Forwarded message" in source.text
        assert "Sports day Friday at 5pm." in source.text
        assert source.metadata["sender"] == "Parent <parent@example.test>"
        assert {item.filename for item in source.attachments} == set(media)
        assert {item.media_type for item in source.attachments} == {
            "image/png", "application/pdf",
        }
        for item in source.attachments:
            staged_path = tmp_path / Path(item.content_ref).name
            assert staged_path.read_bytes() == media[item.filename]
        if provider_fails_first and len(parser_calls) == 1:
            raise TimeoutError("temporary provider outage")
        return ParseOutcome([_draft()], [])

    environment = PipelineEnvironment(monkeypatch, AsyncMock(side_effect=parse))
    hass, store, processor = await environment.async_setup()
    hass.config = SimpleNamespace(media_dirs={"local": str(tmp_path)})
    hass.async_add_executor_job = asyncio.to_thread

    async def stage(envelope, document):
        return await async_stage_email_attachments(hass, envelope, document)

    collect = FakeImapClient(search_uids=(31,), messages={31: raw})

    def before_ack():
        assert _snapshot_has_durable_source(
            environment.backend.latest, "<forwarded-beta@example.test>",
        )
        assert list(tmp_path.iterdir()) == []

    ack = FakeImapClient(before_store=before_ack)
    clients = [collect]
    if provider_fails_first:
        clients.append(FakeImapClient(search_uids=(31,), messages={31: raw}))
    clients.append(ack)
    factory = ScriptedFactory(*clients)
    source = _source(factory)

    first = await async_poll_email_source(
        source, store, processor, attachment_stager=stage,
    )
    assert list(tmp_path.iterdir()) == []
    if provider_fails_first:
        assert first.processing_failures == 1
        assert first.acknowledged == 0
        assert ack.uid_calls == []
        assert store.list() == ()
        assert not store.is_source_durable("<forwarded-beta@example.test>")
        result = await async_poll_email_source(
            source, store, processor, attachment_stager=stage,
        )
    else:
        result = first
    assert result.processed == result.acknowledged == 1
    assert len(store.list()) == 1
    assert len(parser_calls) == (2 if provider_fails_first else 1)
    assert list(tmp_path.iterdir()) == []
    assert ack.uid_calls == [("store", ("31", "+FLAGS.SILENT", "(\\Seen)"))]
    assert factory.remaining == 0


async def test_runtime_reconnects_after_mailbox_outage_and_deduplicates_redelivery(
    monkeypatch, caplog, tmp_path,
) -> None:
    parser = AsyncMock(return_value=ParseOutcome([_draft()], []))
    environment = PipelineEnvironment(monkeypatch, parser)
    hass, store, processor = await environment.async_setup()
    hass.config = SimpleNamespace(media_dirs={"local": str(tmp_path)})
    hass.async_add_executor_job = asyncio.to_thread
    outage = FakeImapClient()
    outage.login = AsyncMock(side_effect=OSError("mailbox unavailable"))
    raw = _message("redelivered-beta@example.test")
    collect = FakeImapClient(search_uids=(41, 42), messages={41: raw, 42: raw})
    first_ack = FakeImapClient()
    second_ack = FakeImapClient()
    factory = ScriptedFactory(outage, collect, first_ack, second_ack)
    runtime = EmailPollingRuntime(hass, _source(factory), store, processor)

    await runtime._async_poll()
    assert "Direct IMAP poll failed" in caplog.text
    parser.assert_not_awaited()
    assert store.list() == ()
    assert outage.logout_calls == 1

    await runtime._async_poll()
    parser.assert_awaited_once()
    assert len(store.list()) == 1
    assert first_ack.uid_calls == [("store", ("41", "+FLAGS.SILENT", "(\\Seen)"))]
    assert second_ack.uid_calls == [("store", ("42", "+FLAGS.SILENT", "(\\Seen)"))]
    assert factory.remaining == 0


async def test_pending_save_failure_leaves_mail_unseen_and_retryable_after_restart(
    monkeypatch,
) -> None:
    parser = AsyncMock(return_value=ParseOutcome([_draft()], []))
    environment = PipelineEnvironment(monkeypatch, parser)
    _, store, processor = await environment.async_setup()
    save = environment.backend.async_save
    failed = False

    async def fail_pending_once(data):
        nonlocal failed
        if data.get("items") and not failed:
            failed = True
            raise OSError("disk temporarily unavailable")
        await save(data)

    monkeypatch.setattr(environment.backend, "async_save", fail_pending_once)
    raw = _message("disk-retry-beta@example.test")
    collect = FakeImapClient(search_uids=(51,), messages={51: raw})
    retry = FakeImapClient(search_uids=(51,), messages={51: raw})
    ack = FakeImapClient()
    factory = ScriptedFactory(collect, retry, ack)
    source = _source(factory)

    result = await async_poll_email_source(source, store, processor)
    assert failed
    assert result.processing_failures == 1
    assert result.acknowledged == 0
    assert ack.uid_calls == []
    assert store.list() == ()
    assert not _snapshot_has_durable_source(
        environment.backend.latest, "<disk-retry-beta@example.test>",
    )

    _, restored, restored_processor = await environment.async_setup()
    assert restored.list() == ()
    result = await async_poll_email_source(source, restored, restored_processor)
    assert result.processed == result.acknowledged == 1
    assert result.processing_failures == 0
    assert parser.await_count == 2
    assert len(restored.list()) == 1
    assert ack.uid_calls == [("store", ("51", "+FLAGS.SILENT", "(\\Seen)"))]
    assert factory.remaining == 0
