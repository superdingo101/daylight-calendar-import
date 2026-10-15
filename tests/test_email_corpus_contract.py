"""Source-grounding invariants for synthetic email extraction examples.

These checks keep recorded fixtures honest; live extraction accuracy is measured
only by the optional reference-provider evaluator.
"""
from datetime import datetime, timedelta
from email.message import EmailMessage
import json
from pathlib import Path

from custom_components.daylight_calendar_import.email_normalize import normalize_email
from custom_components.daylight_calendar_import.email_source import (
    DirectImapReference,
    EmailEnvelope,
    EmailProvenance,
    EmailSourceType,
)


CORPUS = Path(__file__).parent / "fixtures" / "extraction"


def case(name):
    return json.loads((CORPUS / f"email-{name}.json").read_text())


def test_sports_schedule_preserves_all_24_rows_and_shared_instructions():
    fixture = case("sports-24-games")
    events = fixture["expected"]["events"]
    rows = [line.split(" | ") for line in fixture["source_text"].splitlines()
            if line[:2].isdigit() and " | " in line]
    assert len(rows) == len(events) == 24
    assert len({event["title"] for event in events}) == 24
    for (number, day, hours, title, venue), event in zip(rows, events, strict=True):
        start, end = hours.split("–")
        assert event["title"] == title
        assert event["title"].endswith(f"game {number}")
        assert event["start"] == f"{day}T{start}:00-07:00"
        assert event["end"] == f"{day}T{end}:00-07:00"
        assert event["location"] == venue
        assert event["description"] == "Wear blue; bring shin guards and water."
        assert event["description"] in fixture["source_text"]
        assert event["all_day"] is False
        assert (datetime.fromisoformat(event["end"]) -
                datetime.fromisoformat(event["start"])) == timedelta(minutes=75)


def test_invitation_preserves_contact_and_supplies_without_inventing_rsvp_event():
    fixture = case("birthday-invitation")
    (event,) = fixture["expected"]["events"]
    assert event["title"] == "Alex’s birthday"
    assert event["location"] in fixture["source_text"]
    assert event["start"] == "2026-10-17T14:00:00-07:00"
    assert event["end"] == "2026-10-17T16:00:00-07:00"
    for detail in ("Pizza", "reusable water bottle", "host@party.example"):
        assert detail in event["description"]
        assert detail in fixture["source_text"]
    assert "no RSVP deadline has been set" in fixture["source_text"]


def test_registration_deadline_and_camp_are_distinct_calendar_items():
    fixture = case("registration-and-camp")
    deadline, camp = fixture["expected"]["events"]
    assert deadline["title"] == "Robotics camp registration deadline"
    assert deadline["all_day"] is True
    assert (deadline["start"], deadline["end"]) == ("2026-10-09", "2026-10-10")
    assert deadline["description"] == "Submit the form at https://camp.example/register."
    assert deadline["description"] in fixture["source_text"]
    assert camp["title"] == "Robotics camp"
    assert camp["all_day"] is False
    assert camp["start"] == "2026-10-19T09:00:00-07:00"
    assert camp["end"] == "2026-10-19T15:00:00-07:00"
    assert camp["location"] in fixture["source_text"]
    assert camp["description"] in fixture["source_text"]


def test_dense_school_newsletter_keeps_six_events_without_undated_noise():
    fixture = case("school-newsletter")
    events = fixture["expected"]["events"]
    assert [event["title"] for event in events] == [
        "Welcome assembly", "Family science night", "Teacher workday",
        "Autumn break", "Garden club", "PTA budget meeting",
    ]
    assert [(event["start"], event["end"]) for event in events] == [
        ("2026-10-05T08:15:00-07:00", "2026-10-05T08:45:00-07:00"),
        ("2026-10-07T18:00:00-07:00", "2026-10-07T19:30:00-07:00"),
        ("2026-10-09", "2026-10-10"),
        ("2026-10-12", "2026-10-15"),  # Inclusive Oct 12–14 closure.
        ("2026-10-15T15:15:00-07:00", "2026-10-15T16:15:00-07:00"),
        ("2026-10-21T17:00:00-07:00", "2026-10-21T18:00:00-07:00"),
    ]
    assert [line.split(" — ")[0] for line in fixture["source_text"].splitlines()
            if line.startswith("October ")] == [
        "October 5, 08:15–08:45", "October 7, 18:00–19:30",
        "October 9 (all day)", "October 12–14 (all days, inclusive)",
        "October 15, 15:15–16:15", "October 21, 17:00–18:00",
    ]
    assert [event["all_day"] for event in events] == [False, False, True, True, False, False]
    for event in events:
        assert event["title"] in fixture["source_text"]
        assert event["description"] in fixture["source_text"]
        if event["location"]:
            assert event["location"] in fixture["source_text"]
    assert "Volunteer training dates will be announced later" in fixture["source_text"]


def test_email_cases_explicitly_identify_email_source_and_fixed_context():
    for name in ("sports-24-games", "birthday-invitation", "registration-and-camp", "school-newsletter"):
        fixture = case(name)
        assert fixture["source_kind"] == "email"
        message = EmailMessage()
        message["From"] = "sender@corpus.example"
        message["Subject"] = "Header-only event on December 1, 2026, 09:00–10:00 PDT"
        message.set_content(fixture["source_text"])
        source = normalize_email(EmailEnvelope(
            received_at=datetime.fromisoformat(fixture["reference_datetime"]),
            raw_message=message.as_bytes(),
            provenance=EmailProvenance(
                source_id="corpus", source_type=EmailSourceType.DIRECT_IMAP,
                transport_reference=DirectImapReference("INBOX", 1, 1),
            ),
        ))
        assert source.text == fixture["source_text"]
        assert source.title == message["Subject"]
        assert source.title not in fixture["source_text"]
        assert source.metadata["sender"] == message["From"]
        assert message["From"] not in fixture["source_text"]
        reference = datetime.fromisoformat(fixture["reference_datetime"])
        assert reference.utcoffset() == timedelta(hours=-7)
        assert fixture["time_zone"] == "America/Los_Angeles"
        assert len(fixture["expected"]["event_assumptions"]) == len(fixture["expected"]["events"])
