"""Normalized source contracts for text and future attachment ingestion."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


class SourceKind(StrEnum):
    """Origin of a document before parsing."""

    MANUAL_TEXT = "manual_text"
    EMAIL = "email"
    IMAGE = "image"
    PDF = "pdf"


@dataclass(frozen=True, slots=True)
class SourceAttachment:
    """Attachment metadata; bytes live outside the review store."""

    id: str
    media_type: str
    size_bytes: int
    content_ref: str
    filename: str | None = None
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class SourceDocument:
    """One normalized source, with optional text and attachment references."""

    id: str
    kind: SourceKind
    received_at: datetime
    text: str | None = None
    title: str | None = None
    attachments: tuple[SourceAttachment, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    upstream_source_id: str | None = None


class TextSourceAdapter:
    """Normalize the existing text actions before parsing or deduplication."""

    def create(self, text: str, *, source_id: str | None = None) -> SourceDocument:
        """Make a text document without persisting its raw upstream ID."""
        clean_text = text.strip()
        if not clean_text:
            raise ValueError("text must not be empty")
        return SourceDocument(
            id=str(uuid4()),
            kind=SourceKind.MANUAL_TEXT,
            received_at=datetime.now(UTC),
            text=clean_text,
            upstream_source_id=source_id,
        )
