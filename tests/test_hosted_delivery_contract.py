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


def _validate_delivery_page_semantics(page: dict) -> None:
    """Semantic invariants the JSON Schema cannot express by property."""
    deliveries = page["deliveries"]
    delivery_ids = [item["delivery_id"] for item in deliveries]
    if len(delivery_ids) != len(set(delivery_ids)):
        raise ValueError("delivery IDs must be unique within a page")
    for item in deliveries:
        attachments = item["source"]["attachments"]
        ids = [attachment["id"] for attachment in attachments]
        if len(ids) != len(set(ids)):
            raise ValueError("attachment IDs must be unique within a source")
        if sum(attachment["size_bytes"] for attachment in attachments) > 10 * 1024 * 1024:
            raise ValueError("aggregate attachment bytes exceed 10 MiB")


def _validate_lease_trace_immutable(trace: dict) -> None:
    source = None
    for event in trace["steps"]:
        if event["op"] != "claim":
            continue
        if source is None:
            source = event["source"]
        elif source != event["source"]:
            raise ValueError("re-lease MUST preserve the original source payload")
    if source is None:
        raise ValueError("at least one source claim is required")


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
    first_acknowledged_at = None
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
                if first_acknowledged_at is None:
                    first_acknowledged_at = step["acknowledged_at"]
                assert step["acknowledged_at"] == first_acknowledged_at
            else:
                assert step["code"] == "lease_not_current"
    assert acknowledged_token is not None
    assert current_token is None
    _validate_lease_trace_immutable(trace)


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


@pytest.mark.parametrize("query", [
    {},
    {"limit": 1},
    {"limit": 50, "cursor": "nextPageAbc123_-"},
])
def test_h3_bounded_poll_query_schema_accepts_valid_normalized_inputs(query, schema_registry):
    schemas, registry = schema_registry
    _validate("delivery-poll-query.schema.json", query, schemas, registry)


@pytest.mark.parametrize("query", [
    {"limit": 0}, {"limit": 51}, {"limit": "20"}, {"limit": -1},
    {"cursor": ""}, {"cursor": "../other-installation"}, {"cursor": "!"}, 
    {"cursor": "x" * 1025}, {"installation_id": "another-tenant"},
])
def test_h3_bounded_poll_query_rejects_invalid_or_tenant_scoped_inputs(query, schema_registry):
    schemas, registry = schema_registry
    with pytest.raises(ValidationError):
        _validate("delivery-poll-query.schema.json", query, schemas, registry)


def test_h3_sources_are_compatible_with_public_local_source_contract():
    """Wire metadata maps into existing HA domain types; binary content is staged locally."""
    from custom_components.daylight_calendar_import.sources import (
        SourceAttachment, SourceDocument, SourceKind,
    )
    text_page = _read(FIXTURE_DIR / "valid" / "delivery-page-text.json")
    item = text_page["deliveries"][0]
    raw = item["source"]
    source = SourceDocument(
        id=item["source_id"],
        kind=SourceKind(raw["kind"]),
        received_at=datetime.fromisoformat(raw["received_at"]),
        title=raw.get("title"),
        text=raw.get("text"),
        metadata=raw["metadata"],
        upstream_source_id=item["source_id"],
    )
    assert source.kind is SourceKind.EMAIL
    assert source.id == source.upstream_source_id
    assert source.metadata == {"sender": "teacher@example.test"}
    assert source.text and not source.attachments

    attachment_page = _read(FIXTURE_DIR / "valid" / "delivery-page-attachment.json")
    item = attachment_page["deliveries"][0]
    descriptor = item["source"]["attachments"][0]
    # The local content_ref is created only after authenticated download,
    # size/hash verification and sandbox staging; never sent by Cloud.
    stored = SourceAttachment(
        id=descriptor["id"], media_type=descriptor["media_type"],
        size_bytes=descriptor["size_bytes"], sha256=descriptor["sha256"],
        filename=descriptor.get("filename"), content_ref="staged-local-ref",
    )
    assert stored.content_ref not in descriptor
    assert stored.sha256 == descriptor["sha256"]


def test_h3_wire_rejects_disallowed_calendar_writes_or_secrets(schema_registry):
    schemas, registry = schema_registry
    page = _read(FIXTURE_DIR / "valid" / "delivery-page-text.json")
    import copy
    for field in ("password", "authorization", "calendar_write", "model_provider"):
        modified = copy.deepcopy(page)
        modified["deliveries"][0]["source"]["metadata"][field] = "secret"
        with pytest.raises(ValidationError):
            _validate("delivery-page.schema.json", modified, schemas, registry)


def test_semantically_conflicting_attachment_ids_are_rejected(schema_registry):
    """uniqueItems cannot detect different descriptors with the same ID."""
    schemas, registry = schema_registry
    page = _read(
        FIXTURE_DIR / "invalid" / "delivery-page-duplicate-attachment-id-semantic.json"
    )
    # Structural schema validates: uniqueness by *ID* needs a semantic rule.
    _validate("delivery-page.schema.json", page, schemas, registry)
    with pytest.raises(ValueError, match="attachment IDs must be unique"):
        _validate_delivery_page_semantics(page)


def test_page_semantics_reject_duplicate_deliveries_and_aggregate_oversize():
    import copy

    page = _read(FIXTURE_DIR / "valid" / "delivery-page-attachment.json")
    _validate_delivery_page_semantics(page)
    duplicate = copy.deepcopy(page)
    duplicate["deliveries"].append(copy.deepcopy(duplicate["deliveries"][0]))
    with pytest.raises(ValueError, match="delivery IDs must be unique"):
        _validate_delivery_page_semantics(duplicate)

    oversized = copy.deepcopy(page)
    attachment = oversized["deliveries"][0]["source"]["attachments"][0]
    attachment["size_bytes"] = 6 * 1024 * 1024
    oversized["deliveries"][0]["source"]["attachments"].append(
        {**attachment, "id": "different-attachment"}
    )
    with pytest.raises(ValueError, match="aggregate attachment bytes"):
        _validate_delivery_page_semantics(oversized)


def test_released_lease_cannot_change_source_payload_before_retry_ack():
    import copy

    trace = _read(FIXTURE_DIR / "valid" / "delivery-stale-ack-trace.json")
    _validate_lease_trace_immutable(trace)
    changed_text = copy.deepcopy(trace)
    changed_text["steps"][2]["source"]["text"] += " silently changed"
    with pytest.raises(ValueError, match="original source payload"):
        _validate_lease_trace_immutable(changed_text)

    changed_attachment = copy.deepcopy(trace)
    attachment = {
        "id": "source.pdf", "media_type": "application/pdf", "size_bytes": 4096,
        "sha256": "a" * 64,
    }
    changed_attachment["steps"][2]["source"]["attachments"].append(attachment)
    with pytest.raises(ValueError, match="original source payload"):
        _validate_lease_trace_immutable(changed_attachment)
