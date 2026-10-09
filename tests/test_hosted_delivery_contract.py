"""Executable, cloud-independent H3 hosted-source protocol contract.

These tests validate public schemas/fixtures and normative lease/ACK traces.
They do not pretend to exercise the private cloud server or a live HA adapter.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from hashlib import sha256
import json
import math
import re
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


def _strict_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"non-finite JSON constant: {value}")


def _reject_surrogates(value):
    if isinstance(value, str):
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError("unpaired Unicode surrogate")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    elif isinstance(value, dict):
        for key, item in value.items():
            _reject_surrogates(key)
            _reject_surrogates(item)
    elif isinstance(value, list):
        for item in value:
            _reject_surrogates(item)


def _decode_strict(raw):
    result = json.loads(raw, object_pairs_hook=_strict_pairs, parse_constant=_reject_constant)
    _reject_surrogates(result)
    return result


def _read(path: Path) -> dict:
    return _decode_strict(path.read_text(encoding="utf-8"))


def _step(trace: dict, operation: str) -> dict:
    matches = [step for step in trace["steps"] if step["op"] == operation]
    assert len(matches) == 1
    return matches[0]


def _pending_records(trace: dict) -> dict:
    from custom_components.daylight_calendar_import.storage import PendingImport

    records = [PendingImport.from_dict(raw) for raw in trace.get("local_store_after_restart", {}).get("items", [])]
    assert len({record.id for record in records}) == len(records)
    return {record.id: record for record in records}


def _source_evidence_sha256(source: dict) -> str:
    """Canonical complete-source evidence independent of namespaced dedup ID."""
    def integer_values(value):
        # JSON Schema permits 1024.0 for an integer field; normalize its value.
        if isinstance(value, float):
            if not value.is_integer():
                raise ValueError("source numbers must be integers")
            return int(value)
        if isinstance(value, dict):
            return {key: integer_values(item) for key, item in value.items()}
        if isinstance(value, list):
            return [integer_values(item) for item in value]
        return value

    _reject_surrogates(source)
    canonical = json.dumps(
        integer_values(source), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def _ack_path_from_recovered_checkpoint(
    recovered: dict, *, expected_local_entry_id: str, pending_records: dict | None = None
) -> str:
    """Rebuild the ACK path using recovered checkpoint and independent local store.

    Production HA supplies expected_local_entry_id from its active config entry.
    Neither the original claim nor Cloud source bytes are required. Pending
    records come from independently loaded durable storage, never the checkpoint.
    """
    from custom_components.daylight_calendar_import.dedup import source_fingerprint

    assert recovered["local_config_entry_id"] == expected_local_entry_id
    delivery_id = recovered["delivery_id"]
    assert isinstance(delivery_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{16,128}", delivery_id)
    assert recovered["source_fingerprint"] == source_fingerprint(
        f"hosted:{expected_local_entry_id}:{delivery_id}"
    )
    def matches(value, pattern):
        return isinstance(value, str) and re.fullmatch(pattern, value) is not None

    digest = r"[a-f0-9]{64}"
    assert matches(recovered["source_evidence_sha256"], digest)
    assert matches(recovered["lease_token"], r"[A-Za-z0-9_-]{32,256}")
    expiry = recovered["source_expires_at"]
    assert matches(expiry, r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})")
    assert FormatChecker().conforms(expiry, "date-time")
    assert datetime.fromisoformat(expiry.upper()).utcoffset() is not None
    manifest = recovered["verified_attachments"]
    assert isinstance(manifest, dict) and len(manifest) <= 4
    assert all(
        matches(key, r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}") and matches(value, digest)
        for key, value in manifest.items()
    )
    descriptors = recovered["attachment_descriptors"]
    assert isinstance(descriptors, list) and len(descriptors) <= 4
    descriptor_validator = Draft202012Validator(_read(SCHEMA_DIR / "delivery-attachment.schema.json"))
    for descriptor in descriptors:
        descriptor_validator.validate(descriptor)
    expected_manifest = {descriptor["id"]: descriptor["sha256"] for descriptor in descriptors}
    assert len(expected_manifest) == len(descriptors)
    assert sum(descriptor["size_bytes"] for descriptor in descriptors) <= 10 * 1024 * 1024
    assert manifest == expected_manifest
    assert recovered["attachment_descriptors_sha256"] == _source_evidence_sha256({"attachments": descriptors})
    assert recovered["attachments_verification_complete"] is True
    disposition = recovered["disposition"]
    assert isinstance(disposition, dict)
    assert disposition.get("status") in {"pending", "no_events", "duplicate"}
    if disposition["status"] == "pending":
        assert set(disposition) == {"status", "pending_import_id"}
        pending_id = disposition["pending_import_id"]
        assert isinstance(pending_id, str) and 0 < len(pending_id) <= 128
        # This is a separately recovered local pending-store snapshot, not a
        # record manufactured from the checkpoint being authorized.
        record = (pending_records or {}).get(pending_id)
        assert record is not None and record.id == pending_id
        assert record.source_fingerprint == recovered["source_fingerprint"]
        assert record.events
    else:
        assert set(disposition) == {"status"}
    return f"/v1/sources/{delivery_id}/ack"


def _check_recovered_checkpoint(
    saved: dict, recovered: dict, *, expected_local_entry_id: str, pending_records: dict | None = None
) -> None:
    """Ensure a durable saved fixture reconstructs the same standalone ACK."""
    assert _ack_path_from_recovered_checkpoint(
        saved, expected_local_entry_id=expected_local_entry_id, pending_records=pending_records
    ) == _ack_path_from_recovered_checkpoint(
        recovered, expected_local_entry_id=expected_local_entry_id, pending_records=pending_records
    )
    assert saved["source_evidence_sha256"] == recovered["source_evidence_sha256"]
    assert saved["source_expires_at"] == recovered["source_expires_at"]
    assert saved["lease_token"] == recovered["lease_token"]
    assert saved["verified_attachments"] == recovered["verified_attachments"]
    assert saved["disposition"] == recovered["disposition"]
    assert saved["attachment_descriptors"] == recovered["attachment_descriptors"]
    assert saved["attachment_descriptors_sha256"] == recovered["attachment_descriptors_sha256"]


def _check_checkpoint_evidence(claim: dict, saved: dict, recovered: dict, *, pending_records: dict | None = None) -> None:
    """Verify source and attachment evidence before the original ACK."""
    _check_recovered_checkpoint(
        saved, recovered, expected_local_entry_id=claim["local_config_entry_id"], pending_records=pending_records
    )
    assert saved["delivery_id"] == claim["delivery_id"]
    assert recovered["delivery_id"] == claim["delivery_id"]
    assert saved["source_expires_at"] == claim["source_expires_at"]
    assert saved["lease_token"] == claim["token"]
    assert saved["attachment_descriptors"] == claim["source"]["attachments"]
    assert saved["verified_attachments"] == {
        attachment["id"]: attachment["sha256"]
        for attachment in claim["source"]["attachments"]
    }
    expected_evidence = _source_evidence_sha256(claim["source"])
    assert saved["source_evidence_sha256"] == expected_evidence
    assert recovered["source_evidence_sha256"] == expected_evidence

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


def _validate_delivery_page_semantics(page: dict, *, request: dict | None = None) -> None:
    """Semantic invariants the JSON Schema cannot express by property."""
    deliveries = page["deliveries"]
    if len(deliveries) > (request or {}).get("limit", 20):
        raise ValueError("page exceeds requested limit")
    delivery_ids = [item["delivery_id"] for item in deliveries]
    if len(delivery_ids) != len(set(delivery_ids)):
        raise ValueError("delivery IDs must be unique within a page")
    source_ids = [item["source_id"] for item in deliveries]
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("source IDs must be unique within a page")
    for item in deliveries:
        attachments = item["source"]["attachments"]
        ids = [attachment["id"] for attachment in attachments]
        if len(ids) != len(set(ids)):
            raise ValueError("attachment IDs must be unique within a source")
        if sum(attachment["size_bytes"] for attachment in attachments) > 10 * 1024 * 1024:
            raise ValueError("aggregate attachment bytes exceed 10 MiB")


def _validate_source_retention_window(*, enqueued_at: datetime, source_expires_at: datetime) -> None:
    """Cloud must reject sources whose immutable expiry exceeds seven days."""
    if not (enqueued_at < source_expires_at <= enqueued_at + timedelta(days=7)):
        raise ValueError("source retention must be within seven days of Cloud enqueue")


def _validate_claim_retention_window(*, claim_received_at: datetime, source_expires_at: datetime) -> None:
    """HA cannot trust a far-future expiry advertised by the hosted service."""
    if not (claim_received_at < source_expires_at <= claim_received_at + timedelta(days=7, minutes=5)):
        raise ValueError("advertised source expiry exceeds seven-day cap or already passed")


def test_h3_seven_day_source_retention_is_measured_from_cloud_enqueue():
    """No public ingestion timestamp is necessary; Cloud already owns created_at."""
    import copy

    item = _read(FIXTURE_DIR / "valid" / "delivery-page-text.json")["deliveries"][0]
    enqueued_at = datetime.fromisoformat("2026-10-08T16:00:00Z")
    observed_at = datetime.fromisoformat("2026-10-08T16:01:00Z")
    expiry = datetime.fromisoformat(item["source_expires_at"])
    _validate_source_retention_window(enqueued_at=enqueued_at, source_expires_at=expiry)
    _validate_claim_retention_window(claim_received_at=observed_at, source_expires_at=expiry)

    # The email's received_at is not Cloud ingestion time. Old forwarded
    # emails must remain valid with an expiry anchored to Cloud enqueue.
    historical_email = copy.deepcopy(item)
    historical_email["source"]["received_at"] = "2025-01-15T10:00:00Z"
    _validate_source_retention_window(
        enqueued_at=enqueued_at,
        source_expires_at=datetime.fromisoformat(historical_email["source_expires_at"]),
    )

    for invalid_expiry in (
        enqueued_at,
        enqueued_at - timedelta(seconds=1),
        expiry + timedelta(seconds=1),
        enqueued_at + timedelta(days=365 * 50),
    ):
        with pytest.raises(ValueError, match="seven days"):
            _validate_source_retention_window(
                enqueued_at=enqueued_at, source_expires_at=invalid_expiry,
            )

    for invalid_expiry in (
        observed_at,
        observed_at + timedelta(days=7, minutes=5, seconds=1),
        observed_at + timedelta(days=365 * 50),
    ):
        with pytest.raises(ValueError, match="seven-day"):
            _validate_claim_retention_window(
                claim_received_at=observed_at, source_expires_at=invalid_expiry,
            )

    _validate_claim_retention_window(
        claim_received_at=observed_at,
        source_expires_at=observed_at + timedelta(days=7, minutes=5),
    )


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
        _validate_lease_window(
            claim_started_at=datetime.fromisoformat("2026-10-08T16:00:00Z"),
            lease_expires_at=claimed, source_expires_at=expires,
        )
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
        "claim", "download_and_verify", "source_normalized", "parse_success",
        "persist_local_checkpoint", "restart_ha",
        "recover_checkpoint", "ack",
    ]
    saved = _step(trace, "persist_local_checkpoint")
    recovered = _step(trace, "recover_checkpoint")
    assert saved["checkpoint_id"] == recovered["checkpoint_id"]
    assert _step(trace, "parse_success")["event_count"] == 1
    assert saved["disposition"] == {"status": "pending", "pending_import_id": saved["checkpoint_id"]}
    _check_checkpoint_evidence(steps[0], saved, recovered, pending_records=_pending_records(trace))
    assert steps[0]["token"] == steps[-1]["token"]
    assert trace["source_id"] != trace["delivery_id"]


@pytest.mark.parametrize("claim", [
    {"schema_version": 1, "claim_request_id": "ClaimKey_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890", "claim_request_expires_at": "2026-10-08T15:05:00Z"},
    {"schema_version": 1, "claim_request_id": "ClaimKey_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890", "claim_request_expires_at": "2026-10-08T15:05:00Z", "limit": 1},
    {"schema_version": 1, "claim_request_id": "ClaimKey_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890", "claim_request_expires_at": "2026-10-08T15:05:00Z", "limit": 50, "cursor": "nextPageAbc123_-"},
])
def test_h3_bounded_claim_request_schema_accepts_valid_json(claim, schema_registry):
    schemas, registry = schema_registry
    _validate("delivery-claim-request.schema.json", claim, schemas, registry)


@pytest.mark.parametrize("claim", [
    {}, {"schema_version": 1}, {"schema_version": 2, "claim_request_id": "ClaimKey_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890", "claim_request_expires_at": "2026-10-08T15:05:00Z"}, {"schema_version": 1, "claim_request_id": "ClaimKey_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890", "claim_request_expires_at": "2026-10-08T15:05:00Z", "limit": 0},
    {"schema_version": 1, "claim_request_id": "ClaimKey_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890", "claim_request_expires_at": "2026-10-08T15:05:00Z", "limit": 51}, {"schema_version": 1, "claim_request_id": "ClaimKey_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890", "claim_request_expires_at": "2026-10-08T15:05:00Z", "limit": "20"},
    {"schema_version": 1, "claim_request_id": "ClaimKey_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890", "claim_request_expires_at": "2026-10-08T15:05:00Z", "limit": -1}, {"schema_version": 1, "claim_request_id": "ClaimKey_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890", "claim_request_expires_at": "2026-10-08T15:05:00Z", "cursor": ""},
    {"schema_version": 1, "claim_request_id": "ClaimKey_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890", "claim_request_expires_at": "2026-10-08T15:05:00Z", "cursor": "../other-installation"},
    {"schema_version": 1, "claim_request_id": "ClaimKey_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890", "claim_request_expires_at": "2026-10-08T15:05:00Z", "cursor": "!"},
    {"schema_version": 1, "claim_request_id": "ClaimKey_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890", "claim_request_expires_at": "2026-10-08T15:05:00Z", "cursor": "x" * 1025},
    {"schema_version": 1, "claim_request_id": "ClaimKey_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890", "claim_request_expires_at": "2026-10-08T15:05:00Z", "installation_id": "another-tenant"},
])
def test_h3_bounded_claim_request_rejects_invalid_or_tenant_scoped_input(claim, schema_registry):
    schemas, registry = schema_registry
    with pytest.raises(ValidationError):
        _validate("delivery-claim-request.schema.json", claim, schemas, registry)


def test_h3_sources_are_compatible_with_public_local_source_contract():
    """Wire metadata maps into existing HA domain types; binary content is staged locally."""
    from custom_components.daylight_calendar_import.sources import (
        SourceAttachment, SourceDocument, SourceKind,
    )
    text_page = _read(FIXTURE_DIR / "valid" / "delivery-page-text.json")
    item = text_page["deliveries"][0]
    raw = item["source"]
    # Source ID is source correlation only. The durable local fingerprint
    # uses stable delivery identity qualified by the local HA config entry.
    local_identity = "hosted:local-config-entry-1:" + item["delivery_id"]
    source = SourceDocument(
        id=item["source_id"],
        kind=SourceKind(raw["kind"]),
        received_at=datetime.fromisoformat(raw["received_at"]),
        title=raw.get("title"),
        text=raw.get("text"),
        metadata=raw["metadata"],
        upstream_source_id=local_identity,
    )
    assert source.kind is SourceKind.EMAIL
    assert source.id == item["source_id"]
    assert source.upstream_source_id == local_identity
    assert source.id != source.upstream_source_id
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


def test_page_semantics_reject_distinct_deliveries_reusing_source_identity(
    schema_registry,
):
    """Two different delivery IDs must not mask a duplicate source ID."""
    schemas, registry = schema_registry
    page = _read(
        FIXTURE_DIR / "invalid" / "delivery-page-duplicate-source-id-semantic.json"
    )
    _validate("delivery-page.schema.json", page, schemas, registry)
    assert page["deliveries"][0]["delivery_id"] != page["deliveries"][1]["delivery_id"]
    assert page["deliveries"][0]["source_id"] == page["deliveries"][1]["source_id"]
    with pytest.raises(ValueError, match="source IDs must be unique"):
        _validate_delivery_page_semantics(page)


def test_hosted_delivery_fingerprint_keys_are_installation_namespaced():
    """A source collision cannot ACK another delivery or local installation."""
    from custom_components.daylight_calendar_import.dedup import source_fingerprint

    def identity(local_entry: str, delivery_id: str) -> str:
        return f"hosted:{local_entry}:{delivery_id}"

    key = identity("entry-one", "delivery_00000000000000000001")
    assert source_fingerprint(key) == source_fingerprint(key)
    assert source_fingerprint(key) != source_fingerprint(
        identity("entry-one", "delivery_00000000000000000002")
    )
    assert source_fingerprint(key) != source_fingerprint(
        identity("entry-two", "delivery_00000000000000000001")
    )


def _validate_terminal_ack_trace(trace: dict) -> None:
    """Model only the documented public ordering constraint; not a mock cloud."""
    steps = trace["steps"]
    operations = [step["op"] for step in steps]
    status = trace["disposition_status"]
    assert status in {"no_events", "duplicate"}
    parse_op = f"parse_{status}"
    persist_op = f"persist_terminal_{status}"
    recover_op = f"recover_terminal_{status}"
    assert operations[0] == "claim"
    assert operations[-1] == "ack"
    assert operations.index(parse_op) < operations.index(persist_op)
    assert operations.index(persist_op) < operations.index("ack")
    assert operations.index(persist_op) < operations.index("restart_ha")
    assert operations.index("restart_ha") < operations.index(recover_op)
    assert operations.index(recover_op) < operations.index("ack")
    saved = _step(trace, persist_op)
    recovered = _step(trace, recover_op)
    assert saved["disposition"] == recovered["disposition"] == {"status": status}
    _check_checkpoint_evidence(steps[0], saved, recovered, pending_records=_pending_records(trace))


def test_durable_no_event_disposition_can_be_acknowledged_after_restart():
    import copy

    trace = _read(FIXTURE_DIR / "valid" / "delivery-no-events-client-trace.json")
    _validate_terminal_ack_trace(trace)
    assert trace["steps"][0]["token"] == trace["steps"][-1]["token"]
    premature = copy.deepcopy(trace)
    premature["steps"].pop(2)
    with pytest.raises((AssertionError, StopIteration, ValueError)):
        _validate_terminal_ack_trace(premature)
    inconsistent = copy.deepcopy(trace)
    inconsistent["steps"][4]["source_fingerprint"] = "other-hash"
    with pytest.raises(AssertionError):
        _validate_terminal_ack_trace(inconsistent)
    altered_evidence = copy.deepcopy(trace)
    altered_evidence["steps"][4]["source_evidence_sha256"] = "0" * 64
    with pytest.raises(AssertionError):
        _validate_terminal_ack_trace(altered_evidence)
    altered_redelivery = copy.deepcopy(trace)
    altered_redelivery["steps"][0]["source"]["text"] += " tampered"
    with pytest.raises(AssertionError):
        _validate_terminal_ack_trace(altered_redelivery)


def _validate_ack_tombstone_trace(trace: dict) -> None:
    """Wire-idempotency lifetime is bounded by the source expiration."""
    steps = trace["steps"]
    expiry = datetime.fromisoformat(trace["source_expires_at"])
    confirmed = steps[0]
    assert confirmed["op"] == "ack_confirmed" and confirmed["status"] == 200
    original_time = confirmed["acknowledged_at"]
    confirmed_token = confirmed["token"]
    assert datetime.fromisoformat(original_time) < expiry
    assert steps[1]["op"] == "delete_source_bytes"
    assert datetime.fromisoformat(steps[1]["at"]) < expiry
    first_retry = steps[2]
    assert first_retry["op"] == "retry_ack"
    assert datetime.fromisoformat(first_retry["at"]) < expiry
    assert first_retry["token"] == confirmed_token
    assert first_retry["status"] == 200
    assert first_retry["acknowledged_at"] == original_time
    assert steps[3]["op"] == "expire_tombstone"
    assert datetime.fromisoformat(steps[3]["at"]) >= expiry
    last_retry = steps[4]
    assert last_retry["op"] == "retry_ack"
    assert datetime.fromisoformat(last_retry["at"]) >= expiry
    assert last_retry["status"] == 404
    assert last_retry["code"] == "not_found"


def test_ack_tombstone_survives_raw_source_deletion_through_expiry():
    import copy

    trace = _read(FIXTURE_DIR / "valid" / "delivery-ack-tombstone-trace.json")
    _validate_ack_tombstone_trace(trace)
    missing_idempotency = copy.deepcopy(trace)
    missing_idempotency["steps"][2]["status"] = 410
    with pytest.raises(AssertionError):
        _validate_ack_tombstone_trace(missing_idempotency)
    early_delete = copy.deepcopy(trace)
    early_delete["steps"][3]["at"] = "2026-10-09T16:00:00Z"
    with pytest.raises(AssertionError):
        _validate_ack_tombstone_trace(early_delete)
    mutated_time = copy.deepcopy(trace)
    mutated_time["steps"][2]["acknowledged_at"] = "2026-10-10T16:00:00Z"
    with pytest.raises(AssertionError):
        _validate_ack_tombstone_trace(mutated_time)
    wrong_terminal = copy.deepcopy(trace)
    wrong_terminal["steps"][4]["status"] = 410
    wrong_terminal["steps"][4]["code"] = "source_expired"
    with pytest.raises(AssertionError):
        _validate_ack_tombstone_trace(wrong_terminal)


def _validate_identity_history(claims: list[dict]) -> None:
    """No source or delivery ID is reassigned to new immutable bytes over time."""
    sources: dict[str, tuple[str, dict]] = {}
    deliveries: dict[str, tuple[str, dict]] = {}
    for claim in claims:
        source_id = claim["source_id"]
        delivery_id = claim["delivery_id"]
        payload = claim["source"]
        expiry = claim["source_expires_at"]
        if source_id in sources and sources[source_id] != (delivery_id, payload, expiry):
            raise ValueError("source identity reassigned to different delivery, content or expiry")
        if delivery_id in deliveries and deliveries[delivery_id] != (source_id, payload, expiry):
            raise ValueError("delivery identity reassigned to different source, content or expiry")
        sources[source_id] = (delivery_id, payload, expiry)
        deliveries[delivery_id] = (source_id, payload, expiry)


def test_source_and_delivery_ids_cannot_be_reassigned_across_poll_cycles():
    import copy

    original = _read(FIXTURE_DIR / "valid" / "delivery-page-text.json")["deliveries"][0]
    renewed = copy.deepcopy(original)
    renewed["lease_token"] = "leaseToken_NewClaim1234567890ABCDEFGHIJKLMNOPQRSTUV"
    _validate_identity_history([original, renewed])

    shortened_expiry = copy.deepcopy(renewed)
    shortened_expiry["source_expires_at"] = "2026-10-12T16:00:00Z"
    with pytest.raises(ValueError, match="source identity reassigned"):
        _validate_identity_history([original, shortened_expiry])
    extended_expiry = copy.deepcopy(renewed)
    extended_expiry["source_expires_at"] = "2026-10-18T16:00:00Z"
    with pytest.raises(ValueError, match="source identity reassigned"):
        _validate_identity_history([original, extended_expiry])

    reused_source = copy.deepcopy(original)
    reused_source["delivery_id"] = "delivery_00000000000000000099"
    with pytest.raises(ValueError, match="source identity reassigned"):
        _validate_identity_history([original, reused_source])

    replaced_content = copy.deepcopy(original)
    replaced_content["source"]["text"] = "Changed email under the original ID"
    with pytest.raises(ValueError, match="source identity reassigned"):
        _validate_identity_history([original, replaced_content])

    reused_delivery = copy.deepcopy(original)
    reused_delivery["source_id"] = "source_000000000000000000000099"
    with pytest.raises(ValueError, match="delivery identity reassigned"):
        _validate_identity_history([original, reused_delivery])


def test_same_source_after_retention_requires_fresh_delivery_identity():
    """Bounded Cloud retention must not require a permanent upstream mapping."""
    import copy

    original = _read(FIXTURE_DIR / "valid" / "delivery-page-text.json")["deliveries"][0]
    reingested = copy.deepcopy(original)
    reingested["delivery_id"] = "delivery_00000000000000000099"
    reingested["source_id"] = "source_000000000000000000000099"
    reingested["source_expires_at"] = "2026-10-23T16:00:00Z"
    reingested["lease_expires_at"] = "2026-10-16T16:05:00Z"
    reingested["lease_token"] = "leaseToken_NewClaim1234567890ABCDEFGHIJKLMNOPQRSTUV"
    _validate_identity_history([original, reingested])

    # The source may also have changed after expiry, but neither old ID
    # may be reassigned: a fresh delivery and source ID are necessary.
    changed_after_expiry = copy.deepcopy(reingested)
    changed_after_expiry["source"]["text"] = "Updated events after the old retention period"
    _validate_identity_history([original, changed_after_expiry])

    attempted_revival = copy.deepcopy(reingested)
    attempted_revival["delivery_id"] = original["delivery_id"]
    with pytest.raises(ValueError, match="delivery identity reassigned"):
        _validate_identity_history([original, attempted_revival])


def test_durable_pending_checkpoint_requires_content_evidence_after_restart():
    import copy

    trace = _read(FIXTURE_DIR / "valid" / "delivery-durable-client-trace.json")
    claim, saved, recovered = trace["steps"][0], _step(trace, "persist_local_checkpoint"), _step(trace, "recover_checkpoint")
    _check_checkpoint_evidence(claim, saved, recovered, pending_records=_pending_records(trace))
    changed = copy.deepcopy(trace)
    changed["steps"][0]["source"]["metadata"]["sender"] = "attacker@example.test"
    with pytest.raises(AssertionError):
        _check_checkpoint_evidence(changed["steps"][0], saved, recovered, pending_records=_pending_records(trace))
    lost = copy.deepcopy(trace)
    del _step(lost, "recover_checkpoint")["source_evidence_sha256"]
    with pytest.raises(KeyError):
        _check_checkpoint_evidence(claim, saved, _step(lost, "recover_checkpoint"), pending_records=_pending_records(trace))


@pytest.mark.parametrize("raw", [
    '{"schema_version":1,"schema_version":1}',
    '{"schema_version":NaN}',
    '{"schema_version":Infinity}',
    '{"size_bytes":1e999}',
    '{"source":{"text":"\\ud800"}}',
    '{"\\udfff":"value"}',
])
def test_h3_strict_json_rejects_invalid_wire_documents(raw):
    with pytest.raises(ValueError):
        _decode_strict(raw)


@pytest.mark.parametrize(("field", "value"), [
    ("delivery_id", "A" * 16 + "\n"),
    ("source_id", "B" * 16 + "\n"),
    ("lease_token", "C" * 32 + "\n"),
])
def test_h3_identifier_patterns_reject_trailing_newline(field, value, schema_registry):
    schemas, registry = schema_registry
    page = _read(FIXTURE_DIR / "valid" / "delivery-page-text.json")
    page["deliveries"][0][field] = value
    with pytest.raises(ValidationError):
        _validate("delivery-page.schema.json", page, schemas, registry)


def test_h3_checkpoint_rejects_wrong_namespace_delivery_or_expiry():
    import copy

    trace = _read(FIXTURE_DIR / "valid" / "delivery-durable-client-trace.json")
    claim, saved, recovered = trace["steps"][0], _step(trace, "persist_local_checkpoint"), _step(trace, "recover_checkpoint")
    _check_checkpoint_evidence(claim, saved, recovered, pending_records=_pending_records(trace))
    for key, value in [
        ("local_config_entry_id", "entry-two"),
        ("delivery_id", "delivery_00000000000000000099"),
        ("source_expires_at", "2026-10-12T16:00:00Z"),
    ]:
        changed = copy.deepcopy(claim)
        changed[key] = value
        with pytest.raises(AssertionError):
            _check_checkpoint_evidence(changed, saved, recovered, pending_records=_pending_records(trace))
    changed = copy.deepcopy(recovered)
    changed["source_expires_at"] = "2026-10-12T16:00:00Z"
    with pytest.raises(AssertionError):
        _check_checkpoint_evidence(claim, saved, changed, pending_records=_pending_records(trace))


@pytest.mark.parametrize("request_id", [
    "", "too-short", "A" * 22 + "\n", "../unsafe", "A" * 129,
])
def test_claim_identity_rejects_missing_weak_and_trailing_newline(request_id, schema_registry):
    schemas, registry = schema_registry
    with pytest.raises(ValidationError):
        _validate(
            "delivery-claim-request.schema.json",
            {"schema_version": 1, "claim_request_id": request_id, "claim_request_expires_at": "2026-10-08T15:05:00Z"},
            schemas, registry,
        )


def _check_claim_idempotency_trace(trace: dict, schemas: dict, registry: Registry) -> None:
    """Retries of a consumed request ID never lease another page."""
    originals = {}
    for step in trace["steps"]:
        request_id = step["request_id"]
        parameters = step["parameters"]
        now = datetime.fromisoformat(step["server_at"])
        deadline = datetime.fromisoformat(parameters["claim_request_expires_at"])
        assert deadline.utcoffset() is not None
        if step["op"] in ("expired_after_marker_purge", "expired_at_boundary"):
            # Check expiry BEFORE checking a persisted consumed-ID marker.
            assert now >= deadline
            assert request_id in originals
            assert step["status"] == 400
            assert step["code"] == "claim_request_expired"
            assert step["new_leases_issued"] is False
            continue
        assert now < deadline
        if step["status"] == 200:
            _validate("delivery-page.schema.json", step["response"], schemas, registry)
            _validate_delivery_page_semantics(step["response"], request=parameters)
        if step["op"] == "claim":
            assert deadline <= now + timedelta(minutes=5)
            assert request_id not in originals
            assert step["status"] == 200
            originals[request_id] = (parameters, step["response"])
        else:
            prior_parameters, prior_response = originals[request_id]
            assert step["new_leases_issued"] is False
            if step["op"] == "retry_same":
                assert parameters == prior_parameters
                assert step["status"] == 200
                assert step["response"] == prior_response
            elif step["op"] == "retry_after_cleanup":
                assert parameters == prior_parameters
                assert step["status"] == 409
                assert step["code"] == "claim_not_replayable"
            elif step["op"] == "retry_changed_parameters":
                assert parameters != prior_parameters
                assert step["status"] == 400
                assert step["code"] == "invalid_request"
            else:
                raise ValueError("unexpected request-ID trace operation")


def test_claim_replays_do_not_allocate_second_batch(schema_registry):
    import copy

    schemas, registry = schema_registry
    trace = _read(FIXTURE_DIR / "valid" / "delivery-idempotent-claim-trace.json")
    _check_claim_idempotency_trace(trace, schemas, registry)
    changed_token = copy.deepcopy(trace)
    changed_token["steps"][1]["response"]["deliveries"][0]["lease_token"] = "Z" * 48
    with pytest.raises(AssertionError):
        _check_claim_idempotency_trace(changed_token, schemas, registry)
    repeated_after_ack = copy.deepcopy(trace)
    repeated_after_ack["steps"][2]["new_leases_issued"] = True
    with pytest.raises(AssertionError):
        _check_claim_idempotency_trace(repeated_after_ack, schemas, registry)
    changed_params = copy.deepcopy(trace)
    changed_params["steps"][3]["parameters"]["limit"] = 20
    with pytest.raises(AssertionError):
        _check_claim_idempotency_trace(changed_params, schemas, registry)
    late_replay_claimed = copy.deepcopy(trace)
    late_replay_claimed["steps"][4]["status"] = 200
    late_replay_claimed["steps"][4]["new_leases_issued"] = True
    with pytest.raises(AssertionError):
        _check_claim_idempotency_trace(late_replay_claimed, schemas, registry)
    boundary_before_expiry = copy.deepcopy(trace)
    boundary_before_expiry["steps"][5]["server_at"] = "2026-10-08T15:04:59Z"
    with pytest.raises(AssertionError):
        _check_claim_idempotency_trace(boundary_before_expiry, schemas, registry)
    excessive_future = copy.deepcopy(trace)
    excessive_future["steps"][0]["parameters"]["claim_request_expires_at"] = (
        "2026-10-08T17:05:00Z"
    )
    with pytest.raises(AssertionError):
        _check_claim_idempotency_trace(excessive_future, schemas, registry)


@pytest.mark.parametrize(("expires", "now", "accepted"), [
    ("2026-10-08T15:05:00Z", "2026-10-08T15:00:00Z", True),
    ("2026-10-08T15:05:00Z", "2026-10-08T15:04:59Z", True),
    ("2026-10-08T15:05:00Z", "2026-10-08T15:05:00Z", False),
    ("2026-10-08T15:05:00Z", "2026-10-15T15:05:00Z", False),
    ("2026-10-08T15:06:00Z", "2026-10-08T15:00:00Z", False),
])
def test_claim_request_server_admission_clock_boundary(expires, now, accepted):
    timestamp = datetime.fromisoformat(expires)
    observed_at = datetime.fromisoformat(now)
    assert (observed_at < timestamp <= observed_at + timedelta(minutes=5)) is accepted


def test_lost_ack_response_after_attachment_cleanup_uses_durable_proof():
    import copy

    trace = _read(FIXTURE_DIR / "valid" / "delivery-ack-retry-after-cleanup.json")
    steps = trace["steps"]
    assert [step["op"] for step in steps] == [
        "claim", "download_and_verify", "parse_success", "persist_local_checkpoint",
        "ack_applied", "delete_cloud_source", "recover_checkpoint", "retry_same_ack",
    ]
    claim, saved, recovered = steps[0], _step(trace, "persist_local_checkpoint"), _step(trace, "recover_checkpoint")
    # First ACK was based on the full claim, before Cloud deleted bytes.
    _check_checkpoint_evidence(claim, saved, recovered, pending_records=_pending_records(trace))
    # The ambiguous *retry* must be decidable using recovered local data alone.
    _check_recovered_checkpoint(
        saved, recovered, expected_local_entry_id="entry-one",
        pending_records=_pending_records(trace),
    )
    assert _ack_path_from_recovered_checkpoint(
        recovered, expected_local_entry_id="entry-one",
        pending_records=_pending_records(trace),
    ) == "/v1/sources/delivery_00000000000000000002/ack"
    assert _step(trace, "parse_success")["event_count"] == 1
    assert saved["disposition"] == {"status": "pending", "pending_import_id": saved["checkpoint_id"]}
    retry = _step(trace, "retry_same_ack")
    assert retry["token"] == recovered["lease_token"]
    assert retry["status"] == 200
    assert retry["acknowledged_at"] == _step(trace, "ack_applied")["acknowledged_at"]
    assert recovered["cloud_attachment_bytes_available"] is False
    assert saved["verified_attachments"] == steps[1]["verified_attachments"]
    for field, value in [
        ("verified_attachments", {}),
        ("attachments_verification_complete", False),
        ("lease_token", "Z" * 48),
    ]:
        corrupt = copy.deepcopy(recovered)
        corrupt[field] = value
        with pytest.raises(AssertionError):
            _check_checkpoint_evidence(claim, saved, corrupt, pending_records=_pending_records(trace))


def test_recovered_ack_path_requires_durable_delivery_id_and_entry():
    """Recovery works without Cloud source, pre-restart claim or any ID reversal."""
    import copy

    trace = _read(FIXTURE_DIR / "valid" / "delivery-ack-retry-after-cleanup.json")
    saved, recovered = _step(trace, "persist_local_checkpoint"), _step(trace, "recover_checkpoint")
    _check_recovered_checkpoint(saved, recovered, expected_local_entry_id="entry-one", pending_records=_pending_records(trace))
    assert _ack_path_from_recovered_checkpoint(
        recovered, expected_local_entry_id="entry-one",
        pending_records=_pending_records(trace),
    ) == "/v1/sources/delivery_00000000000000000002/ack"

    # A fingerprint is one-way; it cannot supply an omitted delivery ID.
    for field in ("delivery_id", "local_config_entry_id"):
        for target in ("saved", "recovered"):
            saved_copy, recovered_copy = copy.deepcopy(saved), copy.deepcopy(recovered)
            del (saved_copy if target == "saved" else recovered_copy)[field]
            with pytest.raises(KeyError):
                _check_recovered_checkpoint(
                    saved_copy, recovered_copy, expected_local_entry_id="entry-one",
                    pending_records=_pending_records(trace),
                )

    for field, wrong_value in (
        ("delivery_id", "delivery_00000000000000000099"),
        ("local_config_entry_id", "entry-two"),
    ):
        for target in ("saved", "recovered"):
            saved_copy, recovered_copy = copy.deepcopy(saved), copy.deepcopy(recovered)
            (saved_copy if target == "saved" else recovered_copy)[field] = wrong_value
            with pytest.raises(AssertionError):
                _check_recovered_checkpoint(
                    saved_copy, recovered_copy, expected_local_entry_id="entry-one",
                    pending_records=_pending_records(trace),
                )

    # Even a consistently mislabeled namespace is rejected using the HA entry.
    with pytest.raises(AssertionError):
        _check_recovered_checkpoint(
            saved, recovered, expected_local_entry_id="another-config-entry",
            pending_records=_pending_records(trace),
        )


def _validate_cursor_isolation_trace(trace, schemas, registry):
    errors = []
    assert {step["scenario"] for step in trace["steps"]} == {
        "other_installation", "unknown", "expired", "tampered",
    }
    for step in trace["steps"]:
        assert step["op"] == "claim_with_cursor"
        assert step["authenticated"] is True
        assert step["status"] == 400
        _validate("error.schema.json", step["response"], schemas, registry)
        observable = {key: value for key, value in step["response"].items() if key != "request_id"}
        assert observable["error"]["code"] == "invalid_cursor"
        assert observable["error"]["retryable"] is False
        errors.append(observable)
    assert all(error == errors[0] for error in errors)


def test_cursor_tenant_isolation_has_one_non_enumerating_error(schema_registry):
    import copy

    schemas, registry = schema_registry
    trace = _read(FIXTURE_DIR / "valid" / "delivery-cursor-isolation-trace.json")
    _validate_cursor_isolation_trace(trace, schemas, registry)
    leaked = copy.deepcopy(trace)
    leaked["steps"][0]["response"]["error"]["message"] = "cursor belongs to another installation"
    with pytest.raises(AssertionError):
        _validate_cursor_isolation_trace(leaked, schemas, registry)


def test_ack_response_rejects_ending_line_break(schema_registry):
    schemas, registry = schema_registry
    valid = _read(FIXTURE_DIR / "valid" / "delivery-ack-response.json")
    _validate("delivery-ack-response.schema.json", valid, schemas, registry)
    valid["delivery_id"] += "\n"
    with pytest.raises(ValidationError):
        _validate("delivery-ack-response.schema.json", valid, schemas, registry)


def _validate_lease_window(*, claim_started_at, lease_expires_at, source_expires_at):
    if not (claim_started_at < lease_expires_at <= min(
        claim_started_at + timedelta(minutes=30), source_expires_at,
    )):
        raise ValueError("lease exceeds claim-relative deadline")


@pytest.mark.parametrize("seconds,valid", [(0, False), (1, True), (1800, True), (1801, False), (86400, False)])
def test_claim_relative_lease_deadline(seconds, valid):
    start = datetime.fromisoformat("2026-10-08T16:00:00Z")
    kwargs = dict(claim_started_at=start, lease_expires_at=start + timedelta(seconds=seconds),
                  source_expires_at=start + timedelta(days=7))
    if valid:
        _validate_lease_window(**kwargs)
    else:
        with pytest.raises(ValueError, match="claim-relative"):
            _validate_lease_window(**kwargs)
    with pytest.raises(ValueError):
        _validate_lease_window(claim_started_at=start, lease_expires_at=start + timedelta(minutes=5),
                               source_expires_at=start + timedelta(minutes=4))


@pytest.mark.parametrize("limit", [None, 1, 20, 50])
def test_claim_page_respects_effective_requested_limit(limit, schema_registry):
    import copy

    schemas, registry = schema_registry
    item = _read(FIXTURE_DIR / "valid" / "delivery-page-text.json")["deliveries"][0]
    request = _read(FIXTURE_DIR / "valid" / "delivery-claim-request.json")
    request.pop("limit", None)
    if limit is not None:
        request["limit"] = limit
    _validate("delivery-claim-request.schema.json", request, schemas, registry)
    bound = request.get("limit", 20)
    page = {"schema_version": 1, "deliveries": [], "next_cursor": None}
    for index in range(bound + 1):
        entry = copy.deepcopy(item)
        entry["delivery_id"] = f"delivery_{index:024d}"
        entry["source_id"] = f"source_{index:024d}"
        page["deliveries"].append(entry)
    if bound < 50:
        _validate("delivery-page.schema.json", page, schemas, registry)
    with pytest.raises(ValueError, match="requested limit"):
        _validate_delivery_page_semantics(page, request=request)
    page["deliveries"].pop()
    _validate("delivery-page.schema.json", page, schemas, registry)
    _validate_delivery_page_semantics(page, request=request)


@pytest.mark.parametrize("field,value", [
    ("delivery_id", "../unsafe"), ("delivery_id", "A" * 16 + "\n"),
    ("lease_token", True), ("lease_token", "short"), ("lease_token", "A" * 32 + "\n"),
    ("source_evidence_sha256", "z" * 64), ("source_evidence_sha256", "A" * 64),
    ("source_evidence_sha256", 123), ("source_expires_at", "2026-10-08"),
    ("source_expires_at", "2026-10-08T16:00:00"),
    ("source_expires_at", "2026-10-08 16:00:00+00:00"),
    ("verified_attachments", {"../unsafe": "a" * 64}),
    ("verified_attachments", {"flyer.pdf": "z" * 64}),
    ("verified_attachments", {"flyer.pdf": True}),
    ("verified_attachments", {str(i): "a" * 64 for i in range(5)}),
])
def test_standalone_recovered_ack_rejects_malformed_proof(field, value):
    from custom_components.daylight_calendar_import.dedup import source_fingerprint

    trace = _read(FIXTURE_DIR / "valid" / "delivery-ack-retry-after-cleanup.json")
    recovered = _step(trace, "recover_checkpoint")
    recovered[field] = value
    if field == "delivery_id":
        recovered["source_fingerprint"] = source_fingerprint(f"hosted:entry-one:{value}")
    with pytest.raises(AssertionError):
        _ack_path_from_recovered_checkpoint(recovered, expected_local_entry_id="entry-one", pending_records=_pending_records(trace))


def test_source_evidence_string_escaping_vector(schema_registry):
    schemas, registry = schema_registry
    vector = _read(FIXTURE_DIR / "valid" / "delivery-evidence-canonical-vector.json")
    _validate("delivery-source.schema.json", vector["source"], schemas, registry)
    assert _source_evidence_sha256(vector["source"]) == vector["sha256"]
    assert sha256(vector["canonical_utf8"].encode("utf-8")).hexdigest() == vector["sha256"]
    # Alternate legal JSON escaping and member order decode to the same evidence.
    alternate = json.dumps(vector["source"], ensure_ascii=True).replace("/", "\\/")
    assert _source_evidence_sha256(_decode_strict(alternate)) == vector["sha256"]
    source = _read(FIXTURE_DIR / "valid" / "delivery-page-attachment.json")["deliveries"][0]["source"]
    original = _source_evidence_sha256(source)
    source["attachments"][0]["size_bytes"] = 1024.0
    assert _source_evidence_sha256(source) == original


@pytest.mark.parametrize("disposition", [
    None, {}, {"status": "downloaded"}, {"status": "failed"},
    {"status": "processing"}, {"status": "pending"},
    {"status": "pending", "pending_import_id": ""},
    {"status": "pending", "pending_import_id": True},
    {"status": "no_events", "pending_import_id": "unexpected"},
])
def test_recovered_proof_without_eligible_disposition_cannot_authorize_ack(disposition):
    trace = _read(FIXTURE_DIR / "valid" / "delivery-ack-retry-after-cleanup.json")
    recovered = _step(trace, "recover_checkpoint")
    recovered["disposition"] = disposition
    with pytest.raises(AssertionError):
        _ack_path_from_recovered_checkpoint(recovered, expected_local_entry_id="entry-one", pending_records=_pending_records(trace))
    del recovered["disposition"]
    with pytest.raises(KeyError):
        _ack_path_from_recovered_checkpoint(recovered, expected_local_entry_id="entry-one", pending_records=_pending_records(trace))


def test_disposition_survives_restart_and_duplicate_is_ack_eligible():
    import copy

    trace = _read(FIXTURE_DIR / "valid" / "delivery-duplicate-client-trace.json")
    _validate_terminal_ack_trace(trace)
    saved = _step(trace, "persist_terminal_duplicate")
    recovered = _step(trace, "recover_terminal_duplicate")
    assert _ack_path_from_recovered_checkpoint(recovered, expected_local_entry_id="entry-one", pending_records=_pending_records(trace))
    corrupt = copy.deepcopy(recovered)
    corrupt["disposition"] = {"status": "no_events"}
    with pytest.raises(AssertionError):
        _check_recovered_checkpoint(saved, corrupt, expected_local_entry_id="entry-one", pending_records=_pending_records(trace))

    pending = _read(FIXTURE_DIR / "valid" / "delivery-durable-client-trace.json")
    saved = _step(pending, "persist_local_checkpoint")
    recovered = copy.deepcopy(_step(pending, "recover_checkpoint"))
    recovered["disposition"]["pending_import_id"] = "another-import"
    with pytest.raises(AssertionError):
        _check_recovered_checkpoint(saved, recovered, expected_local_entry_id="entry-one", pending_records=_pending_records(pending))


@pytest.mark.parametrize("damage", ["none", "missing", "wrong_source", "empty_events"])
async def test_restart_ack_resolves_actual_durable_pending_store(hass, monkeypatch, damage):
    from unittest.mock import AsyncMock
    from custom_components.daylight_calendar_import.storage import PendingImportStore

    trace = _read(FIXTURE_DIR / "valid" / "delivery-ack-retry-after-cleanup.json")
    recovered = _step(trace, "recover_checkpoint")
    snapshot = trace["local_store_after_restart"]
    if damage == "missing":
        snapshot["items"] = []
    elif damage == "wrong_source":
        snapshot["items"][0]["source_fingerprint"] = "v1:source:" + "0" * 64
    elif damage == "empty_events":
        snapshot["items"][0]["events"] = []
    store = PendingImportStore(hass)
    monkeypatch.setattr(store._store, "async_load", AsyncMock(return_value=snapshot))
    await store.async_load()
    # No saved checkpoint or claim is supplied: resolve only the recovered
    # proof and the production store loaded from its independent disk snapshot.
    if damage == "none":
        assert _ack_path_from_recovered_checkpoint(
            recovered, expected_local_entry_id="entry-one", pending_records=store,
        ) == "/v1/sources/delivery_00000000000000000002/ack"
    else:
        with pytest.raises(AssertionError):
            _ack_path_from_recovered_checkpoint(
                recovered, expected_local_entry_id="entry-one", pending_records=store,
            )


@pytest.mark.parametrize("damage", ["empty", "partial", "unrelated", "descriptor_changed", "descriptor_missing", "duplicate_descriptor"])
def test_restart_only_manifest_proves_completeness(damage):
    import copy

    trace = _read(FIXTURE_DIR / "valid" / "delivery-ack-retry-after-cleanup.json")
    recovered = _step(trace, "recover_checkpoint")
    if damage in {"empty", "partial"}:
        recovered["verified_attachments"] = {}
        if damage == "partial":
            recovered["attachment_descriptors"].append({
                **recovered["attachment_descriptors"][0], "id": "second.pdf",
            })
    elif damage == "unrelated":
        recovered["verified_attachments"] = {"other.pdf": "a" * 64}
    elif damage == "descriptor_changed":
        recovered["attachment_descriptors"][0]["filename"] = "Changed.pdf"
    elif damage == "descriptor_missing":
        del recovered["attachment_descriptors"]
    else:
        recovered["attachment_descriptors"].append(copy.deepcopy(recovered["attachment_descriptors"][0]))
    with pytest.raises((AssertionError, KeyError)):
        _ack_path_from_recovered_checkpoint(
            recovered, expected_local_entry_id="entry-one", pending_records=_pending_records(trace),
        )


@pytest.mark.parametrize("step_index", [0, 1])
@pytest.mark.parametrize("field", ["schema_version", "source_id", "source_expires_at", "lease_expires_at", "source"])
def test_claim_replay_rejects_partial_wire_pages(step_index, field, schema_registry):
    schemas, registry = schema_registry
    trace = _read(FIXTURE_DIR / "valid" / "delivery-idempotent-claim-trace.json")
    response = trace["steps"][step_index]["response"]
    if field == "schema_version":
        del response[field]
    else:
        del response["deliveries"][0][field]
    with pytest.raises(ValidationError):
        _check_claim_idempotency_trace(trace, schemas, registry)
