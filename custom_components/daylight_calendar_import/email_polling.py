"""Bounded orchestration for one self-hosted email polling cycle."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from .email_normalize import EmailNormalizationError, normalize_email
from .email_source import EmailEnvelope, EmailSource
from .sources import SourceDocument
from .storage import PendingImportStore


type EmailDocumentProcessor = Callable[[SourceDocument, str], Awaitable[None]]


class StagedEmailDocument(Protocol):
    """Temporary resources associated with one email parse call."""

    @property
    def document(self) -> SourceDocument:
        """Return the document passed to the processor."""

    async def async_cleanup(self, context: str) -> None:
        """Release temporary resources."""


type EmailAttachmentStager = Callable[
    [EmailEnvelope, SourceDocument],
    Awaitable[StagedEmailDocument],
]


@dataclass(frozen=True, slots=True)
class EmailPollResult:
    """Outcome counts for one complete source collection attempt."""

    discovered: int
    claimed: int
    processed: int
    duplicates: int
    normalization_failures: int
    processing_failures: int


async def async_poll_email_source(
    source: EmailSource,
    store: PendingImportStore,
    processor: EmailDocumentProcessor,
    *,
    attachment_stager: EmailAttachmentStager | None = None,
) -> EmailPollResult:
    """Collect and process every envelope yielded during one poll cycle."""
    discovered = 0
    claimed = 0
    processed = 0
    duplicates = 0
    normalization_failures = 0
    processing_failures = 0

    async for envelope in source.async_collect():
        discovered += 1
        try:
            document = normalize_email(envelope)
        except EmailNormalizationError:
            normalization_failures += 1
            continue

        source_id = document.upstream_source_id or ""
        if not source_id.strip():
            normalization_failures += 1
            continue

        activity_id = await store.async_begin_source_submission(
            source_id=source_id,
            source_kind=document.kind.value,
            source_title=document.title,
            received_at=document.received_at,
        )
        if activity_id is None:
            duplicates += 1
            continue

        claimed += 1
        stage: StagedEmailDocument | None = None
        try:
            process_document = document
            if attachment_stager is not None:
                stage = await attachment_stager(envelope, document)
                process_document = stage.document
            await processor(process_document, activity_id)
        except asyncio.CancelledError:
            if stage is not None:
                try:
                    await stage.async_cleanup("processor cancellation")
                except asyncio.CancelledError:
                    pass
            try:
                await store.async_record_parse_failure(activity_id)
            except (asyncio.CancelledError, Exception):
                pass
            raise
        except Exception:
            cleanup_cancelled = False
            if stage is not None:
                try:
                    await stage.async_cleanup("processor failure")
                except asyncio.CancelledError:
                    cleanup_cancelled = True
            await store.async_record_parse_failure(activity_id)
            if cleanup_cancelled:
                raise asyncio.CancelledError
            processing_failures += 1
            continue

        processed += 1
        if stage is not None:
            await stage.async_cleanup("successful processing")

    return EmailPollResult(
        discovered=discovered,
        claimed=claimed,
        processed=processed,
        duplicates=duplicates,
        normalization_failures=normalization_failures,
        processing_failures=processing_failures,
    )
