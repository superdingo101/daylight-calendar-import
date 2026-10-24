# ruff: noqa: F811 -- pytest shared fixture import
"""Service destinations survive restart and never inherit unbound recovery data."""

from dataclasses import asdict, replace
from unittest.mock import AsyncMock

import pytest

from custom_components.daylight_calendar_import.hosted_credentials import (
    CredentialCheckpoint,
    HostedCredentialStore,
    _decode,
)
from custom_components.daylight_calendar_import.hosted_lifecycle import (
    HostedLifecycleController,
)
from custom_components.daylight_calendar_import.hosted_transport import (
    HostedLifecycleTransport,
    HostedServiceBinding,
    HostedTransportError,
)
from tests.test_hosted_lifecycle import CURRENT
from tests.test_hosted_pairing import pairing, start  # noqa: F401 -- shared real-disk fixture

SERVICE = HostedServiceBinding("https://api.example.test", "https://account.example.test")


def test_binding_is_canonical_and_transport_identity_is_read_only():
    transport = HostedLifecycleTransport(
        AsyncMock(), api_origin="https://API.example.test:443/",
        account_origin="https://ACCOUNT.example.test/",
    )
    assert transport.service_binding == SERVICE
    with pytest.raises(AttributeError):
        transport.service_binding = SERVICE
    checkpoint = CredentialCheckpoint(service_binding=SERVICE)
    assert _decode(asdict(checkpoint)) == checkpoint
    legacy = asdict(CredentialCheckpoint())
    del legacy["service_binding"]
    assert _decode(legacy).service_binding is None


@pytest.mark.parametrize("value", [[], "https://api.example.test", 1])
def test_malformed_binding_is_not_recovery_authority(value):
    with pytest.raises(ValueError):
        CredentialCheckpoint(service_binding=value)
    with pytest.raises(ValueError):
        _decode({**asdict(CredentialCheckpoint()), "service_binding": value})


@pytest.mark.parametrize("origin", ["http://api.example.test", "https://api.example.test/path", "https://u:p@api.example.test"])
def test_invalid_binding_destination_is_rejected(origin):
    with pytest.raises(ValueError):
        HostedServiceBinding(origin, SERVICE.account_origin)


async def test_binding_is_saved_with_consent_before_first_request_and_survives_restart(pairing, hass):
    c, store, transport = pairing
    async def creation(*_args):
        recovered = HostedCredentialStore(hass, "pairing")
        checkpoint = await recovered.async_load()
        assert checkpoint.service_binding == SERVICE
        assert checkpoint.processing_consent and checkpoint.pairing_verifier
        return transport.start_pairing.return_value
    transport.start_pairing.side_effect = creation
    await start(c)
    assert store.snapshot.service_binding == SERVICE


@pytest.mark.parametrize("operation", ["async_reconcile", "async_rotate", "async_poll_pairing", "async_disconnect", "async_start_pairing"])
@pytest.mark.parametrize("changed", ["api", "account", "legacy"])
async def test_saved_recovery_never_leaves_for_changed_or_unknown_service(pairing, operation, changed):
    c, store, transport = pairing
    await start(c)
    old = store.snapshot
    if changed == "legacy":
        await store.async_checkpoint(old, replace(old, service_binding=None))
    else:
        transport.service_binding = HostedServiceBinding(
            "https://other.example.test" if changed == "api" else SERVICE.api_origin,
            "https://other.example.test" if changed == "account" else SERVICE.account_origin,
        )
    # Include a bearer for disconnect and recovery, preserving pairing material.
    old = store.snapshot
    await store.async_checkpoint(old, replace(old, current=CURRENT))
    transport.reset_mock()
    if operation == "async_start_pairing":
        # Existing namespaces require separate reconciliation even before service checks.
        assert (await start(c)).state == "reconciliation_required"
    else:
        with pytest.raises(HostedTransportError, match="^service_reconciliation_required$"):
            await getattr(c, operation)()
    assert not transport.mock_calls
    assert store.snapshot.current == CURRENT
    assert store.snapshot.local_disconnect == ("unconfirmed" if operation == "async_disconnect" else "none")


@pytest.mark.parametrize("legacy", ["consent", "verifier"])
async def test_unbound_pairing_intent_cannot_be_relabelled(pairing, legacy):
    c, store, transport = pairing
    old = store.snapshot
    await store.async_checkpoint(old, replace(
        old, processing_consent=True,
        pairing_verifier="v" * 43 if legacy == "verifier" else None,
    ))
    before = store.snapshot
    with pytest.raises(HostedTransportError, match="^service_reconciliation_required$"):
        await start(c)
    assert store.snapshot is before and not transport.mock_calls


async def test_empty_bound_store_cannot_change_services(pairing):
    c, store, transport = pairing
    old = store.snapshot
    await store.async_checkpoint(old, replace(old, service_binding=SERVICE))
    transport.service_binding = HostedServiceBinding("https://other.example.test", SERVICE.account_origin)
    with pytest.raises(HostedTransportError, match="^service_reconciliation_required$"):
        await start(c)
    assert not transport.mock_calls


async def test_legacy_bound_token_requires_explicit_reconciliation(pairing):
    _c, store, transport = pairing
    old = store.snapshot
    await store.async_checkpoint(old, replace(old, processing_consent=True, current=CURRENT))
    with pytest.raises(HostedTransportError, match="service_reconciliation_required"):
        await HostedLifecycleController(store, transport).async_reconcile()
    assert not transport.mock_calls


@pytest.mark.parametrize("field", ["api_origin", "account_origin"])
@pytest.mark.parametrize("value", [None, [], 1, "https://" + "a" * 2041])
def test_service_binding_bounds_both_configured_origins(field, value):
    values = asdict(SERVICE)
    values[field] = value
    with pytest.raises(ValueError, match="^invalid trusted origin$"):
        HostedServiceBinding(**values)


def test_maximum_binding_origin_still_round_trips():
    origin = "https://" + "a" * 2040
    checkpoint = CredentialCheckpoint(service_binding=HostedServiceBinding(origin, origin))
    assert _decode(asdict(checkpoint)) == checkpoint
