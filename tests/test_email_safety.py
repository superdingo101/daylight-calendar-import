"""Tests for exact email sender allowlisting."""

from __future__ import annotations

from email.message import EmailMessage

import pytest

from custom_components.daylight_calendar_import import email_safety


def _raw_from(value: str) -> bytes:
    message = EmailMessage()
    message["From"] = value
    message["Subject"] = "Event"
    message.set_content("Friday at 5")
    return message.as_bytes()


def test_normalize_sender_allowlist_casefolds_and_deduplicates() -> None:
    assert email_safety.normalize_sender_allowlist(
        (
            "Trusted@Example.Test",
            "other@example.test",
            "TRUSTED@example.test",
        )
    ) == (
        "trusted@example.test",
        "other@example.test",
    )


@pytest.mark.parametrize(
    "values",
    (
        "trusted@example.test",
        b"trusted@example.test",
        ("",),
        ("   ",),
        ("not-an-address",),
        ("trusted@",),
        (42,),
    ),
)
def test_normalize_sender_allowlist_rejects_invalid_values(values: object) -> None:
    with pytest.raises(
        ValueError,
        match="sender_allowlist entries must be valid email addresses",
    ):
        email_safety.normalize_sender_allowlist(values)


def test_exact_sender_allowlist_requires_at_least_one_sender() -> None:
    with pytest.raises(
        ValueError,
        match="sender allowlist must contain at least one address",
    ):
        email_safety.ExactSenderAllowlist(())


def test_exact_sender_allowlist_accepts_display_name_and_case_insensitively() -> None:
    allowlist = email_safety.ExactSenderAllowlist(
        ("trusted@example.test",)
    )

    assert allowlist.allows(
        _raw_from("Trusted Person <TRUSTED@EXAMPLE.TEST>")
    )


@pytest.mark.parametrize(
    "raw_message",
    (
        b"Subject: Missing From\r\n\r\nBody",
        (
            b"From: trusted@example.test\r\n"
            b"From: other@example.test\r\n"
            b"Subject: Duplicate From\r\n\r\nBody"
        ),
        (
            b"From: trusted@example.test, other@example.test\r\n"
            b"Subject: Multiple addresses\r\n\r\nBody"
        ),
        (
            b"From: blocked@example.test\r\n"
            b"Subject: Blocked\r\n\r\nBody"
        ),
    ),
)
def test_exact_sender_allowlist_rejects_nonmatching_or_ambiguous_from(
    raw_message: bytes,
) -> None:
    allowlist = email_safety.ExactSenderAllowlist(
        ("trusted@example.test",)
    )

    assert allowlist.allows(raw_message) is False


def test_exact_sender_allowlist_rejects_defective_from_header() -> None:
    allowlist = email_safety.ExactSenderAllowlist(
        ("trusted@example.test",)
    )
    raw = (
        b"From: =?utf-8?Q?Broken <trusted@example.test>\r\n"
        b"Subject: Event\r\n\r\nBody"
    )

    assert allowlist.allows(raw) is False


def test_exact_sender_allowlist_rejects_parser_failure(monkeypatch) -> None:
    class BrokenParser:
        def parsebytes(self, _raw_message):
            raise ValueError("broken")

    monkeypatch.setattr(
        email_safety,
        "BytesParser",
        lambda **_kwargs: BrokenParser(),
    )
    allowlist = email_safety.ExactSenderAllowlist(
        ("trusted@example.test",)
    )

    assert allowlist.allows(b"mail") is False


def test_exact_sender_allowlist_rejects_empty_addr_spec(monkeypatch) -> None:
    class FakeAddress:
        addr_spec = " "

    class FakeHeader:
        defects = ()
        addresses = (FakeAddress(),)

    class FakeMessage:
        def get_all(self, _name, _default):
            return [FakeHeader()]

    class FakeParser:
        def parsebytes(self, _raw_message):
            return FakeMessage()

    monkeypatch.setattr(
        email_safety,
        "BytesParser",
        lambda **_kwargs: FakeParser(),
    )
    allowlist = email_safety.ExactSenderAllowlist(
        ("trusted@example.test",)
    )

    assert allowlist.allows(b"mail") is False
