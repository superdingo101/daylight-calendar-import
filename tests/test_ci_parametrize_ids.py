"""Regression tests for bounded parameter IDs in verbose GitHub Actions logs."""

from hashlib import sha256

import pytest


@pytest.mark.parametrize("payload", [b"a" * 1024, b"b" * 1024])
def test_large_binary_parameters_use_short_distinct_ids(request, payload):
    digest = sha256(payload).hexdigest()[:12]
    assert request.node.name.endswith(f"[payload-bytes-1024-{digest}]")
    assert len(request.node.name) < 120


@pytest.mark.parametrize("payload", ["x" * 1024, "y" * 1024])
def test_large_text_parameters_use_short_distinct_ids(request, payload):
    digest = sha256(payload.encode("utf-8")).hexdigest()[:12]
    assert request.node.name.endswith(f"[payload-str-1024-{digest}]")
    assert len(request.node.name) < 120


@pytest.mark.parametrize("payload", [b"small", "short"])
def test_small_parameters_keep_readable_ids(request, payload):
    expected = payload.decode() if isinstance(payload, bytes) else payload
    assert request.node.name.endswith(f"[{expected}]")
