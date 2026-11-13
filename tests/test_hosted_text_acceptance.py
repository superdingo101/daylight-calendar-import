"""Text delivery is accepted only under the original hosted authority."""
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.daylight_calendar_import.hosted_text_acceptance import (
    HostedTextAcceptanceError, async_accept_hosted_text,
)
from custom_components.daylight_calendar_import.hosted_transport import HostedServiceBinding
from custom_components.daylight_calendar_import.hosted_credentials import CredentialCheckpoint
from custom_components.daylight_calendar_import.storage import PendingImportStorageError
from tests.test_hosted_ack_dispatch import checkpoint
from tests.test_hosted_source_transport import page as base_page
from tests.test_storage import FakeStoreBackend, draft, make_store

SERVICE = HostedServiceBinding("https://api.example.test", "https://account.example.test")


def page():
    result = base_page()
    delivery = result["deliveries"][0]
    now = datetime.now(UTC)
    delivery["lease_expires_at"] = (now + timedelta(seconds=90)).isoformat()
    delivery["source_expires_at"] = (now + timedelta(days=1)).isoformat()
    return result


async def run(store, creds, delivery, *, parse=None, claim=None, service=SERVICE):
    async def default_parse(source):
        return SimpleNamespace(events=[draft()], event_assumptions=(), warnings=[]), None

    def route(source):
        return SimpleNamespace(parser_source=source, calendar_entity="calendar.test",
                               requires_confirmation=False, warnings=[])

    return await async_accept_hosted_text(
        store=store, credentials=creds, claim_checkpoint=claim or creds.snapshot,
        service_binding=service, local_config_entry_id="entry", delivery=delivery,
        route_source=route, parse_source=parse or default_parse,
    )


