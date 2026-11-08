"""Private evidence for a durably accepted hosted delivery.

This module does not fetch sources or acknowledge them. The transport must verify
source and attachment bytes before supplying their digests to the store.
"""
from __future__ import annotations

import re
import json
from hashlib import sha256
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class HostedDeliveryEvidence:
    """Evidence prepared only after source and attachment validation."""

    local_config_entry_id: str
    delivery_id: str
    lease_token: str = field(repr=False)
    source_expires_at: str
    source_evidence_sha256: str
    attachment_manifest_sha256: str
    attachment_descriptors: tuple[dict[str, Any], ...] = ()
    verified_attachments: tuple[dict[str, Any], ...] = ()

    def validate(self) -> None:
        """Reject malformed or ambiguous evidence before any storage write."""
        if any(type(value) is not str or _ID.fullmatch(value) is None
               for value in (self.local_config_entry_id, self.delivery_id)):
            raise ValueError("invalid hosted delivery identity")
        if type(self.lease_token) is not str or not 1 <= len(self.lease_token) <= 1024:
            raise ValueError("invalid hosted lease token")
        if any(type(value) is not str or _HASH.fullmatch(value) is None
               for value in (self.source_evidence_sha256, self.attachment_manifest_sha256)):
            raise ValueError("invalid hosted evidence digest")
        descriptors = self.attachment_descriptors
        verified = self.verified_attachments
        if not isinstance(descriptors, (list, tuple)) or not isinstance(verified, (list, tuple)):
            raise ValueError("invalid hosted attachment evidence")
        if len(descriptors) > 4 or len(descriptors) != len(verified):
            raise ValueError("invalid hosted attachment evidence")
        ids = set()
        total = 0
        for descriptor, checked in zip(descriptors, verified, strict=True):
            if (type(descriptor) is not dict or set(descriptor) not in (
                    {"id", "media_type", "size_bytes", "sha256"},
                    {"id", "filename", "media_type", "size_bytes", "sha256"})
                    or type(checked) is not dict or set(checked) != {"id", "size_bytes", "sha256"}):
                raise ValueError("invalid hosted attachment evidence")
            ident, media, size, digest = (descriptor["id"], descriptor["media_type"],
                                           descriptor["size_bytes"], descriptor["sha256"])
            if (type(ident) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", ident) is None
                    or ident in ids or media not in ("image/jpeg", "image/png", "image/webp", "application/pdf")
                    or type(size) is not int or not 1 <= size <= 10485760
                    or type(digest) is not str or _HASH.fullmatch(digest) is None
                    or type(checked["size_bytes"]) is not int
                    or checked != {"id": ident, "size_bytes": size, "sha256": digest}
                    or ("filename" in descriptor and descriptor["filename"] is not None and
                        (type(descriptor["filename"]) is not str or not 1 <= len(descriptor["filename"]) <= 512))):
                raise ValueError("invalid hosted attachment evidence")
            ids.add(ident)
            total += size
        try:
            manifest = json.dumps({"attachments": list(descriptors)}, ensure_ascii=False,
                                  sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, UnicodeEncodeError, ValueError):
            raise ValueError("invalid hosted attachment evidence") from None
        if total > 10485760 or sha256(manifest).hexdigest() != self.attachment_manifest_sha256:
            raise ValueError("invalid hosted attachment manifest")
        try:
            expiry = datetime.fromisoformat(self.source_expires_at.replace("Z", "+00:00"))
            if expiry.tzinfo is None or expiry.utcoffset() is None:
                raise ValueError
        except (AttributeError, TypeError, ValueError):
            raise ValueError("invalid hosted source expiry") from None

    @property
    def source_id(self) -> str:
        """Create the installation-scoped deduplication identity."""
        return f"hosted:{self.local_config_entry_id}:{self.delivery_id}"


@dataclass(frozen=True, slots=True)
class HostedDeliveryJournalEntry:
    """A private ACK obligation independent of the review lifecycle."""

    evidence: HostedDeliveryEvidence
    source_fingerprint: str
    acceptance_disposition: str
    ack_status: str = "pending"
    attempt_count: int = 0
    last_attempt_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self.evidence), "source_fingerprint": self.source_fingerprint,
                "acceptance_disposition": self.acceptance_disposition,
                "ack_status": self.ack_status, "attempt_count": self.attempt_count,
                "last_attempt_at": self.last_attempt_at}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> HostedDeliveryJournalEntry:
        fields = {"local_config_entry_id", "delivery_id", "lease_token", "source_expires_at",
                  "source_evidence_sha256", "attachment_manifest_sha256", "attachment_descriptors",
                  "verified_attachments", "source_fingerprint",
                  "acceptance_disposition", "ack_status", "attempt_count", "last_attempt_at"}
        if type(raw) is not dict or set(raw) != fields:
            raise ValueError("invalid hosted delivery journal")
        evidence_fields = {key: raw[key] for key in fields if key in HostedDeliveryEvidence.__dataclass_fields__}
        evidence = HostedDeliveryEvidence(**evidence_fields)
        evidence.validate()
        evidence = HostedDeliveryEvidence(**{**evidence_fields,
            "attachment_descriptors": tuple(evidence.attachment_descriptors),
            "verified_attachments": tuple(evidence.verified_attachments)})
        if (raw["acceptance_disposition"] not in ("pending", "duplicate", "no_events")
                or raw["ack_status"] not in ("pending", "confirmed", "expired")
                or type(raw["attempt_count"]) is not int or raw["attempt_count"] < 0
                or (raw["last_attempt_at"] is not None and type(raw["last_attempt_at"]) is not str)):
            raise ValueError("invalid hosted delivery journal")
        from .dedup import source_fingerprint
        if raw["source_fingerprint"] != source_fingerprint(evidence.source_id):
            raise ValueError("invalid hosted source fingerprint")
        return cls(evidence, raw["source_fingerprint"], raw["acceptance_disposition"],
                   raw["ack_status"], raw["attempt_count"], raw["last_attempt_at"])
