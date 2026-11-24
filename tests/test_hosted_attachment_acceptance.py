"""Mutation-sensitive checks for hosted attachment evidence and lease authority."""

from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

from custom_components.daylight_calendar_import.hosted_attachment_acceptance import (
    async_verify_attachments,
    manifest_digest,
)
from custom_components.daylight_calendar_import.sources import SourceAttachment


def test_manifest_digest_preserves_unicode_and_order_without_nan():
    descriptors = (
        {"id": "first", "filename": "café.png"},
        {"id": "second", "filename": "poster.png"},
    )
    raw = '{"attachments":[{"filename":"café.png","id":"first"},{"filename":"poster.png","id":"second"}]}'
    assert manifest_digest(descriptors) == sha256(raw.encode("utf-8")).hexdigest()
    assert manifest_digest(tuple(reversed(descriptors))) != manifest_digest(descriptors)
    with pytest.raises(ValueError, match="Out of range float values"):
        manifest_digest(({"size": float("nan")},))


async def test_attachment_fetch_keeps_exact_verified_evidence_and_authority():
    binding = object()
    first = {
        "id": "image-1", "media_type": "image/png", "size_bytes": 3,
        "filename": "café.png", "sha256": "a" * 64,
    }
    second = {
        "id": "document-2", "media_type": "text/plain", "size_bytes": 4,
        "filename": "details.txt", "sha256": "b" * 64,
    }
    fetch = AsyncMock(side_effect=(b"png", b"text"))
    transport = SimpleNamespace(service_binding=binding, async_fetch_attachment=fetch)
    guard = Mock()
    delivery = {"delivery_id": "delivery-123", "lease_token": "lease-456"}
    attachments, verified = await async_verify_attachments(
        delivery, (first, second), transport, "secret", binding, guard, ValueError,
    )
    assert attachments == (
        SourceAttachment(
            id="image-1", media_type="image/png", size_bytes=3,
            content_ref="", filename="café.png", sha256="a" * 64, content=b"png",
        ),
        SourceAttachment(
            id="document-2", media_type="text/plain", size_bytes=4,
            content_ref="", filename="details.txt", sha256="b" * 64, content=b"text",
        ),
    )
    assert verified == (
        {"id": "image-1", "size_bytes": 3, "sha256": "a" * 64},
        {"id": "document-2", "size_bytes": 4, "sha256": "b" * 64},
    )
    assert fetch.await_args_list == [
        call(token="secret", delivery_id="delivery-123", lease_token="lease-456", descriptor=first),
        call(token="secret", delivery_id="delivery-123", lease_token="lease-456", descriptor=second),
    ]
    assert guard.call_count == 4


@pytest.mark.parametrize("transport", [None, "untrusted"])
async def test_attachment_fetch_rejects_missing_or_wrong_service(transport):
    binding = object()
    transport_obj = (
        None if transport is None else
        SimpleNamespace(service_binding=object(), async_fetch_attachment=AsyncMock())
    )
    guard = Mock()
    with pytest.raises(ValueError, match="^hosted attachment authority unavailable$"):
        await async_verify_attachments(
            {"delivery_id": "delivery", "lease_token": "lease"},
            ({"id": "attachment"},), transport_obj, "secret", binding, guard, ValueError,
        )
    guard.assert_not_called()
    if transport_obj is not None:
        transport_obj.async_fetch_attachment.assert_not_awaited()


async def test_empty_attachment_manifest_requires_no_transport():
    guard = Mock()
    assert await async_verify_attachments({}, (), None, None, None, guard, ValueError) == ((), ())
    guard.assert_not_called()


async def test_attachment_authority_is_rechecked_after_fetch():
    binding = object()
    guard = Mock(side_effect=(None, ValueError("lease revoked")))
    fetch = AsyncMock(return_value=b"bytes")
    transport = SimpleNamespace(service_binding=binding, async_fetch_attachment=fetch)
    with pytest.raises(ValueError, match="lease revoked"):
        await async_verify_attachments(
            {"delivery_id": "delivery", "lease_token": "lease"},
            ({"id": "document", "media_type": "text/plain", "size_bytes": 5,
              "sha256": "f" * 64},),
            transport, "token", binding, guard, ValueError,
        )
    fetch.assert_awaited_once()
    assert guard.call_count == 2
