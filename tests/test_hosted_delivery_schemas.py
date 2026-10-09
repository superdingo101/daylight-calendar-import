"""Public wire examples and semantic validators; no server or journal implementation."""

from __future__ import annotations

from datetime import datetime, timedelta
from hashlib import sha256
import json
import math
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

from custom_components.daylight_calendar_import.dedup import source_fingerprint
from custom_components.daylight_calendar_import.sources import (
    SourceAttachment, SourceDocument, SourceKind,
)

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


def test_h3_sources_are_compatible_with_public_local_source_contract():
    """Wire metadata maps into existing HA domain types; binary content is staged locally."""
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


def test_ack_response_rejects_ending_line_break(schema_registry):
    schemas, registry = schema_registry
    valid = _read(FIXTURE_DIR / "valid" / "delivery-ack-response.json")
    _validate("delivery-ack-response.schema.json", valid, schemas, registry)
    valid["delivery_id"] += "\n"
    with pytest.raises(ValidationError):
        _validate("delivery-ack-response.schema.json", valid, schemas, registry)


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


@pytest.mark.parametrize('limit', [1, 20, 50])
def test_bounded_claims(limit, schema_registry):
    schemas, registry = schema_registry
    _validate('delivery-claim-request.schema.json', {'schema_version': 1}, schemas, registry)
    _validate('delivery-claim-request.schema.json', {'schema_version': 1, 'limit': limit}, schemas, registry)
    _validate('delivery-page.schema.json', {'schema_version': 1, 'deliveries': []}, schemas, registry)


@pytest.mark.parametrize('extra', [
    {'limit': 0}, {'limit': 51}, {'limit': True}, {'limit': '20'},
    {'cursor': 'opaque'}, {'claim_request_id': 'old-protocol'},
    {'claim_request_expires_at': '2026-10-08T16:00:00Z'},
    {'installation_id': 'another-tenant'},
])
def test_claim_rejects_invalid_and_superseded_fields(extra, schema_registry):
    schemas, registry = schema_registry
    with pytest.raises(ValidationError):
        _validate('delivery-claim-request.schema.json', {'schema_version': 1, **extra}, schemas, registry)


@pytest.mark.parametrize('limit', [1, 20, 50])
def test_response_enforces_requested_limit(limit):
    from copy import deepcopy
    page = _read(FIXTURE_DIR / 'valid/delivery-page-text.json')
    template = page['deliveries'][0]
    page['deliveries'] = [dict(deepcopy(template), delivery_id=f'delivery_{i:024}', source_id=f'source_{i:024}') for i in range(limit)]
    _validate_delivery_page_semantics(page, request={'limit': limit})
    page['deliveries'].append(dict(deepcopy(template), delivery_id='delivery_extra', source_id='source_extra'))
    with pytest.raises(ValueError, match='requested limit'):
        _validate_delivery_page_semantics(page, request={'limit': limit})
