"""Transport-neutral contracts for self-hosted email ingestion."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable


MAX_IMAP_IDENTIFIER = 2**32 - 1


class EmailSourceType(StrEnum):
    """Configured transport used to collect inbound email."""

    DIRECT_IMAP = "direct_imap"


def _validate_imap_identifier(name: str, value: object) -> None:
    """Reject values outside IMAP's nonzero unsigned 32-bit identifier range."""
    if type(value) is not int or not 1 <= value <= MAX_IMAP_IDENTIFIER:
        raise ValueError(f"{name} must be a nonzero unsigned 32-bit integer")


@dataclass(frozen=True, slots=True)
class DirectImapReference:
    """Stable IMAP identity required for safe delayed acknowledgement."""

    mailbox: str
    uid_validity: int
    uid: int

    def __post_init__(self) -> None:
        """Reject identifiers that cannot be valid IMAP message references."""
        if not isinstance(self.mailbox, str) or not self.mailbox.strip():
            raise ValueError("mailbox must be a non-empty string")
        _validate_imap_identifier("uid_validity", self.uid_validity)
        _validate_imap_identifier("uid", self.uid)


type EmailTransportReference = DirectImapReference


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
    disposition: EmailDisposition = field(default_factory=EmailDisposition)
    sender_allowlist: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EmailProvenance:
    """Bounded transport provenance retained for recovery and acknowledgement."""

    source_id: str
    source_type: EmailSourceType
    transport_reference: EmailTransportReference


@dataclass(frozen=True, slots=True)
class EmailEnvelope:
    """Fetched RFC message plus transport-neutral identity and provenance."""

    received_at: datetime
    raw_message: bytes = field(repr=False)
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
        provenance: EmailProvenance,
        *,
        disposition: EmailDisposition,
    ) -> None:
        """Apply upstream disposition after provenance is durably persisted."""
        ...
