"""Executable contract tests for the public Daylight Hosted API v1 schemas."""

from __future__ import annotations

import json
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "schemas").is_dir():
    ROOT = ROOT.parent

SCHEMA_DIR = ROOT / "schemas" / "hosted" / "v1"
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "hosted" / "v1"

SCHEMA_FILES = (
    "source-attachment.schema.json",
    "source-document.schema.json",
    "event-draft.schema.json",
    "draft-warning.schema.json",
    "rejected-candidate.schema.json",
    "parsed-event.schema.json",
    "parse-request.schema.json",
    "parse-response.schema.json",
    "error.schema.json",
    "delivery-attachment.schema.json",
    "delivery-source.schema.json",
    "delivery-item.schema.json",
    "delivery-page.schema.json",
    "delivery-claim-request.schema.json",
    "delivery-ack-request.schema.json",
    "delivery-ack-response.schema.json",
)

VALID_FIXTURES = (
    ("parse-request.schema.json", "parse-request-text.json"),
    ("parse-request.schema.json", "parse-request-image.json"),
    ("parse-request.schema.json", "parse-request-pdf.json"),
    ("parse-response.schema.json", "parse-response-remote-meeting.json"),
    ("parse-response.schema.json", "parse-response-long-description.json"),
    ("parse-response.schema.json", "parse-response-partial.json"),
    ("error.schema.json", "error-provider-rate-limited.json"),
    ("delivery-claim-request.schema.json", "delivery-claim-request.json"),
    ("delivery-page.schema.json", "delivery-page-text.json"),
    ("delivery-page.schema.json", "delivery-page-attachment.json"),
    ("delivery-ack-request.schema.json", "delivery-ack-request.json"),
    ("delivery-ack-response.schema.json", "delivery-ack-response.json"),
)

INVALID_FIXTURES = (
    ("parse-request.schema.json", "parse-request-missing-time-zone.json"),
    ("parse-request.schema.json", "parse-request-unsupported-media.json"),
    ("parse-response.schema.json", "parse-response-invalid-confidence.json"),
    ("error.schema.json", "error-missing-retryable.json"),
    ("delivery-claim-request.schema.json", "delivery-claim-request-missing-id.json"),
    ("delivery-claim-request.schema.json", "delivery-claim-request-missing-expiry.json"),
    ("delivery-page.schema.json", "delivery-page-missing-lease-token.json"),
    ("delivery-page.schema.json", "delivery-page-extra-metadata.json"),
    ("delivery-page.schema.json", "delivery-page-attachment-missing-sha256.json"),
    ("delivery-ack-request.schema.json", "delivery-ack-request-missing-token.json"),
    ("delivery-ack-request.schema.json", "delivery-ack-request-short-token.json"),
    ("delivery-ack-response.schema.json", "delivery-ack-response-invalid-status.json"),
)


def _load(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def schemas() -> dict[str, dict]:
    result = {name: _load(SCHEMA_DIR / name) for name in SCHEMA_FILES}
    for value in result.values():
        Draft202012Validator.check_schema(value)
    return result


@pytest.fixture(scope="module")
def registry(schemas: dict[str, dict]) -> Registry:
    resources = [(value["$id"], Resource.from_contents(value)) for value in schemas.values()]
    return Registry().with_resources(resources)


def _validator(name: str, schemas: dict[str, dict], registry: Registry) -> Draft202012Validator:
    return Draft202012Validator(schemas[name], registry=registry, format_checker=FormatChecker())


def _validate_parse_request(
    instance: dict, schemas: dict[str, dict], registry: Registry
) -> None:
    _validator("parse-request.schema.json", schemas, registry).validate(instance)

    try:
        ZoneInfo(instance["time_zone"])
    except ZoneInfoNotFoundError as err:
        raise ValueError("time_zone must be an IANA time-zone identifier") from err

    attachment_ids = [attachment["id"] for attachment in instance["source"]["attachments"]]
    if len(attachment_ids) != len(set(attachment_ids)):
        raise ValueError("attachment IDs must be unique within a source")


@pytest.mark.parametrize(("schema_name", "fixture_name"), VALID_FIXTURES)
def test_valid_hosted_v1_fixtures(
    schema_name: str, fixture_name: str, schemas: dict[str, dict], registry: Registry
) -> None:
    instance = _load(FIXTURE_DIR / "valid" / fixture_name)
    if schema_name == "parse-request.schema.json":
        _validate_parse_request(instance, schemas, registry)
    else:
        _validator(schema_name, schemas, registry).validate(instance)


@pytest.mark.parametrize(("schema_name", "fixture_name"), INVALID_FIXTURES)
def test_invalid_hosted_v1_fixtures(
    schema_name: str, fixture_name: str, schemas: dict[str, dict], registry: Registry
) -> None:
    with pytest.raises(ValidationError):
        _validator(schema_name, schemas, registry).validate(
            _load(FIXTURE_DIR / "invalid" / fixture_name)
        )


def test_remote_meeting_fixture_preserves_join_information(
    schemas: dict[str, dict], registry: Registry
) -> None:
    instance = _load(FIXTURE_DIR / "valid" / "parse-response-remote-meeting.json")
    _validator("parse-response.schema.json", schemas, registry).validate(instance)
    description = instance["events"][0]["draft"]["description"]
    assert "zoom.us" in description
    assert "Meeting ID:" in description
    assert "Passcode:" in description


def test_long_description_fixture_is_meaningfully_long(
    schemas: dict[str, dict], registry: Registry
) -> None:
    instance = _load(FIXTURE_DIR / "valid" / "parse-response-long-description.json")
    _validator("parse-response.schema.json", schemas, registry).validate(instance)
    assert len(instance["events"][0]["draft"]["description"]) > 1000


@pytest.mark.parametrize("time_zone", ("CET", "GMT", "EST5EDT"))
def test_parse_request_accepts_valid_slashless_iana_time_zones(
    time_zone: str, schemas: dict[str, dict], registry: Registry
) -> None:
    instance = _load(FIXTURE_DIR / "valid" / "parse-request-text.json")
    instance["time_zone"] = time_zone
    _validate_parse_request(instance, schemas, registry)


def test_parse_request_rejects_duplicate_attachment_ids(
    schemas: dict[str, dict], registry: Registry
) -> None:
    instance = _load(FIXTURE_DIR / "valid" / "parse-request-image.json")
    duplicate = dict(instance["source"]["attachments"][0])
    duplicate["filename"] = "different-name.png"
    instance["source"]["attachments"].append(duplicate)

    with pytest.raises(ValueError, match="attachment IDs must be unique"):
        _validate_parse_request(instance, schemas, registry)
