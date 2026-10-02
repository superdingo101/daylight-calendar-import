"""Bounded orchestration for one self-hosted email polling cycle."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass

from .email_normalize import EmailNormalizationError, normalize_email
from .email_source import EmailEnvelope, EmailSource
from .sources import SourceDocument
from .storage import PendingImportStore


type EmailDocumentProcessor = Callable[[SourceDocument, str], Awaitable[None]]
type EmailAttachmentStager = Callable[
    [EmailEnvelope, SourceDocument],
    AbstractAsyncContextManager[SourceDocument],
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
    """Collect and process every envelope yielded during one poll cycle.

    Transport failures are intentionally allowed to escape. The caller may invoke
    this function again later; Direct IMAP opens a fresh connection for each
    collection attempt. Per-message normalization and processing failures are
    isolated so one bad message does not prevent later eligible messages in the
    same successful collection from being considered.

    The processor receives the normalized document and its durable activity ID.
    An optional attachment stager may add temporary media references around that
    one processor call. Before returning successfully, the processor must hand the
    claimed source into a durable local outcome such as pending review. Upstream
    acknowledgement is deliberately outside this v0.5 slice.
    """
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
        processor_completed = False
        try:
            context = (
                attachment_stager(envelope, document)
                if attachment_stager is not None
                else nullcontext(document)
            )
            async with context as process_document:
                await processor(process_document, activity_id)
                processor_completed = True
        except asyncio.CancelledError:
            if not processor_completed:
                try:
                    await store.async_record_parse_failure(activity_id)
                except (asyncio.CancelledError, Exception):
                    pass
            raise
        except Exception:
            if processor_completed:
                raise
            await store.async_record_parse_failure(activity_id)
            processing_failures += 1
            continue

        processed += 1

    return EmailPollResult(
        discovered=discovered,
        claimed=claimed,
        processed=processed,
        duplicates=duplicates,
        normalization_failures=normalization_failures,
        processing_failures=processing_failures,
    )
