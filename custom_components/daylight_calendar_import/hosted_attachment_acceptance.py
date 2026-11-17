"""Prepare bounded, lease-authorized attachment evidence for local acceptance."""
from __future__ import annotations

import json
from hashlib import sha256

from .sources import SourceAttachment


def manifest_digest(descriptors):
    """Hash the canonical ordered manifest used by the ACK journal."""
    canonical = json.dumps(
        {"attachments": list(descriptors)}, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


async def async_verify_attachments(delivery, descriptors, transport, token, binding, guard, error):
    """Fetch each attachment under the current claim and retain bytes only in memory."""
    if not descriptors:
        return (), ()
    if transport is None or transport.service_binding != binding:
        raise error("hosted attachment authority unavailable")
    attachments, verified = [], []
    for item in descriptors:
        guard()
        content = await transport.async_fetch_attachment(
            token=token, delivery_id=delivery["delivery_id"],
            lease_token=delivery["lease_token"], descriptor=item,
        )
        guard()
        attachments.append(SourceAttachment(
            id=item["id"], media_type=item["media_type"], size_bytes=item["size_bytes"],
            content_ref="", filename=item.get("filename"), sha256=item["sha256"], content=content,
        ))
        verified.append({key: item[key] for key in ("id", "size_bytes", "sha256")})
    return tuple(attachments), tuple(verified)