async def test_real_atomic_store_and_duplicate_never_reparse(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    creds = SimpleNamespace(snapshot=checkpoint())
    delivery = page()["deliveries"][0]
    result = await run(store, creds, delivery)
    assert result.pending is not None
    assert len(backend.saved) == 1
    entry = backend.saved[-1]["hosted_delivery_journal"][0]
    assert entry["public_source_id"] == delivery["source_id"]
    assert entry["account_id"] == creds.snapshot.current.account_id
    restarted = make_store(monkeypatch, FakeStoreBackend(load_result=backend.saved[-1]))
    await restarted.async_load()

    async def never_parse(_source):
        raise AssertionError("re-delivery must not parse")

    with pytest.raises(HostedTextAcceptanceError, match=r"^hosted delivery already accepted$"):
        await run(restarted, creds, delivery, parse=never_parse)


@pytest.mark.parametrize("change", [
    lambda d: d["source"].update(attachments=[{"id": "a", "media_type": "image/png",
                                               "size_bytes": 1, "sha256": "a" * 64}]),
    lambda d: d["source"].update(text="   "),
    lambda d: d.update(source_id="short"),
    lambda d: d.update(lease_expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat()),
])
async def test_invalid_and_expired_never_parse_or_save(monkeypatch, change):
    store = make_store(monkeypatch, FakeStoreBackend())
    delivery = deepcopy(page()["deliveries"][0])
    change(delivery)

    async def never_parse(_source):
        raise AssertionError("invalid input reached parser")

    with pytest.raises(HostedTextAcceptanceError):
        await run(store, SimpleNamespace(snapshot=checkpoint()), delivery, parse=never_parse)
    assert not store.hosted_ack_journal()


async def test_credential_change_before_commit_blocks_ack_obligation(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    creds = SimpleNamespace(snapshot=checkpoint())

    async def parse(_source):
        creds.snapshot = replace(creds.snapshot, revision=1)
        return SimpleNamespace(events=[], event_assumptions=(), warnings=[]), None

    with pytest.raises(HostedTextAcceptanceError, match="authority unavailable"):
        await run(store, creds, page()["deliveries"][0], parse=parse)
    assert not store.hosted_ack_journal()


@pytest.mark.parametrize("mode", ["unbound", "disconnected", "expired", "rotation", "binding"])
async def test_unavailable_authority_cannot_parse(monkeypatch, mode):
    store = make_store(monkeypatch, FakeStoreBackend())
    cp = checkpoint()
    service = SERVICE
    if mode == "unbound":
        cp = CredentialCheckpoint()
    elif mode == "disconnected":
        cp = replace(cp, local_disconnect="unconfirmed")
    elif mode == "expired":
        cp = replace(cp, current=replace(cp.current, expires_at=(datetime.now(UTC) - timedelta(days=1)).isoformat()))
    elif mode == "rotation":
        cp = replace(cp, rotation_not_before=(datetime.now(UTC) + timedelta(days=1)).isoformat())
    else:
        service = HostedServiceBinding("https://other.example.test", "https://account.example.test")

    async def never_parse(_source):
        raise AssertionError("unauthorized source reached parser")

    with pytest.raises(HostedTextAcceptanceError, match="authority unavailable"):
        await run(store, SimpleNamespace(snapshot=cp), page()["deliveries"][0], parse=never_parse,
                  service=service)
    assert not store.hosted_ack_journal()


async def test_parser_guard_is_called_at_atomic_commit(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    calls = []

    async def parse(_source):
        return SimpleNamespace(events=[], event_assumptions=(), warnings=[]), lambda: calls.append(1)

    await run(store, SimpleNamespace(snapshot=checkpoint()), page()["deliveries"][0], parse=parse)
    assert calls == [1]
    assert store.hosted_ack_journal()[0].acceptance_disposition == "no_events"


async def test_rfc3339_lowercase_utc_is_accepted(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    delivery = page()["deliveries"][0]
    delivery["lease_expires_at"] = delivery["lease_expires_at"].replace("+00:00", "z")
    delivery["source_expires_at"] = delivery["source_expires_at"].replace("+00:00", "z")
    delivery["source"]["received_at"] = delivery["source"]["received_at"].replace("Z", "z")
    result = await run(store, SimpleNamespace(snapshot=checkpoint()), delivery)
    assert result.pending is not None
    assert store.hosted_ack_journal()[0].evidence.source_expires_at.endswith("Z")


async def test_parser_failure_or_storage_error_cannot_ack(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    creds = SimpleNamespace(snapshot=checkpoint())

    async def fail(_source):
        raise RuntimeError("parser failed")

    with pytest.raises(RuntimeError):
        await run(store, creds, page()["deliveries"][0], parse=fail)
    assert not store.hosted_ack_journal()
    backend.save_error = OSError("failed write")
    with pytest.raises(OSError):
        await run(store, creds, page()["deliveries"][0])
    with pytest.raises(PendingImportStorageError):
        store.hosted_ack_journal()


async def test_hosted_text_is_routed_and_persisted_without_losing_evidence(monkeypatch):
    """The real review store must retain every routing and verification input."""
    import json
    from hashlib import sha256
    from uuid import UUID

    from custom_components.daylight_calendar_import.sources import SourceDocument, SourceKind

    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    creds = SimpleNamespace(snapshot=checkpoint())
    delivery = page()["deliveries"][0]
    original_source = delivery["source"]
    # Insertion order deliberately differs from canonical key order, and the
    # non-ASCII text makes escaping affect the evidence digest.
    delivery["source"] = {
        "metadata": {"sender": "école@example.test"},
        "title": "Café activities",
        "text": "Réunion at the library",
        "received_at": original_source["received_at"],
        "kind": "email",
        "attachments": [],
    }
    source = delivery["source"]
    expected_digest = sha256(json.dumps(
        source, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()
    expected_source_id = f"hosted:entry:{delivery['delivery_id']}"
    parser_input = object()
    routed_documents = []
    received_parser_inputs = []
    add_arguments = []
    original_add = store.async_add

    def route(document):
        assert isinstance(document, SourceDocument)
        UUID(document.id)
        assert document.kind is SourceKind.EMAIL
        assert document.received_at == datetime.fromisoformat(source["received_at"])
        assert document.text == source["text"]
        assert document.title == source["title"]
        assert document.metadata == source["metadata"]
        assert document.metadata is not source["metadata"]
        assert document.upstream_source_id == expected_source_id
        routed_documents.append(document)
        return SimpleNamespace(
            parser_source=parser_input,
            calendar_entity="calendar.hosted",
            requires_confirmation=True,
            warnings=["Route requires review"],
        )

    async def parse(value):
        received_parser_inputs.append(value)
        assert value is parser_input
        return SimpleNamespace(
            events=[draft()],
            event_assumptions=((),),
            warnings=["Parser requires review"],
        ), None

    async def capture_add(**kwargs):
        add_arguments.append(kwargs)
        return await original_add(**kwargs)

    monkeypatch.setattr(store, "async_add", capture_add)
    result = await async_accept_hosted_text(
        store=store, credentials=creds, claim_checkpoint=creds.snapshot,
        service_binding=SERVICE, local_config_entry_id="entry", delivery=delivery,
        route_source=route, parse_source=parse,
    )
    assert len(routed_documents) == 1
    assert received_parser_inputs == [parser_input]
    assert len(add_arguments) == 1
    stored = add_arguments[0]
    assert stored["source_text"] == source["text"]
    assert stored["events"] == [draft()]
    assert stored["event_assumptions"] == ((),)
    assert stored["source_id"] == expected_source_id
    assert stored["calendar_entity"] == "calendar.hosted"
    assert stored["routing_unresolved"] is True
    assert stored["source_kind"] == "email"
    assert stored["source_title"] == "Café activities"
    assert stored["source_sender"] == "école@example.test"
    assert stored["warnings"] == ["Parser requires review", "Route requires review"]
    assert callable(stored["acceptance_guard"])
    evidence = stored["hosted_evidence"]
    assert evidence.source_evidence_sha256 == expected_digest
    assert evidence.attachment_manifest_sha256 == sha256(b'{"attachments":[]}').hexdigest()
    assert evidence.delivery_id == delivery["delivery_id"]
    assert evidence.lease_token == delivery["lease_token"]
    assert evidence.public_source_id == delivery["source_id"]
    assert evidence.local_config_entry_id == "entry"
    assert evidence.account_id == creds.snapshot.current.account_id
    assert evidence.installation_id == creds.snapshot.current.installation_id
    assert evidence.source_expires_at == delivery["source_expires_at"]
    assert result.pending is not None
    assert result.pending.source_text == source["text"]
    assert result.pending.source_kind == "email"
    assert result.pending.source_title == "Café activities"
    assert result.pending.source_sender == "école@example.test"
    assert result.pending.warnings == ("Parser requires review", "Route requires review")
    assert result.pending.events[0].calendar_entity == "calendar.hosted"
    assert result.pending.events[0].routing_unresolved is True
    assert backend.saved[-1]["hosted_delivery_journal"][0]["source_evidence_sha256"] == expected_digest


async def test_invalid_delivery_has_exact_sanitized_error(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    delivery = page()["deliveries"][0]
    delivery["source"]["text"] = "   "
    with pytest.raises(HostedTextAcceptanceError, match=r"^invalid hosted text delivery$"):
        await run(store, SimpleNamespace(snapshot=checkpoint()), delivery)
    assert not store.hosted_ack_journal()


async def test_authority_fencing_requires_a_credential_even_with_consent(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    cp = replace(checkpoint(), current=None)

    async def should_not_parse(_source):
        pytest.fail("a delivery with no current credential reached the parser")

    with pytest.raises(HostedTextAcceptanceError, match=r"^hosted delivery authority unavailable$"):
        await run(store, SimpleNamespace(snapshot=cp), page()["deliveries"][0],
                  parse=should_not_parse)
    assert not store.hosted_ack_journal()


async def test_reconnect_alone_blocks_delivery(monkeypatch):
    from custom_components.daylight_calendar_import.hosted_credentials import ReconnectIntent
    from tests.test_hosted_ack_dispatch import ACCOUNT, INSTALLATION

    store = make_store(monkeypatch, FakeStoreBackend())
    cp = replace(
        checkpoint(), pairing_verifier="v" * 43,
        reconnect=ReconnectIntent(ACCOUNT, INSTALLATION, "none", "pending-v2-local-only"),
    )

    async def should_not_parse(_source):
        pytest.fail("a reconnecting installation reached the parser")

    with pytest.raises(HostedTextAcceptanceError, match=r"^hosted delivery authority unavailable$"):
        await run(store, SimpleNamespace(snapshot=cp), page()["deliveries"][0],
                  parse=should_not_parse)
    assert not store.hosted_ack_journal()


@pytest.mark.parametrize("boundary", ["credential", "lease"])
async def test_expiration_is_exclusive_at_exact_boundary(monkeypatch, boundary):
    """Equal deadlines are already expired, not still valid."""
    import custom_components.daylight_calendar_import.hosted_text_acceptance as acceptance

    frozen_now = datetime(2026, 11, 12, 21, 0, tzinfo=UTC)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen_now if tz is None else frozen_now.astimezone(tz)

    monkeypatch.setattr(acceptance, "datetime", FrozenDatetime)
    delivery = page()["deliveries"][0]
    delivery["lease_expires_at"] = (frozen_now if boundary == "lease"
                                    else frozen_now + timedelta(seconds=90)).isoformat()
    delivery["source_expires_at"] = (frozen_now + timedelta(days=1)).isoformat()
    cp = checkpoint()
    if boundary == "credential":
        cp = replace(cp, current=replace(
            cp.current, expires_at=frozen_now.isoformat(),
        ))
    store = make_store(monkeypatch, FakeStoreBackend())

    async def should_not_parse(_source):
        pytest.fail("expired authority reached parser")

    with pytest.raises(HostedTextAcceptanceError, match=r"^hosted delivery authority unavailable$"):
        await run(store, SimpleNamespace(snapshot=cp), delivery, parse=should_not_parse)
    assert not store.hosted_ack_journal()
