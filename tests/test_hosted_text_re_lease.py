"""A replacement lease is persisted only for the original accepted source."""
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest

from custom_components.daylight_calendar_import.hosted_delivery_journal import HostedDeliveryEvidence
from custom_components.daylight_calendar_import.hosted_text_re_lease import (
    HostedTextReLeaseError, async_re_lease_hosted_text,
)
from tests.test_hosted_ack_dispatch import SERVICE, checkpoint
from tests.test_hosted_source_transport import page
from tests.test_storage import FakeStoreBackend, make_store


def claim():
    result = deepcopy(page()["deliveries"][0])
    now = datetime.now(UTC)
    result["lease_expires_at"] = (now + timedelta(seconds=60)).isoformat()
    result["source_expires_at"] = (now + timedelta(days=1)).isoformat()
    return result


async def setup(monkeypatch):
    delivery = claim()
    cp = checkpoint()
    canonical = json.dumps(delivery["source"], ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False).encode("utf-8")
    evidence = HostedDeliveryEvidence(
        local_config_entry_id="entry", delivery_id=delivery["delivery_id"],
        lease_token="old-token", source_expires_at=delivery["source_expires_at"],
        source_evidence_sha256=sha256(canonical).hexdigest(),
        attachment_manifest_sha256=sha256(b'{"attachments":[]}').hexdigest(),
        account_id=cp.current.account_id, installation_id=cp.current.installation_id,
        public_source_id=delivery["source_id"],
    )
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_add(source_text="hello", events=[], source_id=evidence.source_id,
                          hosted_evidence=evidence)
    return store, backend, SimpleNamespace(snapshot=cp), store.hosted_ack_journal()[0], delivery


async def re_lease(store, creds, entry, delivery, **kwargs):
    return await async_re_lease_hosted_text(
        store=store, credentials=creds, claim_checkpoint=kwargs.get("checkpoint", creds.snapshot),
        service_binding=SERVICE, local_config_entry_id=kwargs.get("local", "entry"),
        entry=entry, delivery=delivery,
    )


async def test_re_lease_persists_new_token_across_restart(monkeypatch):
    store, backend, creds, entry, delivery = await setup(monkeypatch)
    result = await re_lease(store, creds, entry, delivery)
    assert result.evidence.lease_token == delivery["lease_token"]
    assert result.source_fingerprint == entry.source_fingerprint
    restarted = make_store(monkeypatch, FakeStoreBackend(load_result=backend.saved[-1]))
    await restarted.async_load()
    assert restarted.hosted_ack_journal()[0] == result
    with pytest.raises(ValueError, match="stale"):
        await re_lease(store, creds, entry, delivery)


@pytest.mark.parametrize("field", ["source", "source_id", "delivery_id", "source_expires_at"])
async def test_changed_evidence_does_not_replace_token(monkeypatch, field):
    store, _, creds, entry, delivery = await setup(monkeypatch)
    if field == "source":
        delivery["source"]["text"] = "changed private text"
    elif field in {"source_id", "delivery_id"}:
        delivery[field] = "x" * 32
    else:
        delivery[field] = (datetime.now(UTC) + timedelta(hours=12)).isoformat()
    with pytest.raises(ValueError):
        await re_lease(store, creds, entry, delivery)
    assert store.hosted_ack_journal()[0] == entry


@pytest.mark.parametrize("kind", ["expired", "attachment", "malformed", "local", "credentials"])
async def test_unusable_claim_never_updates_journal(monkeypatch, kind):
    store, _, creds, entry, delivery = await setup(monkeypatch)
    if kind == "expired":
        delivery["lease_expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    elif kind == "attachment":
        delivery["source"]["attachments"] = [{"id": "x", "media_type": "image/png",
                                              "size_bytes": 1, "sha256": "a" * 64}]
    elif kind == "malformed":
        delivery["lease_token"] = "short"
    old = creds.snapshot
    if kind == "credentials":
        creds.snapshot = replace(old, revision=1)
    with pytest.raises(HostedTextReLeaseError):
        await re_lease(store, creds, entry, delivery,
                       local="wrong" if kind == "local" else "entry", checkpoint=old)
    assert store.hosted_ack_journal()[0] == entry


async def test_authority_change_during_save_never_returns_success(monkeypatch):
    store, _, creds, entry, delivery = await setup(monkeypatch)
    save = store.async_reconcile_hosted_ack

    async def change_authority(**kwargs):
        result = await save(**kwargs)
        creds.snapshot = replace(creds.snapshot, revision=1)
        return result

    monkeypatch.setattr(store, "async_reconcile_hosted_ack", change_authority)
    with pytest.raises(HostedTextReLeaseError, match="authority unavailable"):
        await re_lease(store, creds, entry, delivery)
    # The uncertain caller result cannot be used to send an ACK. A future
    # reconciliation must inspect the newly durable journal token.
    assert store.hosted_ack_journal()[0].evidence.lease_token == delivery["lease_token"]


async def test_failed_store_does_not_report_success(monkeypatch):
    store, backend, creds, entry, delivery = await setup(monkeypatch)
    backend.save_error = OSError("private storage details")
    with pytest.raises(OSError):
        await re_lease(store, creds, entry, delivery)
