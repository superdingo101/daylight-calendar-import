"""Transport-neutral contracts for self-hosted email ingestion."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable


class EmailSourceType(StrEnum):
    """Configured transport used to collect inbound email."""

    DIRECT_IMAP = "direct_imap"


@dataclass(frozen=True, slots=True)
class EmailDisposition:
    """Requested upstream handling after a durable local checkpoint."""

    mark_seen: bool = False
    move_to_folder: str | None = None
    add_flag: str | None = None


@dataclass(frozen=True, slots=True)
class EmailSourceConfig:
    """Transport-neutral configuration shared by email-source adapters."""

    source_id: str
    source_type: EmailSourceType = EmailSourceType.DIRECT_IMAP
    disposition: EmailDisposition = EmailDisposition()


@dataclass(frozen=True, slots=True)
class EmailProvenance:
    """Bounded transport provenance retained for recovery and acknowledgement."""

    source_id: str
    source_type: EmailSourceType
    transport_reference: str
    mailbox: str | None = None


@dataclass(frozen=True, slots=True)
class EmailEnvelope:
    """Fetched RFC message plus transport-neutral identity and provenance."""

    received_at: datetime
    raw_message: bytes
    provenance: EmailProvenance
    upstream_source_id: str | None = None


@runtime_checkable
class EmailSource(Protocol):
    """Collect email and apply transport-specific acknowledgement safely."""

    @property
    def config(self) -> EmailSourceConfig:
        """Return the source configuration without transport secrets."""
        ...

    def async_collect(self) -> AsyncIterator[EmailEnvelope]:
        """Yield every eligible message discovered by this source."""
        ...

    async def async_acknowledge(
        self,
        envelope: EmailEnvelope,
        *,
        disposition: EmailDisposition,
    ) -> None:
        """Apply the requested upstream disposition after durable persistence."""
        ...
