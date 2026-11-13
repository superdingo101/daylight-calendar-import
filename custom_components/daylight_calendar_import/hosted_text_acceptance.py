"""Dormant one-shot hosted text delivery into the local review store.

The caller must serialize this operation with credential/lifecycle changes and
other delivery operations for the entry. It must use the credential checkpoint
that actually authenticated the claim. This module does not poll or send ACKs.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
import json
from uuid import uuid4

from .hosted_delivery_journal import HostedDeliveryEvidence
from .hosted_source_transport import _date, _page
from .hosted_transport import HostedServiceBinding, _strict_strings
from .sources import SourceDocument, SourceKind


class HostedTextAcceptanceError(ValueError):
    """A source cannot be safely accepted with the available authority."""


async def async_accept_hosted_text(
    *, store, credentials, claim_checkpoint, service_binding, local_config_entry_id,
    delivery, route_source, parse_source,
):
    """Parse one text claim and atomically persist its outcome and ACK journal.

    route_source is the existing local routing function; parse_source returns the
    existing (ParseOutcome, optional acceptance guard) pair. Both remain owned by
    the runtime and are never selected by source metadata.
    """
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
        raise HostedTextAcceptanceError("invalid hosted text delivery") from None

    checkpoint = claim_checkpoint
    credential = checkpoint.current

    def guard() -> None:
        now = datetime.now(UTC)
        if (credentials.snapshot != checkpoint or credential is None
                or type(service_binding) is not HostedServiceBinding
                or checkpoint.service_binding != service_binding or not checkpoint.processing_consent
                or checkpoint.local_disconnect != "none" or checkpoint.reconnect is not None
                or checkpoint.candidate is not None or checkpoint.rotation_not_before is not None
                or datetime.fromisoformat(credential.expires_at) <= now
                or _date(delivery["lease_expires_at"]) <= now
                or _date(delivery["source_expires_at"]) <= now):
            raise HostedTextAcceptanceError("hosted delivery authority unavailable")

    guard()
    evidence = HostedDeliveryEvidence(
        local_config_entry_id=local_config_entry_id,
        delivery_id=delivery["delivery_id"], lease_token=delivery["lease_token"],
        source_expires_at=delivery["source_expires_at"].removesuffix("z")
        + ("Z" if delivery["source_expires_at"].endswith("z") else ""),
        source_evidence_sha256=sha256(canonical).hexdigest(),
        attachment_manifest_sha256=sha256(b'{"attachments":[]}').hexdigest(),
        account_id=credential.account_id, installation_id=credential.installation_id,
        public_source_id=delivery["source_id"],
    )
    evidence.validate()
    if any(item.evidence.source_id == evidence.source_id for item in store.hosted_ack_journal()):
        raise HostedTextAcceptanceError("hosted delivery already accepted")
    document = SourceDocument(
        id=str(uuid4()), kind=SourceKind.EMAIL,
        received_at=_date(source["received_at"]),
        text=source["text"], title=source.get("title"), metadata=deepcopy(source["metadata"]),
        upstream_source_id=evidence.source_id,
    )
    route = route_source(document)
    outcome, parser_guard = await parse_source(route.parser_source)

    def acceptance_guard() -> None:
        guard()
        if parser_guard is not None:
            parser_guard()

    return await store.async_add(
        source_text=document.text, events=outcome.events,
        event_assumptions=outcome.event_assumptions or None,
        source_id=evidence.source_id, calendar_entity=route.calendar_entity,
        routing_unresolved=route.requires_confirmation, source_kind=document.kind.value,
        source_title=document.title, source_sender=document.metadata.get("sender"),
        warnings=[*outcome.warnings, *route.warnings], acceptance_guard=acceptance_guard,
        hosted_evidence=evidence,
    )
