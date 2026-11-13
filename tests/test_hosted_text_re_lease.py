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


async def setup(monkeypatch, *, modify=None, prior_attachments=False):
    delivery = claim()
    if modify is not None:
        modify(delivery)
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
    if prior_attachments:
        # Model an already-accepted source with attachment evidence. A text-only
        # re-lease must not discard or launder those verified descriptors.
        descriptor = {"id": "scan.png", "media_type": "image/png",
                      "size_bytes": 3, "sha256": "a" * 64}
        verified = {"id": "scan.png", "size_bytes": 3, "sha256": "a" * 64}
        manifest = json.dumps(
            {"attachments": [descriptor]}, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        evidence = replace(
            evidence, attachment_descriptors=(descriptor,),
            verified_attachments=(verified,),
            attachment_manifest_sha256=sha256(manifest).hexdigest(),
        )
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await store.async_add(source_text="hello", events=[], source_id=evidence.source_id,
                          hosted_evidence=evidence)
    return store, backend, SimpleNamespace(snapshot=cp), store.hosted_ack_journal()[0], delivery


async def re_lease(store, creds, entry, delivery, **kwargs):
    return await async_re_lease_hosted_text(
        store=store, credentials=creds, claim_checkpoint=kwargs.get("checkpoint", creds.snapshot),
        service_binding=kwargs.get("service", SERVICE),
        local_config_entry_id=kwargs.get("local", "entry"),
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


async def test_accepts_exact_canonical_unicode_source_without_changing_evidence(monkeypatch):
    """Re-lease hashes the same UTF-8 source bytes as the original acceptance."""
    def modify(delivery):
        delivery["source"]["text"] = "Réunion · café"
        delivery["source"]["metadata"]["sender"] = "école@example.test"

    store, backend, creds, entry, delivery = await setup(monkeypatch, modify=modify)
    before = entry.evidence
    result = await re_lease(store, creds, entry, delivery)
    assert result.evidence.source_evidence_sha256 == before.source_evidence_sha256
    assert result.evidence.source_expires_at == before.source_expires_at
    assert result.evidence.attachment_manifest_sha256 == before.attachment_manifest_sha256
    assert result.evidence.attachment_descriptors == ()
    assert result.evidence.verified_attachments == ()
    assert result.evidence.account_id == before.account_id
    assert result.evidence.installation_id == before.installation_id
    assert result.evidence.public_source_id == before.public_source_id
    assert result.evidence.delivery_id == before.delivery_id
    assert result.evidence.local_config_entry_id == before.local_config_entry_id
    assert result.evidence.lease_token == delivery["lease_token"]
    assert backend.saved[-1]["hosted_delivery_journal"][0]["lease_token"] == delivery["lease_token"]


async def test_lowercase_utc_re_lease_preserves_original_expiry(monkeypatch):
    """RFC3339 lowercase 'z' is normalized to the original durable uppercase 'Z'."""
    def uppercase_expiry(delivery):
        delivery["source_expires_at"] = delivery["source_expires_at"].replace("+00:00", "Z")

    store, _, creds, entry, delivery = await setup(monkeypatch, modify=uppercase_expiry)
    original = entry.evidence.source_expires_at
    assert original.endswith("Z")
    delivery["source_expires_at"] = original[:-1] + "z"
    delivery["lease_expires_at"] = delivery["lease_expires_at"].replace("+00:00", "z")
    result = await re_lease(store, creds, entry, delivery)
    assert result.evidence.source_expires_at == original
    assert result.evidence.lease_token == delivery["lease_token"]
    assert store.hosted_ack_journal()[0] == result


@pytest.mark.parametrize("invalid", ["blank_text", "bad_token", "attachment"])
async def test_invalid_claim_has_stable_sanitized_error(monkeypatch, invalid):
    store, _, creds, entry, delivery = await setup(monkeypatch)
    if invalid == "blank_text":
        delivery["source"]["text"] = "   "
    elif invalid == "bad_token":
        delivery["lease_token"] = "!"
    else:
        delivery["source"]["attachments"] = [
            {"id": "scan.png", "media_type": "image/png",
             "size_bytes": 3, "sha256": "a" * 64},
        ]
    with pytest.raises(HostedTextReLeaseError, match=r"^invalid hosted text delivery$"):
        await re_lease(store, creds, entry, delivery)
    assert store.hosted_ack_journal() == (entry,)


@pytest.mark.parametrize("failure", [
    "no_credential", "namespace", "disconnect", "reconnect",
    "candidate", "rotation", "binding", "wrong_service_type",
])
async def test_re_lease_requires_every_independent_authority_check(monkeypatch, failure):
    from custom_components.daylight_calendar_import.hosted_credentials import (
        CandidateCredential, ReconnectIntent,
    )
    from custom_components.daylight_calendar_import.hosted_transport import HostedServiceBinding

    store, _, creds, entry, delivery = await setup(monkeypatch)
    cp = creds.snapshot
    service = SERVICE
    if failure == "no_credential":
        cp = replace(cp, current=None)
    elif failure == "namespace":
        cp = replace(cp, current=replace(cp.current, account_id="b" * 20))
    elif failure == "disconnect":
        cp = replace(cp, local_disconnect="unconfirmed")
    elif failure == "reconnect":
        cp = replace(
            cp, pairing_verifier="v" * 43,
            reconnect=ReconnectIntent(cp.current.account_id, cp.current.installation_id,
                                      "none", "pending-v2-local-only"),
        )
    elif failure == "candidate":
        deadline = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        cp = replace(
            cp, candidate=CandidateCredential(
                cp.current.installation_id, cp.current.token, deadline, deadline,
            ),
        )
    elif failure == "rotation":
        cp = replace(
            cp, rotation_not_before=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        )
    elif failure == "binding":
        cp = replace(
            cp, service_binding=HostedServiceBinding(
                "https://other.example.test", "https://account.example.test",
            ),
        )
    else:
        class EqualButUntrustedService:
            def __eq__(self, other):
                return other == SERVICE

        service = EqualButUntrustedService()
        assert cp.service_binding == service
    creds.snapshot = cp
    with pytest.raises(
        HostedTextReLeaseError, match=r"^hosted delivery authority unavailable$",
    ):
        await re_lease(store, creds, entry, delivery, checkpoint=cp, service=service)
    assert store.hosted_ack_journal() == (entry,)


@pytest.mark.parametrize("boundary", ["credential", "lease", "source"])
async def test_re_lease_rejects_exact_expiration_boundary(monkeypatch, boundary):
    """'Not after' is not sufficient: equality with now must be rejected."""
    import custom_components.daylight_calendar_import.hosted_text_re_lease as module

    store, _, creds, entry, delivery = await setup(monkeypatch)
    cp = creds.snapshot
    if boundary == "source":
        boundary_time = datetime.fromisoformat(delivery["source_expires_at"])
        # The claimed source is unexpired at validation and expires precisely
        # before authority is checked, without changing its persisted evidence.
        validation_time = boundary_time - timedelta(seconds=1)
    else:
        boundary_time = datetime.now(UTC) + timedelta(seconds=10)
        validation_time = boundary_time
    if boundary == "credential":
        cp = replace(cp, current=replace(
            cp.current, expires_at=boundary_time.isoformat(),
        ))
    elif boundary == "lease":
        delivery["lease_expires_at"] = boundary_time.isoformat()

    class BoundaryClock(datetime):
        calls = 0

        @classmethod
        def now(cls, tz=None):
            cls.calls += 1
            value = validation_time if cls.calls == 1 else boundary_time
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(module, "datetime", BoundaryClock)
    creds.snapshot = cp
    with pytest.raises(
        HostedTextReLeaseError, match=r"^hosted delivery authority unavailable$",
    ):
        await re_lease(store, creds, entry, delivery, checkpoint=cp)
    assert store.hosted_ack_journal() == (entry,)


async def test_text_only_re_lease_cannot_launder_existing_attachment_evidence(monkeypatch):
    store, _, creds, entry, delivery = await setup(monkeypatch, prior_attachments=True)
    assert entry.evidence.attachment_descriptors
    assert entry.evidence.verified_attachments
    with pytest.raises(ValueError, match=r"^hosted delivery evidence changed$"):
        await re_lease(store, creds, entry, delivery)
    assert store.hosted_ack_journal() == (entry,)


async def test_one_shot_validation_receives_delivery_and_single_item_limit(monkeypatch):
    """Validation must inspect actual claim data, with one-shot page bounds."""
    import custom_components.daylight_calendar_import.hosted_text_re_lease as module

    store, _, creds, entry, delivery = await setup(monkeypatch)
    original_page = module._page
    original_strict = module._strict_strings
    strict_seen = []
    page_seen = []

    def strict(value):
        assert isinstance(value, dict)
        assert value == delivery
        strict_seen.append(True)
        return original_strict(value)

    def one_page(value, limit, now):
        assert limit == 1
        assert value == {"schema_version": 1, "deliveries": [delivery]}
        page_seen.append(True)
        return original_page(value, limit, now)

    monkeypatch.setattr(module, "_strict_strings", strict)
    monkeypatch.setattr(module, "_page", one_page)
    result = await re_lease(store, creds, entry, delivery)
    assert strict_seen == [True]
    assert page_seen == [True]
    assert result.evidence.lease_token == delivery["lease_token"]
