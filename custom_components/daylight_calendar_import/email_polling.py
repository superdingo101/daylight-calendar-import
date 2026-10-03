"""Bounded orchestration for one self-hosted email polling cycle."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
from dataclasses import dataclass
from typing import Protocol

from .email_normalize import EmailNormalizationError, normalize_email
from .email_safety import ExactSenderAllowlist
from .email_source import EmailDisposition, EmailEnvelope, EmailSource
from .sources import SourceDocument
from .storage import PendingImportStore

_LOGGER = logging.getLogger(__name__)


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


async def _async_acknowledge_if_durable(
    source: EmailSource,
    store: PendingImportStore,
    envelope: EmailEnvelope,
    source_id: str,
    disposition: EmailDisposition,
) -> bool | None:
    """Acknowledge only after the source identity is known to be durable."""
    if disposition == EmailDisposition():
        return None
    if not store.is_source_durable(source_id):
        return None
    try:
        await source.async_acknowledge(
            envelope.provenance,
            disposition=disposition,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return False
    return True


@dataclass(frozen=True, slots=True)
class EmailPollResult:
    """Outcome counts for one complete source collection attempt."""

    discovered: int
    claimed: int
    processed: int
    duplicates: int
    normalization_failures: int
    processing_failures: int
    safety_rejections: int = 0
    acknowledged: int = 0
    acknowledgement_failures: int = 0


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
    safety_rejections = 0
    acknowledged = 0
    acknowledgement_failures = 0
    disposition = source.config.disposition
    sender_allowlist = (
        ExactSenderAllowlist(source.config.sender_allowlist)
        if source.config.sender_allowlist
        else None
    )

    async for envelope in source.async_collect():
        discovered += 1
        if (
            sender_allowlist is not None
            and not sender_allowlist.allows(envelope.raw_message)
        ):
            safety_rejections += 1
            continue
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
            acknowledgement = await _async_acknowledge_if_durable(
                source, store, envelope, source_id, disposition
            )
            if acknowledgement is True:
                acknowledged += 1
            elif acknowledgement is False:
                acknowledgement_failures += 1
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
            if cleanup_cancelled:
                try:
                    await store.async_record_parse_failure(activity_id)
                except asyncio.CancelledError:
                    pass
                except Exception as release_error:
                    _LOGGER.error(
                        "Failed to release email source claim %s during cancellation: %s",
                        activity_id,
                        release_error,
                    )
                raise asyncio.CancelledError
            await store.async_record_parse_failure(activity_id)
            processing_failures += 1
            continue

        processed += 1
        if stage is not None:
            await stage.async_cleanup("successful processing")
        acknowledgement = await _async_acknowledge_if_durable(
            source, store, envelope, source_id, disposition
        )
        if acknowledgement is True:
            acknowledged += 1
        elif acknowledgement is False:
            acknowledgement_failures += 1

    return EmailPollResult(
        discovered=discovered,
        claimed=claimed,
        processed=processed,
        duplicates=duplicates,
        normalization_failures=normalization_failures,
        processing_failures=processing_failures,
        safety_rejections=safety_rejections,
        acknowledged=acknowledged,
        acknowledgement_failures=acknowledgement_failures,
    )
