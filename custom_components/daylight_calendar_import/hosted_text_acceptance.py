"""Dormant one-shot hosted text delivery into the local review store.

The caller must serialize this operation with credential/lifecycle changes and
other delivery operations for the entry. It must use the credential checkpoint
that actually authenticated the claim. This module does not poll or send ACKs.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
import json
from uuid import uuid4

from .hosted_delivery_journal import HostedDeliveryEvidence
from .hosted_attachment_acceptance import async_verify_attachments, manifest_digest
from .hosted_source_transport import _date, _page
from .hosted_transport import HostedServiceBinding, _strict_strings
from .sources import SourceDocument, SourceKind


class HostedTextAcceptanceError(ValueError):
    """A source cannot be safely accepted with the available authority."""


async def async_accept_hosted_text(
    *, store, credentials, claim_checkpoint, service_binding, local_config_entry_id,
    delivery, route_source, parse_source, attachment_transport=None,
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
        if not source["attachments"] and not (source.get("text") or "").strip():
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
    descriptors = tuple(source["attachments"])
    evidence = HostedDeliveryEvidence(
        local_config_entry_id=local_config_entry_id,
        delivery_id=delivery["delivery_id"], lease_token=delivery["lease_token"],
        source_expires_at=delivery["source_expires_at"].removesuffix("z")
        + ("Z" if delivery["source_expires_at"].endswith("z") else ""),
        source_evidence_sha256=sha256(canonical).hexdigest(),
        attachment_manifest_sha256=manifest_digest(descriptors),
        attachment_descriptors=descriptors,
        account_id=credential.account_id, installation_id=credential.installation_id,
        public_source_id=delivery["source_id"],
    )
    if any(item.evidence.source_id == evidence.source_id for item in store.hosted_ack_journal()):
        raise HostedTextAcceptanceError("hosted delivery already accepted")
    attachments, verified = await async_verify_attachments(
        delivery, descriptors, attachment_transport, credential.token,
        service_binding, guard, HostedTextAcceptanceError,
    )
    evidence = replace(evidence, verified_attachments=verified)
    evidence.validate()
    document = SourceDocument(
        id=str(uuid4()), kind=SourceKind.EMAIL,
        received_at=_date(source["received_at"]),
        text=source.get("text"), title=source.get("title"), metadata=deepcopy(source["metadata"]),
        attachments=attachments,
        upstream_source_id=evidence.source_id,
    )
    route = route_source(document)
    outcome, parser_guard = await parse_source(route.parser_source)

    def acceptance_guard() -> None:
        guard()
        if parser_guard is not None:
            parser_guard()

    source_text = "\n\n".join(part for part in (
        document.text, *(f"Attachment (SHA-256: {item['sha256']})" for item in verified),
    ) if part)
    return await store.async_add(
        source_text=source_text, events=outcome.events,
        event_assumptions=outcome.event_assumptions or None,
        source_id=evidence.source_id, calendar_entity=route.calendar_entity,
        routing_unresolved=route.requires_confirmation, source_kind=document.kind.value,
        source_title=document.title, source_sender=document.metadata.get("sender"),
        warnings=[*outcome.warnings, *route.warnings], acceptance_guard=acceptance_guard,
        hosted_evidence=evidence,
        **({"hosted_operation_id": parser_guard.hosted_operation_id}
           if getattr(parser_guard, "hosted_operation_id", None) else {}),
    )
