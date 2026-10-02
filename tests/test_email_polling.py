"""Tests for bounded email polling/orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

import custom_components.daylight_calendar_import.email_polling as email_polling_module
from custom_components.daylight_calendar_import.direct_imap import (
    DirectImapConnectionError,
)
from custom_components.daylight_calendar_import.email_polling import (
    EmailPollResult,
    async_poll_email_source,
)
from custom_components.daylight_calendar_import.email_source import (
    DirectImapReference,
    EmailDisposition,
    EmailEnvelope,
    EmailProvenance,
    EmailSourceConfig,
    EmailSourceType,
)
from custom_components.daylight_calendar_import.sources import (
    SourceAttachment,
    SourceDocument,
    SourceKind,
)


_RECEIVED_AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _envelope(
    message_id: str,
    *,
    subject: str = "Event",
    body: str = "Friday at 5",
    upstream_source_id: str | None = None,
) -> EmailEnvelope:
    raw = (
        f"Subject: {subject}\r\n"
        f"Message-ID: <{message_id}>\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
        f"{body}"
    ).encode()
    return EmailEnvelope(
        received_at=_RECEIVED_AT,
        raw_message=raw,
        provenance=EmailProvenance(
            source_id="primary-email",
            source_type=EmailSourceType.DIRECT_IMAP,
            transport_reference=DirectImapReference(
                mailbox="INBOX",
                uid_validity=10,
                uid=1,
            ),
        ),
        upstream_source_id=upstream_source_id,
    )


class FakeSource:
    """Deterministic email source for one or more poll invocations."""

    def __init__(self, cycles: list[list[EmailEnvelope] | Exception]) -> None:
        self._cycles = cycles
        self.calls = 0
        self._config = EmailSourceConfig(source_id="primary-email")

    @property
    def config(self) -> EmailSourceConfig:
        return self._config

    async def async_collect(self) -> AsyncIterator[EmailEnvelope]:
        cycle = self._cycles[self.calls]
        self.calls += 1
        if isinstance(cycle, Exception):
            raise cycle
        for envelope in cycle:
            yield envelope

    async def async_acknowledge(
        self,
        provenance: EmailProvenance,
        *,
        disposition: EmailDisposition,
    ) -> None:
        raise AssertionError("polling must not acknowledge upstream messages")


class FakeStore:
    """Minimal source-claim store used by orchestration tests."""

    def __init__(self) -> None:
        self.claimed_source_ids: set[str] = set()
        self.claim_calls: list[str] = []
        self.claim_requests: list[dict[str, object]] = []
        self.failures: list[str] = []
        self.fail_claim = False
        self._counter = 0

    async def async_begin_source_submission(
        self,
        *,
        source_id: str,
        source_kind: str,
        source_title: str | None,
        received_at: datetime,
    ) -> str | None:
        self.claim_calls.append(source_id)
        self.claim_requests.append({
            "source_id": source_id,
            "source_kind": source_kind,
            "source_title": source_title,
            "received_at": received_at,
        })
        if self.fail_claim:
            raise RuntimeError("storage unavailable")
        if source_id in self.claimed_source_ids:
            return None
        self.claimed_source_ids.add(source_id)
        self._counter += 1
        return f"activity-{self._counter}"

    async def async_record_parse_failure(self, activity_id: str) -> None:
        self.failures.append(activity_id)


async def test_poll_processes_every_eligible_message_in_one_cycle() -> None:
    source = FakeSource(
        [[
            _envelope("one@example.test", subject="One"),
            _envelope("two@example.test", subject="Two"),
            _envelope("three@example.test", subject="Three"),
        ]]
    )
    store = FakeStore()
    processed: list[tuple[str | None, str]] = []

    async def processor(document, activity_id):
        processed.append((document.title, activity_id))

    result = await async_poll_email_source(source, store, processor)

    assert result == EmailPollResult(
        discovered=3,
        claimed=3,
        processed=3,
        duplicates=0,
        normalization_failures=0,
        processing_failures=0,
    )
    assert processed == [
        ("One", "activity-1"),
        ("Two", "activity-2"),
        ("Three", "activity-3"),
    ]
    assert store.claim_requests == [
        {
            "source_id": "<one@example.test>",
            "source_kind": "email",
            "source_title": "One",
            "received_at": _RECEIVED_AT,
        },
        {
            "source_id": "<two@example.test>",
            "source_kind": "email",
            "source_title": "Two",
            "received_at": _RECEIVED_AT,
        },
        {
            "source_id": "<three@example.test>",
            "source_kind": "email",
            "source_title": "Three",
            "received_at": _RECEIVED_AT,
        },
    ]


async def test_poll_skips_previously_claimed_duplicate_before_processor() -> None:
    source = FakeSource(
        [[
            _envelope("same@example.test"),
            _envelope("same@example.test"),
        ]]
    )
    store = FakeStore()
    processed: list[str] = []

    async def processor(document, activity_id):
        del document
        processed.append(activity_id)

    result = await async_poll_email_source(source, store, processor)

    assert result.duplicates == 1
    assert result.claimed == 1
    assert result.processed == 1
    assert processed == ["activity-1"]


async def test_normalization_failure_does_not_block_later_message() -> None:
    broken = EmailEnvelope(
        received_at=_RECEIVED_AT,
        raw_message=(
            b"Subject: Broken\r\n"
            b"Message-ID: <broken@example.test>\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Disposition: attachment; filename*\r\n\r\n"
            b"Body"
        ),
        provenance=_envelope("base@example.test").provenance,
    )
    source = FakeSource(
        [[broken, _envelope("good@example.test", subject="Good")]]
    )
    store = FakeStore()
    processed: list[str | None] = []

    async def processor(document, activity_id):
        del activity_id
        processed.append(document.title)

    result = await async_poll_email_source(source, store, processor)

    assert result.discovered == 2
    assert result.normalization_failures == 1
    assert result.processed == 1
    assert processed == ["Good"]


async def test_unusable_upstream_identity_is_skipped_before_claim() -> None:
    source = FakeSource(
        [[_envelope("ignored@example.test", upstream_source_id="   ")]]
    )
    store = FakeStore()

    async def processor(document, activity_id):
        raise AssertionError((document, activity_id))

    result = await async_poll_email_source(source, store, processor)

    assert result == EmailPollResult(
        discovered=1,
        claimed=0,
        processed=0,
        duplicates=0,
        normalization_failures=1,
        processing_failures=0,
    )
    assert store.claim_calls == []


async def test_processing_failure_is_recorded_and_later_message_continues() -> None:
    source = FakeSource(
        [[
            _envelope("bad@example.test", subject="Bad"),
            _envelope("good@example.test", subject="Good"),
        ]]
    )
    store = FakeStore()
    processed: list[str | None] = []

    async def processor(document, activity_id):
        processed.append(document.title)
        if document.title == "Bad":
            raise RuntimeError("AI unavailable")
        assert activity_id == "activity-2"

    result = await async_poll_email_source(source, store, processor)

    assert result.processing_failures == 1
    assert result.processed == 1
    assert processed == ["Bad", "Good"]
    assert store.failures == ["activity-1"]


async def test_claim_storage_failure_aborts_cycle_before_processing() -> None:
    source = FakeSource(
        [[_envelope("one@example.test"), _envelope("two@example.test")]]
    )
    store = FakeStore()
    store.fail_claim = True
    processed = False

    async def processor(document, activity_id):
        nonlocal processed
        processed = True
        raise AssertionError((document, activity_id))

    with pytest.raises(RuntimeError, match="storage unavailable"):
        await async_poll_email_source(source, store, processor)

    assert processed is False
    assert store.claim_calls == ["<one@example.test>"]


async def test_transport_failure_aborts_only_that_cycle_and_next_poll_reconnects() -> None:
    source = FakeSource(
        [
            DirectImapConnectionError("connection lost"),
            [_envelope("after@example.test", subject="After reconnect")],
        ]
    )
    store = FakeStore()
    processed: list[str | None] = []

    async def processor(document, activity_id):
        del activity_id
        processed.append(document.title)

    with pytest.raises(DirectImapConnectionError, match="connection lost"):
        await async_poll_email_source(source, store, processor)

    result = await async_poll_email_source(source, store, processor)

    assert source.calls == 2
    assert result.processed == 1
    assert processed == ["After reconnect"]


async def test_processing_failure_checkpoint_failure_aborts_cycle() -> None:
    source = FakeSource(
        [[_envelope("one@example.test"), _envelope("two@example.test")]]
    )

    class FailingFailureStore(FakeStore):
        async def async_record_parse_failure(self, activity_id: str) -> None:
            del activity_id
            raise RuntimeError("checkpoint unavailable")

    store = FailingFailureStore()
    processed: list[str | None] = []

    async def processor(document, activity_id):
        del activity_id
        processed.append(document.title)
        raise RuntimeError("AI failed")

    with pytest.raises(RuntimeError, match="checkpoint unavailable"):
        await async_poll_email_source(source, store, processor)

    assert processed == ["Event"]


async def test_poll_cancellation_releases_claim_and_reraises() -> None:
    source = FakeSource([[_envelope("cancelled@example.test")]])
    store = FakeStore()

    async def processor(document, activity_id):
        del document, activity_id
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await async_poll_email_source(source, store, processor)

    assert store.failures == ["activity-1"]


async def test_poll_cancellation_keeps_original_cancellation_if_checkpoint_fails() -> None:
    source = FakeSource([[_envelope("cancelled@example.test")]])

    class FailingFailureStore(FakeStore):
        async def async_record_parse_failure(self, activity_id: str) -> None:
            del activity_id
            raise RuntimeError("checkpoint unavailable")

    store = FailingFailureStore()

    async def processor(document, activity_id):
        del document, activity_id
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await async_poll_email_source(source, store, processor)



async def test_multiple_normalization_failures_are_counted_individually() -> None:
    broken = EmailEnvelope(
        received_at=_RECEIVED_AT,
        raw_message=(
            b"Subject: Broken\r\n"
            b"Message-ID: <broken@example.test>\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Disposition: attachment; filename*\r\n\r\n"
            b"Body"
        ),
        provenance=_envelope("base@example.test").provenance,
    )
    source = FakeSource(
        [[broken, broken, _envelope("good@example.test", subject="Good")]]
    )
    store = FakeStore()
    processed: list[str | None] = []

    async def processor(document, activity_id):
        del activity_id
        processed.append(document.title)

    result = await async_poll_email_source(source, store, processor)

    assert result == EmailPollResult(
        discovered=3,
        claimed=1,
        processed=1,
        duplicates=0,
        normalization_failures=2,
        processing_failures=0,
    )
    assert processed == ["Good"]


async def test_missing_normalized_identities_are_counted_and_do_not_stop_cycle(
    monkeypatch,
) -> None:
    envelopes = [
        _envelope("one@example.test"),
        _envelope("two@example.test"),
        _envelope("good@example.test"),
    ]
    documents = iter([
        SourceDocument(
            id="doc-1",
            kind=SourceKind.EMAIL,
            received_at=_RECEIVED_AT,
            title="No identity 1",
            upstream_source_id=None,
        ),
        SourceDocument(
            id="doc-2",
            kind=SourceKind.EMAIL,
            received_at=_RECEIVED_AT,
            title="No identity 2",
            upstream_source_id=None,
        ),
        SourceDocument(
            id="doc-3",
            kind=SourceKind.EMAIL,
            received_at=_RECEIVED_AT,
            title="Good",
            upstream_source_id="<good@example.test>",
        ),
    ])
    monkeypatch.setattr(
        email_polling_module,
        "normalize_email",
        lambda envelope: next(documents),
    )
    source = FakeSource([envelopes])
    store = FakeStore()
    processed: list[str | None] = []

    async def processor(document, activity_id):
        del activity_id
        processed.append(document.title)

    result = await async_poll_email_source(source, store, processor)

    assert result == EmailPollResult(
        discovered=3,
        claimed=1,
        processed=1,
        duplicates=0,
        normalization_failures=2,
        processing_failures=0,
    )
    assert store.claim_calls == ["<good@example.test>"]
    assert processed == ["Good"]


async def test_multiple_duplicates_are_counted_and_do_not_stop_cycle() -> None:
    source = FakeSource([[
        _envelope("dup-one@example.test"),
        _envelope("dup-two@example.test"),
        _envelope("good@example.test", subject="Good"),
    ]])
    store = FakeStore()
    store.claimed_source_ids.update({
        "<dup-one@example.test>",
        "<dup-two@example.test>",
    })
    processed: list[str | None] = []

    async def processor(document, activity_id):
        del activity_id
        processed.append(document.title)

    result = await async_poll_email_source(source, store, processor)

    assert result == EmailPollResult(
        discovered=3,
        claimed=1,
        processed=1,
        duplicates=2,
        normalization_failures=0,
        processing_failures=0,
    )
    assert processed == ["Good"]


async def test_multiple_processing_failures_are_counted_individually() -> None:
    source = FakeSource([[
        _envelope("bad-one@example.test", subject="Bad one"),
        _envelope("bad-two@example.test", subject="Bad two"),
        _envelope("good@example.test", subject="Good"),
    ]])
    store = FakeStore()
    processed: list[str | None] = []

    async def processor(document, activity_id):
        del activity_id
        processed.append(document.title)
        if document.title != "Good":
            raise RuntimeError("AI unavailable")

    result = await async_poll_email_source(source, store, processor)

    assert result == EmailPollResult(
        discovered=3,
        claimed=3,
        processed=1,
        duplicates=0,
        normalization_failures=0,
        processing_failures=2,
    )
    assert processed == ["Bad one", "Bad two", "Good"]
    assert store.failures == ["activity-1", "activity-2"]


class FakeAttachmentStage:
    def __init__(
        self,
        document: SourceDocument,
        *,
        cleanup_error: BaseException | None = None,
    ) -> None:
        self.document = document
        self.cleanup_error = cleanup_error
        self.cleanup_contexts: list[str] = []

    async def async_cleanup(self, context: str) -> None:
        self.cleanup_contexts.append(context)
        if self.cleanup_error is not None:
            raise self.cleanup_error


async def test_attachment_stage_wraps_one_processor_call() -> None:
    source = FakeSource([[_envelope("attachment@example.test", subject="With attachment")]])
    store = FakeStore()
    stages: list[tuple[EmailEnvelope, SourceDocument]] = []
    attachment = SourceAttachment(
        id="attachment-1",
        media_type="image/png",
        size_bytes=12,
        content_ref="media-source://media_source/local/staged.png",
    )
    staged_document = SourceDocument(
        id="document-1",
        kind=SourceKind.EMAIL,
        received_at=_RECEIVED_AT,
        text="Friday at 5",
        title="With attachment",
        attachments=(attachment,),
        upstream_source_id="<attachment@example.test>",
    )
    stage = FakeAttachmentStage(staged_document)

    async def stager(envelope, document):
        stages.append((envelope, document))
        return stage

    async def processor(document, activity_id):
        assert activity_id == "activity-1"
        assert document is staged_document

    result = await async_poll_email_source(
        source,
        store,
        processor,
        attachment_stager=stager,
    )

    assert result.processed == 1
    assert len(stages) == 1
    assert stages[0][0].raw_message.startswith(b"Subject: With attachment")
    assert stages[0][1].attachments == ()
    assert stage.cleanup_contexts == ["successful processing"]


async def test_attachment_staging_failure_releases_claim_and_continues() -> None:
    source = FakeSource(
        [[
            _envelope("bad-attachment@example.test", subject="Bad attachment"),
            _envelope("good@example.test", subject="Good"),
        ]]
    )
    store = FakeStore()
    processed: list[str | None] = []

    async def stager(_envelope, document):
        if document.title == "Bad attachment":
            raise RuntimeError("staging failed")
        return FakeAttachmentStage(document)

    async def processor(document, activity_id):
        del activity_id
        processed.append(document.title)

    result = await async_poll_email_source(
        source,
        store,
        processor,
        attachment_stager=stager,
    )

    assert result == EmailPollResult(
        discovered=2,
        claimed=2,
        processed=1,
        duplicates=0,
        normalization_failures=0,
        processing_failures=1,
    )
    assert store.failures == ["activity-1"]
    assert processed == ["Good"]


async def test_attachment_staging_cancellation_releases_claim_and_reraises() -> None:
    source = FakeSource([[_envelope("cancel-stage@example.test")]])
    store = FakeStore()

    async def stager(_envelope, _document):
        raise asyncio.CancelledError

    async def processor(document, activity_id):
        raise AssertionError((document, activity_id))

    with pytest.raises(asyncio.CancelledError):
        await async_poll_email_source(
            source,
            store,
            processor,
            attachment_stager=stager,
        )

    assert store.failures == ["activity-1"]


async def test_processor_failure_cleans_stage_then_releases_claim() -> None:
    source = FakeSource([[_envelope("processor-failure@example.test")]])
    store = FakeStore()
    stage = FakeAttachmentStage(_document_for_polling())

    async def stager(_envelope, _document):
        return stage

    async def processor(_document, _activity_id):
        raise RuntimeError("processor failed")

    result = await async_poll_email_source(
        source,
        store,
        processor,
        attachment_stager=stager,
    )

    assert result.processing_failures == 1
    assert stage.cleanup_contexts == ["processor failure"]
    assert store.failures == ["activity-1"]


async def test_processor_cancellation_cleans_stage_then_releases_claim() -> None:
    source = FakeSource([[_envelope("processor-cancel@example.test")]])
    store = FakeStore()
    stage = FakeAttachmentStage(_document_for_polling())

    async def stager(_envelope, _document):
        return stage

    async def processor(_document, _activity_id):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await async_poll_email_source(
            source,
            store,
            processor,
            attachment_stager=stager,
        )

    assert stage.cleanup_contexts == ["processor cancellation"]
    assert store.failures == ["activity-1"]


async def test_cleanup_cancellation_after_processor_failure_releases_claim_then_stops() -> None:
    source = FakeSource([[
        _envelope("cleanup-cancel@example.test", subject="Bad"),
        _envelope("later@example.test", subject="Later"),
    ]])
    store = FakeStore()
    stage = FakeAttachmentStage(
        _document_for_polling(),
        cleanup_error=asyncio.CancelledError(),
    )
    processed: list[str | None] = []

    async def stager(_envelope, document):
        return stage if document.title == "Bad" else FakeAttachmentStage(document)

    async def processor(document, _activity_id):
        processed.append(document.title)
        raise RuntimeError("processor failed")

    with pytest.raises(asyncio.CancelledError):
        await async_poll_email_source(
            source,
            store,
            processor,
            attachment_stager=stager,
        )

    assert stage.cleanup_contexts == ["processor failure"]
    assert store.failures == ["activity-1"]
    assert processed == ["Event"]
    assert store.claim_calls == ["<cleanup-cancel@example.test>"]


async def test_cleanup_cancellation_after_success_keeps_claim() -> None:
    source = FakeSource([[_envelope("cleanup-after-success@example.test")]])
    store = FakeStore()
    stage = FakeAttachmentStage(
        _document_for_polling(),
        cleanup_error=asyncio.CancelledError(),
    )

    async def stager(_envelope, _document):
        return stage

    async def processor(_document, _activity_id):
        return None

    with pytest.raises(asyncio.CancelledError):
        await async_poll_email_source(
            source,
            store,
            processor,
            attachment_stager=stager,
        )

    assert store.failures == []


def _document_for_polling() -> SourceDocument:
    return SourceDocument(
        id="stage-doc",
        kind=SourceKind.EMAIL,
        received_at=_RECEIVED_AT,
        text="Friday at 5",
        title="Event",
        upstream_source_id="<stage@example.test>",
    )


async def test_processor_cancellation_ignores_cleanup_cancellation_before_claim_release() -> None:
    source = FakeSource([[_envelope("processor-and-cleanup-cancel@example.test")]])
    store = FakeStore()
    stage = FakeAttachmentStage(
        _document_for_polling(),
        cleanup_error=asyncio.CancelledError(),
    )

    async def stager(_envelope, _document):
        return stage

    async def processor(_document, _activity_id):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await async_poll_email_source(
            source,
            store,
            processor,
            attachment_stager=stager,
        )

    assert stage.cleanup_contexts == ["processor cancellation"]
    assert store.failures == ["activity-1"]


async def test_cleanup_cancellation_preserves_shutdown_when_claim_release_fails(
    caplog,
) -> None:
    source = FakeSource([[
        _envelope("cleanup-release-fails@example.test", subject="Bad"),
        _envelope("later@example.test", subject="Later"),
    ]])

    class FailingReleaseStore(FakeStore):
        async def async_record_parse_failure(self, activity_id: str) -> None:
            self.failures.append(activity_id)
            raise RuntimeError("storage unavailable")

    store = FailingReleaseStore()
    stage = FakeAttachmentStage(
        _document_for_polling(),
        cleanup_error=asyncio.CancelledError(),
    )
    processed: list[str | None] = []

    async def stager(_envelope, document):
        return stage if document.title == "Bad" else FakeAttachmentStage(document)

    async def processor(document, _activity_id):
        processed.append(document.title)
        raise RuntimeError("processor failed")

    with pytest.raises(asyncio.CancelledError):
        await async_poll_email_source(
            source,
            store,
            processor,
            attachment_stager=stager,
        )

    assert store.failures == ["activity-1"]
    assert store.claim_calls == ["<cleanup-release-fails@example.test>"]
    assert processed == ["Event"]
    assert "storage unavailable" in caplog.text
    assert "activity-1" in caplog.text


async def test_cleanup_cancellation_preserves_shutdown_when_claim_release_is_cancelled() -> None:
    source = FakeSource([[_envelope("cleanup-release-cancel@example.test")]])

    class CancelledReleaseStore(FakeStore):
        async def async_record_parse_failure(self, activity_id: str) -> None:
            self.failures.append(activity_id)
            raise asyncio.CancelledError

    store = CancelledReleaseStore()
    stage = FakeAttachmentStage(
        _document_for_polling(),
        cleanup_error=asyncio.CancelledError(),
    )

    async def stager(_envelope, _document):
        return stage

    async def processor(_document, _activity_id):
        raise RuntimeError("processor failed")

    with pytest.raises(asyncio.CancelledError):
        await async_poll_email_source(
            source,
            store,
            processor,
            attachment_stager=stager,
        )

    assert store.failures == ["activity-1"]


async def test_claim_release_failure_log_message_is_exact(caplog) -> None:
    source = FakeSource([[_envelope("release-log@example.test")]])

    class FailingReleaseStore(FakeStore):
        async def async_record_parse_failure(self, activity_id: str) -> None:
            raise RuntimeError("storage unavailable")

    store = FailingReleaseStore()
    stage = FakeAttachmentStage(
        _document_for_polling(),
        cleanup_error=asyncio.CancelledError(),
    )

    async def stager(_envelope, _document):
        return stage

    async def processor(_document, _activity_id):
        raise RuntimeError("processor failed")

    with pytest.raises(asyncio.CancelledError):
        await async_poll_email_source(
            source,
            store,
            processor,
            attachment_stager=stager,
        )

    assert caplog.records[-1].getMessage() == (
        "Failed to release email source claim activity-1 during cancellation: "
        "storage unavailable"
    )
