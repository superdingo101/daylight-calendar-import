"""Dormant re-lease verification for an accepted hosted text delivery.

The caller holds entry-scoped exclusive lifecycle ownership and uses the exact
credential checkpoint that authenticated the claim. This operation does not
parse, poll, or send an ACK; the caller must check lease authority again before
any subsequent network operation.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
import json

from .hosted_source_transport import _date, _page
from .hosted_transport import HostedServiceBinding, _strict_strings


class HostedTextReLeaseError(ValueError):
    """A delivery cannot be safely rebound to a new lease."""


async def async_re_lease_hosted_text(
    *, store, credentials, claim_checkpoint, service_binding, local_config_entry_id,
    entry, delivery,
):
    """Verify immutable evidence and durably replace the journal lease token."""
    delivery = deepcopy(delivery)
    try:
        _strict_strings(delivery)
        _page({"schema_version": 1, "deliveries": [delivery]}, 1, datetime.now(UTC))
        source = delivery["source"]
        if source["attachments"] or not isinstance(source.get("text"), str) or not source["text"].strip():
            raise ValueError
        canonical = json.dumps(source, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, OverflowError):
        raise HostedTextReLeaseError("invalid hosted text delivery") from None

    credential = claim_checkpoint.current

    def guard() -> None:
        now = datetime.now(UTC)
        if (credentials.snapshot != claim_checkpoint or credential is None
                or type(service_binding) is not HostedServiceBinding
                or claim_checkpoint.service_binding != service_binding
                or not claim_checkpoint.processing_consent
                or claim_checkpoint.local_disconnect != "none"
                or claim_checkpoint.reconnect is not None
                or claim_checkpoint.candidate is not None
                or claim_checkpoint.rotation_not_before is not None
                or entry.evidence.local_config_entry_id != local_config_entry_id
                or not entry.evidence.matches_namespace(*credential.namespace)
                or datetime.fromisoformat(credential.expires_at) <= now
                or _date(delivery["lease_expires_at"]) <= now
                or _date(delivery["source_expires_at"]) <= now):
            raise HostedTextReLeaseError("hosted delivery authority unavailable")

    guard()
    evidence = replace(
        entry.evidence,
        delivery_id=delivery["delivery_id"],
        lease_token=delivery["lease_token"],
        public_source_id=delivery["source_id"],
        source_expires_at=delivery["source_expires_at"].removesuffix("z")
        + ("Z" if delivery["source_expires_at"].endswith("z") else ""),
        source_evidence_sha256=sha256(canonical).hexdigest(),
        attachment_manifest_sha256=sha256(b'{"attachments":[]}').hexdigest(),
        attachment_descriptors=(), verified_attachments=(),
    )
    result = await store.async_reconcile_hosted_ack(
        entry=entry, action="replace_lease", new_evidence=evidence,
    )
    guard()
    return result
