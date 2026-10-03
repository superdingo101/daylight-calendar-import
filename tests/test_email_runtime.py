"""Tests for Home Assistant Direct IMAP runtime wiring."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import custom_components.daylight_calendar_import.email_runtime as runtime_module
from custom_components.daylight_calendar_import.const import (
    CONF_EMAIL_ENABLED,
    CONF_EMAIL_HOST,
    CONF_EMAIL_MAILBOX,
    CONF_EMAIL_PASSWORD,
    CONF_EMAIL_PORT,
    CONF_EMAIL_USERNAME,
    CONF_EMAIL_VERIFY_SSL,
)
from custom_components.daylight_calendar_import.direct_imap import (
    DirectImapConnectionError,
)
from custom_components.daylight_calendar_import.email_polling import EmailPollResult
from custom_components.daylight_calendar_import.email_runtime import (
    DEFAULT_EMAIL_MAILBOX,
    DEFAULT_EMAIL_PORT,
    EmailPollingRuntime,
    async_setup_email_runtime,
    direct_imap_settings_from_options,
    email_review_source_text,
)
from custom_components.daylight_calendar_import.sources import (
    SourceAttachment,
    SourceDocument,
    SourceKind,
)


class FakeHass:
    def __init__(self) -> None:
        self.created_tasks: list[asyncio.Task] = []
        self.config = SimpleNamespace(media_dirs={})

    def async_create_task(self, coro, name=None):
        task = asyncio.create_task(coro, name=name)
        self.created_tasks.append(task)
        return task


def _options(**overrides):
    values = {
        CONF_EMAIL_ENABLED: True,
        CONF_EMAIL_HOST: "imap.example.test",
        CONF_EMAIL_PORT: 993,
        CONF_EMAIL_USERNAME: "calendar@example.test",
        CONF_EMAIL_PASSWORD: "app-secret",
        CONF_EMAIL_MAILBOX: "INBOX",
        CONF_EMAIL_VERIFY_SSL: True,
    }
    values.update(overrides)
    return values



def test_direct_imap_settings_from_options_uses_defaults_and_policy():
    settings = direct_imap_settings_from_options(
        "entry-1",
        {
            CONF_EMAIL_HOST: "imap.example.test",
            CONF_EMAIL_USERNAME: "calendar@example.test",
            CONF_EMAIL_PASSWORD: "secret",
        },
    )

    assert settings.source_id == "entry-1:direct-imap"
    assert settings.port == DEFAULT_EMAIL_PORT
    assert settings.mailbox == DEFAULT_EMAIL_MAILBOX
    assert settings.verify_ssl is True
    assert settings.sender_allowlist == ()
    assert settings.disposition.mark_seen is True


def test_direct_imap_settings_from_options_preserves_explicit_values():
    settings = direct_imap_settings_from_options(
        "entry-1",
        _options(
            **{
                CONF_EMAIL_PORT: 1993,
                CONF_EMAIL_MAILBOX: "Calendar",
                CONF_EMAIL_VERIFY_SSL: False,
            }
        ),
    )

    assert settings.port == 1993
    assert settings.mailbox == "Calendar"
    assert settings.verify_ssl is False
    assert settings.sender_allowlist == ()
    assert settings.disposition.mark_seen is True


def test_email_review_source_text_includes_attachment_metadata_only():
    attachments = (
        SourceAttachment(
            id="a1",
            filename="flyer.pdf",
            media_type="application/pdf",
            size_bytes=10,
            content_ref="media-source://local/flyer.pdf",
            sha256="abc123",
        ),
        SourceAttachment(
            id="a2",
            filename=None,
            media_type="image/png",
            size_bytes=20,
            content_ref="media-source://local/image.png",
        ),
    )
    document = SourceDocument(
        id="doc-1",
        kind=SourceKind.EMAIL,
        received_at=datetime(2026, 10, 3, 12, 0, tzinfo=UTC),
        text="Friday at 5",
        attachments=attachments,
    )

    assert email_review_source_text(document) == (
        "Friday at 5\n\n"
        "Email attachment: flyer.pdf (SHA-256: abc123)\n\n"
        "Email attachment: image/png"
    )


def test_email_review_source_text_allows_attachment_only_source():
    attachment = SourceAttachment(
        id="a1",
        filename="flyer.pdf",
        media_type="application/pdf",
        size_bytes=10,
        content_ref="media-source://local/flyer.pdf",
    )
    document = SourceDocument(
        id="doc-1",
        kind=SourceKind.EMAIL,
        received_at=datetime(2026, 10, 3, 12, 0, tzinfo=UTC),
        text=None,
        attachments=(attachment,),
    )

    assert email_review_source_text(document) == "Email attachment: flyer.pdf"


async def test_setup_email_runtime_returns_none_when_disabled():
    entry = SimpleNamespace(entry_id="entry-1", options={})
    result = await async_setup_email_runtime(
        FakeHass(),
        entry,
        SimpleNamespace(),
        AsyncMock(),
    )
    assert result is None


async def test_setup_email_runtime_builds_and_starts_runtime(monkeypatch):
    source = object()
    source_factory = Mock(return_value=source)
    runtime = SimpleNamespace(async_start=AsyncMock())
    runtime_factory = Mock(return_value=runtime)
    monkeypatch.setattr(runtime_module, "DirectImapSource", source_factory)
    monkeypatch.setattr(runtime_module, "EmailPollingRuntime", runtime_factory)
    store = SimpleNamespace()
    processor = AsyncMock()
    entry = SimpleNamespace(entry_id="entry-1", options=_options())

    result = await async_setup_email_runtime(
        FakeHass(),
        entry,
        store,
        processor,
    )

    assert result is runtime
    settings = source_factory.call_args.args[0]
    assert settings.source_id == "entry-1:direct-imap"
    runtime_factory.assert_called_once()
    assert runtime_factory.call_args.args[1:] == (source, store, processor)
    runtime.async_start.assert_awaited_once_with()


async def test_runtime_starts_immediately_skips_overlap_and_stops(monkeypatch):
    hass = FakeHass()
    cancel_interval = Mock()
    callbacks = []
    monkeypatch.setattr(
        runtime_module,
        "async_track_time_interval",
        lambda _hass, callback, interval: (
            callbacks.append((callback, interval)) or cancel_interval
        ),
    )
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fake_poll(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()

    monkeypatch.setattr(runtime_module, "async_poll_email_source", fake_poll)
    runtime = EmailPollingRuntime(
        hass,
        SimpleNamespace(),
        SimpleNamespace(),
        AsyncMock(),
        interval=timedelta(seconds=17),
    )

    await runtime.async_start()
    await started.wait()
    assert calls == 1
    assert callbacks[0][1] == timedelta(seconds=17)
    assert getattr(runtime._schedule_poll, "_hass_callback", False) is True

    callbacks[0][0](None)
    await asyncio.sleep(0)
    assert calls == 1

    await runtime.async_stop()
    cancel_interval.assert_called_once_with()
    assert hass.created_tasks[0].cancelled()


async def test_runtime_can_schedule_again_after_completed_poll(monkeypatch):
    hass = FakeHass()
    callbacks = []
    monkeypatch.setattr(
        runtime_module,
        "async_track_time_interval",
        lambda _hass, callback, _interval: (
            callbacks.append(callback) or (lambda: None)
        ),
    )
    poll = AsyncMock()
    monkeypatch.setattr(runtime_module, "async_poll_email_source", poll)
    runtime = EmailPollingRuntime(
        hass,
        SimpleNamespace(),
        SimpleNamespace(),
        AsyncMock(),
    )

    await runtime.async_start()
    await hass.created_tasks[-1]
    callbacks[0](None)
    await hass.created_tasks[-1]

    assert poll.await_count == 2
    await runtime.async_stop()


@pytest.mark.parametrize(
    "error",
    (
        DirectImapConnectionError("offline"),
        RuntimeError("storage failed"),
    ),
)
async def test_runtime_keeps_transient_poll_failure_retryable(
    monkeypatch,
    caplog,
    error,
):
    hass = FakeHass()
    monkeypatch.setattr(
        runtime_module,
        "async_track_time_interval",
        lambda *_args: (lambda: None),
    )
    monkeypatch.setattr(
        runtime_module,
        "async_poll_email_source",
        AsyncMock(side_effect=error),
    )
    runtime = EmailPollingRuntime(
        hass,
        SimpleNamespace(),
        SimpleNamespace(),
        AsyncMock(),
    )

    await runtime.async_start()
    await hass.created_tasks[-1]

    assert "poll failed" in caplog.text.lower()
    await runtime.async_stop()


async def test_runtime_propagates_poll_cancellation(monkeypatch):
    runtime = EmailPollingRuntime(
        FakeHass(),
        SimpleNamespace(),
        SimpleNamespace(),
        AsyncMock(),
    )
    monkeypatch.setattr(
        runtime_module,
        "async_poll_email_source",
        AsyncMock(side_effect=asyncio.CancelledError()),
    )

    with pytest.raises(asyncio.CancelledError):
        await runtime._async_poll()


async def test_runtime_stages_attachments_through_existing_stager(monkeypatch):
    stage = object()
    stager = AsyncMock(return_value=stage)
    monkeypatch.setattr(
        runtime_module,
        "async_stage_email_attachments",
        stager,
    )
    hass = FakeHass()
    runtime = EmailPollingRuntime(
        hass,
        SimpleNamespace(),
        SimpleNamespace(),
        AsyncMock(),
    )
    envelope = object()
    document = object()

    result = await runtime._async_stage_attachments(envelope, document)

    assert result is stage
    stager.assert_awaited_once_with(hass, envelope, document)


async def test_runtime_stop_is_noop_before_start():
    runtime = EmailPollingRuntime(
        FakeHass(),
        SimpleNamespace(),
        SimpleNamespace(),
        AsyncMock(),
    )
    await runtime.async_stop()


async def test_runtime_logs_retryable_failures_from_poll_result(
    monkeypatch,
    caplog,
) -> None:
    runtime = EmailPollingRuntime(
        FakeHass(),
        SimpleNamespace(),
        SimpleNamespace(),
        AsyncMock(),
    )
    monkeypatch.setattr(
        runtime_module,
        "async_poll_email_source",
        AsyncMock(
            return_value=EmailPollResult(
                discovered=3,
                claimed=1,
                processed=1,
                duplicates=2,
                normalization_failures=2,
                processing_failures=3,
                acknowledged=1,
                acknowledgement_failures=4,
            )
        ),
    )

    await runtime._async_poll()

    assert caplog.records[-1].getMessage() == (
        "Direct IMAP poll completed with retryable failures: "
        "normalization=2 processing=3 acknowledgement=4"
    )


async def test_runtime_does_not_warn_when_acknowledgements_succeed(
    monkeypatch,
    caplog,
) -> None:
    runtime = EmailPollingRuntime(
        FakeHass(),
        SimpleNamespace(),
        SimpleNamespace(),
        AsyncMock(),
    )
    monkeypatch.setattr(
        runtime_module,
        "async_poll_email_source",
        AsyncMock(
            return_value=EmailPollResult(
                discovered=1,
                claimed=1,
                processed=1,
                duplicates=0,
                normalization_failures=0,
                processing_failures=0,
                acknowledged=1,
                acknowledgement_failures=0,
            )
        ),
    )

    await runtime._async_poll()

    assert "retryable failures" not in caplog.text.lower()
