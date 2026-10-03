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
        (
            b"From: trusted@example.test,\r\n"
            b"Subject: Trailing comma\r\n\r\nBody"
        ),
        (
            b"From : attacker@example.test\r\n"
            b"From: trusted@example.test\r\n"
            b"Subject: Invalid From field name\r\n\r\nBody"
        ),
        (
            b"Subject:x\r"
            b"From: trusted@example.test\r"
            b"From : attacker@example.test\r\r"
            b"Body"
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


def test_exact_sender_allowlist_rejects_defective_from_header(
    monkeypatch,
) -> None:
    class FakeHeader:
        defects = (ValueError("defective header"),)
        addresses = ()

    class FakeMessage:
        def get_all(self, _name, _default):
            return [FakeHeader()]

    class FakeParser:
        def parsebytes(self, _raw_message):
            return FakeMessage()

    monkeypatch.setattr(
        email_safety,
        "BytesHeaderParser",
        lambda **_kwargs: FakeParser(),
    )
    allowlist = email_safety.ExactSenderAllowlist(
        ("trusted@example.test",)
    )

    assert allowlist.allows(b"X: y\r\n\r\n") is False


def test_exact_sender_allowlist_rejects_parser_failure(monkeypatch) -> None:
    class BrokenParser:
        def parsebytes(self, _raw_message):
            raise ValueError("broken")

    monkeypatch.setattr(
        email_safety,
        "BytesHeaderParser",
        lambda **_kwargs: BrokenParser(),
    )
    allowlist = email_safety.ExactSenderAllowlist(
        ("trusted@example.test",)
    )

    assert allowlist.allows(b"X: y\r\n\r\n") is False


def test_exact_sender_allowlist_rejects_empty_addr_spec(monkeypatch) -> None:
    class FakeAddress:
        addr_spec = " "

    class FakeGroup:
        display_name = None
        addresses = (FakeAddress(),)

    class FakeHeader:
        defects = ()
        groups = (FakeGroup(),)

    class FakeMessage:
        defects = ()

        def get_all(self, _name, _default):
            return [FakeHeader()]

        def raw_items(self):
            return (("From", "trusted@example.test"),)

    class FakeParser:
        def parsebytes(self, _raw_message):
            return FakeMessage()

    monkeypatch.setattr(
        email_safety,
        "BytesHeaderParser",
        lambda **_kwargs: FakeParser(),
    )
    allowlist = email_safety.ExactSenderAllowlist(
        ("trusted@example.test",)
    )

    assert allowlist.allows(b"X: y\r\n\r\n") is False


def test_normalize_sender_allowlist_rejects_missing_username_or_domain(
    monkeypatch,
) -> None:
    class MissingDomainAddress:
        username = "trusted"
        domain = ""

        def __init__(self, *, addr_spec):
            del addr_spec

    monkeypatch.setattr(email_safety, "Address", MissingDomainAddress)

    with pytest.raises(
        ValueError,
        match="sender_allowlist entries must be valid email addresses",
    ):
        email_safety.normalize_sender_allowlist(("trusted@example.test",))


def test_exact_sender_allowlist_rejects_lazy_header_parse_failure() -> None:
    allowlist = email_safety.ExactSenderAllowlist(
        ("trusted@example.test",)
    )
    raw = (
        b"From: :;Z\r\n"
        b"Subject: Malformed From\r\n\r\nBody"
    )

    assert allowlist.allows(raw) is False


@pytest.mark.parametrize(
    "raw_message",
    (
        (
            b"From: Friends: trusted@example.test;\r\n"
            b"Subject: Named group\r\n\r\nBody"
        ),
        (
            b"From: trusted@example.test, Undisclosed:;\r\n"
            b"Subject: Extra empty group\r\n\r\nBody"
        ),
    ),
)
def test_exact_sender_allowlist_rejects_group_syntax(
    raw_message: bytes,
) -> None:
    allowlist = email_safety.ExactSenderAllowlist(
        ("trusted@example.test",)
    )

    assert allowlist.allows(raw_message) is False


def test_exact_sender_allowlist_rejects_empty_mailbox_group(
    monkeypatch,
) -> None:
    class FakeGroup:
        display_name = None
        addresses = ()

    class FakeHeader:
        defects = ()
        groups = (FakeGroup(),)

    class FakeMessage:
        def get_all(self, _name, _default):
            return [FakeHeader()]

    class FakeParser:
        def parsebytes(self, _raw_message):
            return FakeMessage()

    monkeypatch.setattr(
        email_safety,
        "BytesHeaderParser",
        lambda **_kwargs: FakeParser(),
    )
    allowlist = email_safety.ExactSenderAllowlist(
        ("trusted@example.test",)
    )

    assert allowlist.allows(b"X: y\r\n\r\n") is False



def test_exact_sender_allowlist_rejects_message_level_header_defect() -> None:
    allowlist = email_safety.ExactSenderAllowlist(
        ("trusted@example.test",)
    )
    raw = (
        b"\tFrom: attacker@example.test\r\n"
        b"From: trusted@example.test\r\n"
        b"Subject: Ambiguous malformed headers\r\n\r\nBody"
    )

    assert allowlist.allows(raw) is False


def test_exact_sender_allowlist_rejects_disagreement_between_parsers(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        email_safety,
        "getaddresses",
        lambda _values, *, strict: [("", "other@example.test")],
    )
    allowlist = email_safety.ExactSenderAllowlist(
        ("trusted@example.test",)
    )

    assert allowlist.allows(_raw_from("trusted@example.test")) is False


def test_exact_sender_allowlist_rejects_empty_strict_address(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        email_safety,
        "getaddresses",
        lambda _values, *, strict: [("", "")],
    )
    allowlist = email_safety.ExactSenderAllowlist(
        ("trusted@example.test",)
    )

    assert allowlist.allows(_raw_from("trusted@example.test")) is False



def test_raw_header_lines_normalize_supported_line_endings() -> None:
    assert email_safety._raw_header_lines(
        b"One: 1\r\nTwo: 2\rThree: 3"
    ) == (b"One: 1", b"Two: 2", b"Three: 3")



@pytest.mark.parametrize("newline", (b"\r\n", b"\n", b"\r"))
def test_exact_sender_allowlist_rejects_control_whitespace_in_field_name(
    newline: bytes,
) -> None:
    allowlist = email_safety.ExactSenderAllowlist(
        ("trusted@example.test",)
    )
    raw = (
        b"Subject: Event"
        + newline
        + b"From: trusted@example.test"
        + newline
        + b"From \v: attacker@example.test"
        + newline
        + newline
        + b"Body"
    )

    assert allowlist.allows(raw) is False


def test_raw_header_block_excludes_message_body() -> None:
    body = b"body-marker-" + (b"x" * 100_000)
    raw = (
        b"From: trusted@example.test\r\n"
        b"Subject: Event\r\n\r\n"
        + body
    )

    header_block = email_safety._raw_header_block(raw)

    assert header_block == (
        b"From: trusted@example.test\r\n"
        b"Subject: Event"
    )
    assert body not in header_block


def test_raw_header_block_requires_header_body_separator() -> None:
    assert email_safety._raw_header_block(
        b"From: trusted@example.test"
    ) is None


@pytest.mark.parametrize(
    "header_block",
    (
        b"From \v: attacker@example.test\r\nFrom: trusted@example.test",
        b"NoColonHere\r\nFrom: trusted@example.test",
        b": empty-name\r\nFrom: trusted@example.test",
    ),
)
def test_raw_header_validation_rejects_invalid_field_names(
    header_block: bytes,
) -> None:
    assert email_safety._has_invalid_header_field_name(header_block) is True


def test_raw_header_validation_allows_folding() -> None:
    header_block = (
        b"From: Trusted Person\r\n"
        b" <trusted@example.test>\r\n"
        b"Subject: Event"
    )

    assert email_safety._has_invalid_header_field_name(header_block) is False


def test_exact_sender_allowlist_parses_headers_without_body(
    monkeypatch,
) -> None:
    seen: list[bytes] = []
    real_parser = email_safety.BytesHeaderParser

    class RecordingParser:
        def __init__(self, **kwargs):
            self._parser = real_parser(**kwargs)

        def parsebytes(self, raw_headers):
            seen.append(raw_headers)
            return self._parser.parsebytes(raw_headers)

    monkeypatch.setattr(email_safety, "BytesHeaderParser", RecordingParser)
    allowlist = email_safety.ExactSenderAllowlist(
        ("trusted@example.test",)
    )
    raw = (
        b"From: trusted@example.test\r\n"
        b"Subject: Event\r\n\r\n"
        b"body-marker"
    )

    assert allowlist.allows(raw) is True
    assert seen
    assert b"body-marker" not in seen[0]



def test_exact_sender_allowlist_rejects_header_parser_message_defect(
    monkeypatch,
) -> None:
    class FakeMessage:
        defects = (ValueError("defective message headers"),)

    class FakeParser:
        def parsebytes(self, _raw_headers):
            return FakeMessage()

    monkeypatch.setattr(
        email_safety,
        "BytesHeaderParser",
        lambda **_kwargs: FakeParser(),
    )
    allowlist = email_safety.ExactSenderAllowlist(
        ("trusted@example.test",)
    )

    assert allowlist.allows(b"X: y\r\n\r\n") is False



@pytest.mark.parametrize(
    ("separator", "body"),
    (
        (b"\r\n\r\n", b"crlf-body"),
        (b"\n\n", b"lf-body"),
        (b"\r\r", b"cr-body"),
    ),
)
def test_raw_header_block_finds_supported_boundaries(
    separator: bytes,
    body: bytes,
) -> None:
    header = b"From: trusted@example.test"
    assert email_safety._raw_header_block(header + separator + body) == header


def test_raw_header_block_allows_exact_header_limit() -> None:
    header = b"X:" + (b"a" * (email_safety.MAX_EMAIL_HEADER_BYTES - 2))
    raw = header + b"\r\n\r\nBody"

    assert email_safety._raw_header_block(raw) == header


def test_raw_header_block_rejects_header_over_limit() -> None:
    header = b"X:" + (b"a" * (email_safety.MAX_EMAIL_HEADER_BYTES - 1))
    raw = header + b"\r\n\r\nBody"

    assert email_safety._raw_header_block(raw) is None


def test_large_body_does_not_affect_bounded_header_extraction() -> None:
    header = b"From: trusted@example.test"
    raw = header + b"\r\n\r\n" + (b"x" * 1_000_000)

    assert email_safety._raw_header_block(raw) == header
