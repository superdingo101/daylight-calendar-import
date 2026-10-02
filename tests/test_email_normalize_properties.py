from __future__ import annotations

from hashlib import sha256

from hypothesis import given, strategies as st

from custom_components.daylight_calendar_import.email_normalize import (
    FALLBACK_IDENTITY_PREFIX,
    stable_email_identity,
)


@given(st.binary(max_size=4096))
def test_raw_fallback_identity_is_deterministic(raw_body: bytes) -> None:
    raw = b"Subject: Property\r\n\r\n" + raw_body

    expected = FALLBACK_IDENTITY_PREFIX + sha256(raw).hexdigest()
    assert stable_email_identity(raw) == expected
    assert stable_email_identity(raw) == stable_email_identity(raw)


@given(
    st.binary(max_size=1024),
    st.binary(max_size=1024),
)
def test_raw_fallback_tracks_exact_wire_bytes(
    first_body: bytes,
    second_body: bytes,
) -> None:
    first = b"Subject: Property\r\n\r\n" + first_body
    second = b"Subject: Property\r\nX-Wire: changed\r\n\r\n" + second_body

    assert stable_email_identity(first) == (
        FALLBACK_IDENTITY_PREFIX + sha256(first).hexdigest()
    )
    assert stable_email_identity(second) == (
        FALLBACK_IDENTITY_PREFIX + sha256(second).hexdigest()
    )


@given(st.binary(max_size=2048))
def test_valid_message_id_is_authoritative_over_wire_body(raw_body: bytes) -> None:
    raw = (
        b"Message-ID: <property@example.test>\r\n"
        b"Subject: Property\r\n\r\n"
        + raw_body
    )

    assert stable_email_identity(raw) == "<property@example.test>"
