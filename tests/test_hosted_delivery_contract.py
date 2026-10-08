"""Executable, cloud-independent H3 hosted-source protocol contract.

These tests validate public schemas/fixtures and normative lease/ACK traces.
They do not pretend to exercise the private cloud server or a live HA adapter.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "schemas").is_dir():
    ROOT = ROOT.parent
SCHEMA_DIR = ROOT / "schemas" / "hosted" / "v1"
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "hosted" / "v1"


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def schema_registry() -> tuple[dict[str, dict], Registry]:
    schemas = {path.name: _read(path) for path in SCHEMA_DIR.glob("*.schema.json")}
    for schema in schemas.values():
        Draft202012Validator.check_schema(schema)
    registry = Registry().with_resources(
        [(schema["$id"], Resource.from_contents(schema)) for schema in schemas.values()]
    )
    return schemas, registry


def _validate(name: str, data: dict, schemas: dict, registry: Registry) -> None:
    Draft202012Validator(
        schemas[name], registry=registry, format_checker=FormatChecker()
    ).validate(data)


@pytest.mark.parametrize(("schema", "name"), [
    ("delivery-page.schema.json", "delivery-page-text.json"),
    ("delivery-page.schema.json", "delivery-page-attachment.json"),
    ("delivery-ack-request.schema.json", "delivery-ack-request.json"),
    ("delivery-ack-response.schema.json", "delivery-ack-response.json"),
])
def test_h3_valid_json_fixtures(schema, name, schema_registry):
    schemas, registry = schema_registry
    _validate(schema, _read(FIXTURE_DIR / "valid" / name), schemas, registry)


@pytest.mark.parametrize(("schema", "name"), [
    ("delivery-page.schema.json", "delivery-page-missing-lease-token.json"),
    ("delivery-page.schema.json", "delivery-page-extra-metadata.json"),
    ("delivery-page.schema.json", "delivery-page-attachment-missing-sha256.json"),
    ("delivery-ack-request.schema.json", "delivery-ack-request-missing-token.json"),
    ("delivery-ack-request.schema.json", "delivery-ack-request-short-token.json"),
    ("delivery-ack-response.schema.json", "delivery-ack-response-invalid-status.json"),
])
def test_h3_invalid_json_fixtures(schema, name, schema_registry):
    schemas, registry = schema_registry
    with pytest.raises(ValidationError):
        _validate(schema, _read(FIXTURE_DIR / "invalid" / name), schemas, registry)


def test_h3_schema_enforces_page_bounds_and_wire_field_allowlist(schema_registry):
    schemas, registry = schema_registry
    page = _read(FIXTURE_DIR / "valid" / "delivery-page-text.json")
    import copy

    oversized = copy.deepcopy(page)
    oversized["deliveries"] *= 51
    with pytest.raises(ValidationError):
        _validate("delivery-page.schema.json", oversized, schemas, registry)

    for field, value in [
        ("installation_id", "tenant-2"), ("cloud_db_id", 1), ("calendar_entity", "calendar.home")
    ]:
        exposed = copy.deepcopy(page)
        exposed["deliveries"][0][field] = value
        with pytest.raises(ValidationError):
            _validate("delivery-page.schema.json", exposed, schemas, registry)

    for field, value in [
        ("upstream_source_id", "private-mail-id"),
        ("raw_email_headers", "Private"),
        ("attachment_url", "https://malicious.example/steal"),
    ]:
        exposed = copy.deepcopy(page)
        exposed["deliveries"][0]["source"][field] = value
        with pytest.raises(ValidationError):
            _validate("delivery-page.schema.json", exposed, schemas, registry)

    attachment_page = _read(FIXTURE_DIR / "valid" / "delivery-page-attachment.json")
    leaked = copy.deepcopy(attachment_page)
    leaked["deliveries"][0]["source"]["attachments"][0]["fetch_url"] = "https://example.org"
    with pytest.raises(ValidationError):
        _validate("delivery-page.schema.json", leaked, schemas, registry)


def test_h3_semantic_fixture_checks_immutability_integrity_and_retention():
    text_page = _read(FIXTURE_DIR / "valid" / "delivery-page-text.json")
    att_page = _read(FIXTURE_DIR / "valid" / "delivery-page-attachment.json")
    all_items = text_page["deliveries"] + att_page["deliveries"]
    assert len({item["delivery_id"] for item in all_items}) == len(all_items)
    assert len({item["source_id"] for item in all_items}) == len(all_items)
    assert all(item["lease_token"] for item in all_items)
    for item in all_items:
        claimed = datetime.fromisoformat(item["lease_expires_at"])
        expires = datetime.fromisoformat(item["source_expires_at"])
        received = datetime.fromisoformat(item["source"]["received_at"])
        assert received < claimed < expires
        attachments = item["source"]["attachments"]
        assert len({att["id"] for att in attachments}) == len(attachments)
        assert sum(att["size_bytes"] for att in attachments) <= 10 * 1024 * 1024
        assert item["source"].get("text") or attachments
        assert all(len(att["sha256"]) == 64 for att in attachments)


def test_stale_ack_trace_rejects_old_lease_and_repeated_ack_is_idempotent():
    """Reference conformance trace for cloud implementation contract tests."""
    trace = _read(FIXTURE_DIR / "valid" / "delivery-stale-ack-trace.json")
    assert trace["delivery_id"] and trace["source_id"]
    current_token = None
    acknowledged_token = None
    seen = set()
    for step in trace["steps"]:
        token = step["token"]
        if step["op"] == "claim":
            assert token not in seen and acknowledged_token is None
            current_token = token
            seen.add(token)
        elif step["op"] == "expire":
            assert token == current_token
            current_token = None
        else:
            assert step["op"] == "ack"
            valid = (token == current_token or token == acknowledged_token)
            if valid and acknowledged_token is None:
                acknowledged_token = token
                current_token = None
            expected = 200 if valid else 409
            assert step["status"] == expected
            if valid:
                assert step["result"] == "acknowledged"
            else:
                assert step["code"] == "lease_not_current"
    assert acknowledged_token is not None
    assert current_token is None


def test_ack_must_follow_durable_checkpoint_even_after_client_restart():
    """Sequence is a public HA adapter conformance requirement, not a cloud mock."""
    trace = _read(FIXTURE_DIR / "valid" / "delivery-durable-client-trace.json")
    steps = trace["steps"]
    operations = [step["op"] for step in steps]
    assert operations == [
        "claim", "download_and_verify", "source_normalized",
        "persist_local_checkpoint", "restart_ha",
        "recover_checkpoint", "ack",
    ]
    assert steps[3]["checkpoint_id"] == steps[5]["checkpoint_id"]
    assert steps[0]["token"] == steps[-1]["token"]
    assert trace["source_id"] != trace["delivery_id"]
