"""Tests for bounded email polling/orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

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
        del source_kind, source_title, received_at
        self.claim_calls.append(source_id)
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


async def test_cancellation_during_failure_checkpoint_completes_release_first() -> None:
    source = FakeSource([[_envelope("checkpoint-cancel@example.test")]])
    checkpoint_started = asyncio.Event()
    release_checkpoint = asyncio.Event()

    class SlowFailureStore(FakeStore):
        async def async_record_parse_failure(self, activity_id: str) -> None:
            checkpoint_started.set()
            await release_checkpoint.wait()
            self.failures.append(activity_id)

    store = SlowFailureStore()

    async def processor(document, activity_id):
        del document, activity_id
        raise RuntimeError("AI failed")

    task = asyncio.create_task(async_poll_email_source(source, store, processor))
    await checkpoint_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    release_checkpoint.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.failures == ["activity-1"]


async def test_cancellation_during_failed_checkpoint_preserves_cancellation() -> None:
    source = FakeSource([[_envelope("checkpoint-fails@example.test")]])
    checkpoint_started = asyncio.Event()
    release_checkpoint = asyncio.Event()

    class SlowFailingFailureStore(FakeStore):
        async def async_record_parse_failure(self, activity_id: str) -> None:
            del activity_id
            checkpoint_started.set()
            await release_checkpoint.wait()
            raise RuntimeError("checkpoint unavailable")

    store = SlowFailingFailureStore()

    async def processor(document, activity_id):
        del document, activity_id
        raise RuntimeError("AI failed")

    task = asyncio.create_task(async_poll_email_source(source, store, processor))
    await checkpoint_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    release_checkpoint.set()

    with pytest.raises(asyncio.CancelledError):
        await task

