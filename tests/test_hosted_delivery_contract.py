"""Executable, cloud-independent H3 hosted-source protocol contract.

These tests validate public schemas/fixtures and normative lease/ACK traces.
They do not pretend to exercise the private cloud server or a live HA adapter.
"""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
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


def _source_evidence_sha256(source: dict) -> str:
    """Canonical complete-source evidence independent of namespaced dedup ID."""
    canonical = json.dumps(
        source, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def _check_checkpoint_evidence(claim: dict, saved: dict, recovered: dict) -> None:
    """A restart cannot trade a durable source identity for verified contents."""
    from custom_components.daylight_calendar_import.dedup import source_fingerprint

    expected = source_fingerprint(
        f"hosted:{claim['local_config_entry_id']}:{claim['delivery_id']}"
    )
    assert saved["source_fingerprint"] == expected
    assert recovered["source_fingerprint"] == expected
    assert saved["source_expires_at"] == claim["source_expires_at"]
    assert saved["lease_token"] == claim["token"]
    assert saved["verified_attachments"] == {
        attachment["id"]: attachment["sha256"]
        for attachment in claim["source"]["attachments"]
    }
    assert saved["attachments_verification_complete"] is True
    assert recovered["source_expires_at"] == claim["source_expires_at"]
    assert recovered["lease_token"] == saved["lease_token"]
    assert recovered["verified_attachments"] == saved["verified_attachments"]
    assert recovered["attachments_verification_complete"] is True
    expected = _source_evidence_sha256(claim["source"])
    assert saved["source_evidence_sha256"] == expected
    assert recovered["source_evidence_sha256"] == expected


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
    _check_checkpoint_evidence(steps[0], steps[3], steps[5])
    assert steps[0]["token"] == steps[-1]["token"]
    assert trace["source_id"] != trace["delivery_id"]


@pytest.mark.parametrize("claim", [
    {"schema_version": 1, "claim_request_id": "ClaimKey_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"},
    {"schema_version": 1, "claim_request_id": "ClaimKey_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890", "limit": 1},
    {"schema_version": 1, "claim_request_id": "ClaimKey_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890", "limit": 50, "cursor": "nextPageAbc123_-"},
])
def test_h3_bounded_claim_request_schema_accepts_valid_json(claim, schema_registry):
    schemas, registry = schema_registry
    _validate("delivery-claim-request.schema.json", claim, schemas, registry)


@pytest.mark.parametrize("claim", [
    {}, {"schema_version": 1}, {"schema_version": 2, "claim_request_id": "ClaimKey_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"}, {"schema_version": 1, "claim_request_id": "ClaimKey_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890", "limit": 0},
    {"schema_version": 1, "claim_request_id": "ClaimKey_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890", "limit": 51}, {"schema_version": 1, "claim_request_id": "ClaimKey_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890", "limit": "20"},
    {"schema_version": 1, "claim_request_id": "ClaimKey_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890", "limit": -1}, {"schema_version": 1, "claim_request_id": "ClaimKey_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890", "cursor": ""},
    {"schema_version": 1, "claim_request_id": "ClaimKey_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890", "cursor": "../other-installation"},
    {"schema_version": 1, "claim_request_id": "ClaimKey_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890", "cursor": "!"},
    {"schema_version": 1, "claim_request_id": "ClaimKey_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890", "cursor": "x" * 1025},
    {"schema_version": 1, "claim_request_id": "ClaimKey_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890", "installation_id": "another-tenant"},
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
    assert operations[0] == "claim"
    assert operations[-1] == "ack"
    assert operations.index("parse_no_events") < operations.index(
        "persist_terminal_no_events"
    )
    assert operations.index("persist_terminal_no_events") < operations.index("ack")
    assert operations.index("persist_terminal_no_events") < operations.index("restart_ha")
    assert operations.index("restart_ha") < operations.index("recover_terminal_no_events")
    assert operations.index("recover_terminal_no_events") < operations.index("ack")
    saved = next(step for step in steps if step["op"] == "persist_terminal_no_events")
    recovered = next(step for step in steps if step["op"] == "recover_terminal_no_events")
    _check_checkpoint_evidence(steps[0], saved, recovered)


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


def test_durable_pending_checkpoint_requires_content_evidence_after_restart():
    import copy

    trace = _read(FIXTURE_DIR / "valid" / "delivery-durable-client-trace.json")
    claim, saved, recovered = trace["steps"][0], trace["steps"][3], trace["steps"][5]
    _check_checkpoint_evidence(claim, saved, recovered)
    changed = copy.deepcopy(trace)
    changed["steps"][0]["source"]["metadata"]["sender"] = "attacker@example.test"
    with pytest.raises(AssertionError):
        _check_checkpoint_evidence(changed["steps"][0], saved, recovered)
    lost = copy.deepcopy(trace)
    del lost["steps"][5]["source_evidence_sha256"]
    with pytest.raises(KeyError):
        _check_checkpoint_evidence(claim, saved, lost["steps"][5])


@pytest.mark.parametrize("raw", [
    '{"schema_version":1,"schema_version":1}',
    '{"schema_version":NaN}',
    '{"schema_version":Infinity}',
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
    claim, saved, recovered = trace["steps"][0], trace["steps"][3], trace["steps"][5]
    _check_checkpoint_evidence(claim, saved, recovered)
    for key, value in [
        ("local_config_entry_id", "entry-two"),
        ("delivery_id", "delivery_00000000000000000099"),
        ("source_expires_at", "2026-10-12T16:00:00Z"),
    ]:
        changed = copy.deepcopy(claim)
        changed[key] = value
        with pytest.raises(AssertionError):
            _check_checkpoint_evidence(changed, saved, recovered)
    changed = copy.deepcopy(recovered)
    changed["source_expires_at"] = "2026-10-12T16:00:00Z"
    with pytest.raises(AssertionError):
        _check_checkpoint_evidence(claim, saved, changed)


@pytest.mark.parametrize("request_id", [
    "", "too-short", "A" * 22 + "\n", "../unsafe", "A" * 129,
])
def test_claim_identity_rejects_missing_weak_and_trailing_newline(request_id, schema_registry):
    schemas, registry = schema_registry
    with pytest.raises(ValidationError):
        _validate(
            "delivery-claim-request.schema.json",
            {"schema_version": 1, "claim_request_id": request_id},
            schemas, registry,
        )


def _check_claim_idempotency_trace(trace: dict) -> None:
    """Retries of a consumed request ID never lease another page."""
    originals = {}
    for step in trace["steps"]:
        request_id = step["request_id"]
        parameters = step["parameters"]
        if step["op"] == "claim":
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


def test_claim_replays_do_not_allocate_second_batch():
    import copy

    trace = _read(FIXTURE_DIR / "valid" / "delivery-idempotent-claim-trace.json")
    _check_claim_idempotency_trace(trace)
    changed_token = copy.deepcopy(trace)
    changed_token["steps"][1]["response"]["deliveries"][0]["lease_token"] = "Z" * 48
    with pytest.raises(AssertionError):
        _check_claim_idempotency_trace(changed_token)
    repeated_after_ack = copy.deepcopy(trace)
    repeated_after_ack["steps"][2]["new_leases_issued"] = True
    with pytest.raises(AssertionError):
        _check_claim_idempotency_trace(repeated_after_ack)
    changed_params = copy.deepcopy(trace)
    changed_params["steps"][3]["parameters"]["limit"] = 20
    with pytest.raises(AssertionError):
        _check_claim_idempotency_trace(changed_params)


def test_lost_ack_response_after_attachment_cleanup_uses_durable_proof():
    import copy

    trace = _read(FIXTURE_DIR / "valid" / "delivery-ack-retry-after-cleanup.json")
    steps = trace["steps"]
    assert [step["op"] for step in steps] == [
        "claim", "download_and_verify", "persist_local_checkpoint",
        "ack_applied", "delete_cloud_source", "recover_checkpoint", "retry_same_ack",
    ]
    claim, saved, recovered = steps[0], steps[2], steps[5]
    _check_checkpoint_evidence(claim, saved, recovered)
    assert steps[6]["token"] == saved["lease_token"]
    assert steps[6]["status"] == 200
    assert steps[6]["acknowledged_at"] == steps[3]["acknowledged_at"]
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
            _check_checkpoint_evidence(claim, saved, corrupt)


def test_ack_response_rejects_ending_line_break(schema_registry):
    schemas, registry = schema_registry
    valid = _read(FIXTURE_DIR / "valid" / "delivery-ack-response.json")
    _validate("delivery-ack-response.schema.json", valid, schemas, registry)
    valid["delivery_id"] += "\n"
    with pytest.raises(ValidationError):
        _validate("delivery-ack-response.schema.json", valid, schemas, registry)
