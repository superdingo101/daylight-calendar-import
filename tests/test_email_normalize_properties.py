"""Property tests for semantic email identity."""

from email import policy
from email.message import EmailMessage

from hypothesis import given, settings, strategies as st

from custom_components.daylight_calendar_import.email_normalize import (
    FALLBACK_IDENTITY_PREFIX,
    stable_email_identity,
)


PROPERTY_SETTINGS = settings(max_examples=100, deadline=None, derandomize=True)
HEADER_TEXT = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cc", "Cs"),
        blacklist_characters="\r\n",
    ),
    min_size=1,
    max_size=80,
).filter(lambda value: bool(value.strip()))
BODY_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    max_size=300,
)


@PROPERTY_SETTINGS
@given(first_body=BODY_TEXT, second_body=BODY_TEXT)
def test_message_id_is_authoritative_over_body_changes(
    first_body: str,
    second_body: str,
) -> None:
    first = EmailMessage()
    first["Message-ID"] = "<stable@example.test>"
    first.set_content(first_body)
    second = EmailMessage()
    second["Message-ID"] = "<stable@example.test>"
    second.set_content(second_body)

    assert stable_email_identity(first.as_bytes(policy=policy.default)) == (
        stable_email_identity(second.as_bytes(policy=policy.default))
    )


@PROPERTY_SETTINGS
@given(subject=HEADER_TEXT, body=BODY_TEXT)
def test_fallback_identity_is_deterministic(subject: str, body: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message.set_content(body)
    raw = message.as_bytes(policy=policy.default)

    first = stable_email_identity(raw)
    second = stable_email_identity(raw)

    assert first == second
    assert first.startswith(FALLBACK_IDENTITY_PREFIX)
