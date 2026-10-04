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
from .providers import SourceValidationError
from .sources import SourceDocument
from .storage import PendingImportStore

_LOGGER = logging.getLogger(__name__)

_SAFETY_REJECTION_GUIDANCE = (
    "Email was rejected by the configured sender safety policy and left untouched."
)
_NORMALIZATION_FAILURE_GUIDANCE = (
    "Email could not be normalized and was left untouched for a later retry."
)
_IDENTITY_FAILURE_GUIDANCE = (
    "Email did not produce a stable source identity and was left untouched."
)
_TERMINAL_SOURCE_VALIDATION_CODES = frozenset(
    {
        "empty_source",
        "empty_attachment",
        "invalid_attachment",
        "too_many_attachments",
        "source_too_large",
        "unsupported_media",
        "unsupported_capability",
    }
)
_TERMINAL_SOURCE_GUIDANCE = {
    "empty_source": (
        "Email has no supported parser input. Correct the content and resend it "
        "as a new message."
    ),
    "empty_attachment": (
        "Email contains an empty supported attachment. Correct the attachment "
        "and resend it as a new message."
    ),
    "invalid_attachment": (
        "Email contains a malformed or undecodable supported attachment. "
        "Correct the attachment and resend it as a new message."
    ),
    "too_many_attachments": (
        "Email exceeds the supported attachment count. Reduce or split the "
        "attachments and resend it as a new message."
    ),
    "source_too_large": (
        "Email exceeds the supported source size. Reduce the message or "
        "attachments and resend it as a new message."
    ),
    "unsupported_media": (
        "Email contains parser media that is not supported. Resend it with "
        "supported content."
    ),
    "unsupported_capability": (
        "The configured AI Task cannot process this email content. Correct the "
        "configuration or content, then resend it as a new message."
    ),
}


def _terminal_source_guidance(error: Exception) -> str | None:
    """Return terminal guidance only for deterministic source validation errors."""
    if not isinstance(error, SourceValidationError):
        return None
    if error.code not in _TERMINAL_SOURCE_VALIDATION_CODES:
        return None
    return _TERMINAL_SOURCE_GUIDANCE[error.code]


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
    terminal_failures: int = 0
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
    terminal_failures = 0
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
        activity_id = await store.async_begin_source_discovery(
            source_kind="email",
            source_title="Email",
            received_at=envelope.received_at,
        )
        if (
            sender_allowlist is not None
            and not sender_allowlist.allows(envelope.raw_message)
        ):
            await store.async_record_source_failure(
                activity_id,
                _SAFETY_REJECTION_GUIDANCE,
            )
            safety_rejections += 1
            continue
        try:
            document = normalize_email(envelope)
        except EmailNormalizationError:
            await store.async_record_source_failure(
                activity_id,
                _NORMALIZATION_FAILURE_GUIDANCE,
            )
            normalization_failures += 1
            continue

        source_id = document.upstream_source_id or ""
        if not source_id.strip():
            await store.async_record_source_failure(
                activity_id,
                _IDENTITY_FAILURE_GUIDANCE,
            )
            normalization_failures += 1
            continue

        claimed_source = await store.async_claim_source_discovery(
            activity_id,
            source_id=source_id,
            source_kind=document.kind.value,
            source_title=document.title,
        )
        if not claimed_source:
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
        except Exception as error:
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

            terminal_guidance = _terminal_source_guidance(error)
            if terminal_guidance is not None:
                await store.async_record_terminal_source_failure(
                    activity_id,
                    source_id=source_id,
                    guidance=terminal_guidance,
                )
                terminal_failures += 1
                acknowledgement = await _async_acknowledge_if_durable(
                    source,
                    store,
                    envelope,
                    source_id,
                    disposition,
                )
                if acknowledgement is True:
                    acknowledged += 1
                elif acknowledgement is False:
                    acknowledgement_failures += 1
                continue

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
        terminal_failures=terminal_failures,
        safety_rejections=safety_rejections,
        acknowledged=acknowledged,
        acknowledgement_failures=acknowledgement_failures,
    )
