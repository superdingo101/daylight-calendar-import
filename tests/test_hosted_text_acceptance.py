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

    with pytest.raises(HostedTextAcceptanceError, match="already accepted"):
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
