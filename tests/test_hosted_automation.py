"""Durable scheduling budgets survive restart without acquiring new authority."""

from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from custom_components.daylight_calendar_import.hosted_automation import (
    HostedAutomationPolicy,
)
from custom_components.daylight_calendar_import.hosted_credentials import (
    AutomationAttempt,
    AutomationState,
    CredentialCheckpoint,
    HostedCredentialStore,
    _decode,
    _ImmediatePrivateStore,
)
from custom_components.daylight_calendar_import.hosted_lifecycle import (
    HostedConnectionResult,
    HostedLifecycleController,
)
from custom_components.daylight_calendar_import.hosted_transport import (
    HostedServiceBinding,
    HostedTransportError,
)
from tests.test_hosted_lifecycle import (
    _REAL_LOAD,
    _REAL_WRITE,
    CANDIDATE,
    CURRENT,
    status,
)

SERVICE = HostedServiceBinding("https://api.example.test", "https://account.example.test")
RECOVER = HostedConnectionResult("recovery_required")
ACTIVE = HostedConnectionResult("connected", "current", CURRENT.expires_at)
NEAR = replace(ACTIVE, credential_expires_at="2026-10-30T00:00:00Z")


@pytest.fixture
async def automatic(hass, tmp_path, monkeypatch, freezer):
    freezer.move_to("2026-10-24T00:00:00Z")
    hass.config.config_dir = str(tmp_path)
    monkeypatch.setattr(_ImmediatePrivateStore, "_async_load", _REAL_LOAD)
    monkeypatch.setattr(_ImmediatePrivateStore, "_async_write_data", _REAL_WRITE)
    monkeypatch.setattr("custom_components.daylight_calendar_import.hosted_automation.secrets.randbelow", lambda _: 0)
    store = HostedCredentialStore(hass, "automatic")
    old = await store.async_load()
    await store.async_checkpoint(old, replace(old, processing_consent=True, current=CURRENT, service_binding=SERVICE))
    controller = AsyncMock()
    controller.async_reconcile.return_value = ACTIVE
    controller.async_rotate.return_value = HostedConnectionResult("candidate_saved")
    controller.async_poll_pairing.return_value = HostedConnectionResult("pairing_pending")
    return HostedAutomationPolicy(store, controller, SERVICE), store, controller


async def reload(hass):
    store = HostedCredentialStore(hass, "automatic")
    await store.async_load()
    return store


async def test_reserved_status_budget_precedes_network_and_restart_throttles(automatic, hass):
    policy, _store, controller = automatic
    async def check():
        disk = (await reload(hass)).snapshot
        assert disk.automation.status.attempts == 1
        assert datetime.fromisoformat(disk.automation.status.not_before) > datetime.now(UTC)
        return ACTIVE
    controller.async_reconcile.side_effect = check
    result = await policy.async_step(RECOVER)
    assert result == ACTIVE
    restarted = await reload(hass)
    record = restarted.snapshot.automation.status
    assert record.attempts == 0
    assert datetime.fromisoformat(record.not_before) == datetime.now(UTC) + timedelta(minutes=5)
    fresh = HostedAutomationPolicy(restarted, controller, SERVICE)
    assert await fresh.async_step(RECOVER) is None
    assert fresh.plan(ACTIVE).due_at == datetime(2026, 11, 24, tzinfo=UTC)
    assert CURRENT.token not in repr(fresh.plan(ACTIVE))


async def test_retry_after_is_saved_and_honored_across_restart(automatic, hass, freezer):
    policy, _store, controller = automatic
    controller.async_reconcile.side_effect = HostedTransportError("rate_limited", status=429, retryable=True, retry_after=300)
    with pytest.raises(HostedTransportError):
        await policy.async_step(RECOVER)
    restarted = await reload(hass)
    record = restarted.snapshot.automation.status
    assert record.attempts == 1
    assert datetime.fromisoformat(record.not_before) == datetime.now(UTC) + timedelta(seconds=301)
    fresh = HostedAutomationPolicy(restarted, controller, SERVICE)
    assert await fresh.async_step(RECOVER) is None
    freezer.tick(301)
    with pytest.raises(HostedTransportError):
        await fresh.async_step(RECOVER)
    assert restarted.snapshot.automation.status.attempts == 2


async def test_retry_limit_cannot_be_reset_by_restart_but_verified_repair_can(automatic, hass, freezer):
    policy, store, controller = automatic
    controller.async_reconcile.side_effect = HostedTransportError("service_unavailable", retryable=True)
    for number in range(6):
        with pytest.raises(HostedTransportError):
            await policy.async_step(RECOVER)
        assert store.snapshot.automation.status.attempts == number + 1
        freezer.tick(400)
        store = await reload(hass)
        policy = HostedAutomationPolicy(store, controller, SERVICE)
    assert (await policy.async_step(RECOVER)).state == "automatic_attention_required"
    assert controller.async_reconcile.await_count == 6
    await policy.async_note_verified(ACTIVE)
    assert store.snapshot.automation.status.attempts == 0
    assert await policy.async_step(RECOVER) is None


async def test_nonretryable_error_stops_without_replaying(automatic):
    policy, store, controller = automatic
    controller.async_reconcile.side_effect = HostedTransportError("identity_mismatch")
    with pytest.raises(HostedTransportError):
        await policy.async_step(RECOVER)
    assert store.snapshot.automation.status.attempts == 6
    assert policy.plan(RECOVER).attention
    assert controller.async_reconcile.await_count == 1


async def test_uncertain_rotation_never_issues_automatically_again(automatic, hass, freezer):
    _policy, store, _mock = automatic
    transport = AsyncMock()
    transport.service_binding = SERVICE
    transport.installation_status.return_value = status(credential_expires_at=NEAR.credential_expires_at)
    transport.rotate.side_effect = HostedTransportError("service_unavailable", retryable=True)
    controller = HostedLifecycleController(store, transport)
    policy = HostedAutomationPolicy(store, controller, SERVICE)
    with pytest.raises(HostedTransportError):
        await policy.async_step(NEAR)
    assert store.snapshot.rotation_not_before is not None
    freezer.tick(1000)
    restarted = await reload(hass)
    policy = HostedAutomationPolicy(restarted, HostedLifecycleController(restarted, transport), SERVICE)
    result = await policy.async_step(RECOVER)
    assert result.state == "connected"
    assert (await policy.async_step(result)).state == "automatic_attention_required"
    transport.rotate.assert_awaited_once()
    assert restarted.snapshot.current == CURRENT


async def test_phase_changes_do_not_reset_independent_rotation_budget(automatic):
    policy, store, controller = automatic
    controller.async_rotate.side_effect = HostedTransportError("service_unavailable", retryable=True)
    with pytest.raises(HostedTransportError):
        await policy.async_step(NEAR)
    await policy.async_note_verified(ACTIVE)
    assert store.snapshot.automation.rotation.attempts == 1
    assert store.snapshot.automation.status.attempts == 0


@pytest.mark.parametrize("changes,state", [
    ({"processing_consent": False, "current": None}, RECOVER),
    ({"local_disconnect": "unconfirmed"}, RECOVER),
    ({"service_binding": None}, RECOVER),
    ({"current": None}, RECOVER),
    ({}, HostedConnectionResult("repair_required")),
    ({}, HostedConnectionResult("connected", "overlap", CURRENT.expires_at)),
])
async def test_no_automatic_work_without_eligible_saved_authority(automatic, changes, state):
    policy, store, controller = automatic
    old = store.snapshot
    await store.async_checkpoint(old, replace(old, **changes))
    assert policy.plan(state) is None and await policy.async_step(state) is None
    await policy.async_note_verified(state)
    assert not controller.mock_calls


async def test_candidate_budget_is_separate_and_promotion_is_not_authorized_by_metadata(automatic):
    policy, store, controller = automatic
    old = store.snapshot
    await store.async_checkpoint(old, replace(old, candidate=CANDIDATE))
    plan = policy.plan(RECOVER)
    assert plan.phase == "candidate" and plan.limit == 6
    controller.async_reconcile.return_value = HostedConnectionResult("previous_retained", "current", CURRENT.expires_at)
    assert (await policy.async_step(RECOVER)).state == "previous_retained"
    assert store.snapshot.candidate == CANDIDATE
    assert store.snapshot.automation.candidate.attempts == 1


async def test_expired_current_status_requires_revalidation_instead_of_rotation(automatic):
    policy, _store, controller = automatic
    expired = replace(ACTIVE, credential_expires_at="2026-10-23T00:00:00Z")
    assert policy.plan(expired).phase == "status"
    await policy.async_step(expired)
    controller.async_rotate.assert_not_called()


async def test_slow_reservation_does_not_start_work(automatic, freezer, monkeypatch):
    policy, store, controller = automatic
    original = store.async_checkpoint
    async def slow(*args):
        saved = await original(*args)
        freezer.tick(16)
        return saved
    monkeypatch.setattr(store, "async_checkpoint", slow)
    assert await policy.async_step(RECOVER) is None
    assert not controller.mock_calls


@pytest.mark.parametrize("attempts,expected", [(0, False), (32, True)])
async def test_pairing_has_immutable_expiry_and_total_budget(automatic, attempts, expected):
    from custom_components.daylight_calendar_import.hosted_credentials import (
        PairingAttempt,
    )
    policy, store, controller = automatic
    old = store.snapshot
    attempt = PairingAttempt("pairing_1234567890", "2026-10-24T00:10:00Z", 5, "2026-10-24T00:00:00Z")
    await store.async_checkpoint(old, replace(old, current=None, pairing_verifier="v" * 43, pairing=attempt))
    plan = policy.plan(RECOVER)
    old = store.snapshot
    record = AutomationAttempt(plan.scope, attempts, "2026-10-24T00:00:00Z")
    await store.async_checkpoint(old, replace(old, automation=AutomationState(pairing=record)))
    assert policy.plan(RECOVER).attention is expected
    result = await policy.async_step(RECOVER)
    assert result.state == ("automatic_attention_required" if expected else "pairing_pending")
    assert controller.async_poll_pairing.await_count == (0 if expected else 1)


@pytest.mark.parametrize("kind", ["exchanged", "expired"])
async def test_pairing_exchange_and_creation_are_never_replayed(automatic, kind):
    from custom_components.daylight_calendar_import.hosted_credentials import (
        PairingAttempt,
    )
    policy, store, controller = automatic
    old = store.snapshot
    attempt = PairingAttempt("pairing_1234567890", "2026-10-24T00:10:00Z" if kind == "exchanged" else "2026-10-23T00:00:00Z", 5, "2026-10-24T00:00:00Z", kind == "exchanged")
    await store.async_checkpoint(old, replace(old, current=None, pairing_verifier="v" * 43, pairing=attempt))
    assert policy.plan(RECOVER).attention is True
    assert (await policy.async_step(RECOVER)).state == "automatic_attention_required"
    assert not controller.mock_calls


def test_automatic_metadata_is_closed_and_legacy_confirmation_is_conservative():
    record = AutomationAttempt("a" * 64, 1, "2026-10-24T00:00:00Z")
    checkpoint = CredentialCheckpoint(processing_consent=True, candidate=CANDIDATE, automation=AutomationState(status=record))
    assert _decode(asdict(checkpoint)) == checkpoint
    legacy = asdict(checkpoint)
    del legacy["candidate"]["confirmation_attempts"]
    assert _decode(legacy).candidate.confirmation_attempts == 2
    for bad in [[], {}, {**asdict(AutomationState()), "extra": 1}, {**asdict(AutomationState()), "status": []}]:
        with pytest.raises(ValueError):
            AutomationState.decode(bad)
    for values in [{"scope": "bad"}, {"attempts": True}, {"attempts": -1}, {"attempts": 33}]:
        with pytest.raises(ValueError):
            AutomationAttempt(**{**asdict(record), **values})
    for value in [True, -1, 3, 1.0]:
        with pytest.raises(ValueError):
            replace(CANDIDATE, confirmation_attempts=value)
    with pytest.raises(ValueError):
        AutomationState(status={})
    with pytest.raises(ValueError):
        CredentialCheckpoint(automation=AutomationState())


async def test_confirmation_budget_is_committed_before_post_and_survives_restart(automatic, hass):
    _policy, store, _controller = automatic
    old = store.snapshot
    await store.async_checkpoint(old, replace(old, candidate=CANDIDATE))
    transport = AsyncMock()
    transport.service_binding = SERVICE
    transport.installation_status.side_effect = HostedTransportError("unauthenticated", status=401)
    async def uncertain(_token):
        disk = (await reload(hass)).snapshot
        assert disk.candidate.confirmation_attempts == transport.confirm.await_count
        raise HostedTransportError("service_unavailable", retryable=True)
    transport.confirm.side_effect = uncertain
    for number in (1, 2):
        with pytest.raises(HostedTransportError):
            await HostedLifecycleController(store, transport).async_reconcile()
        store = await reload(hass)
        assert store.snapshot.candidate.confirmation_attempts == number
        assert store.snapshot.current == CURRENT
    transport.installation_status.side_effect = [HostedTransportError("unauthenticated", status=401), status()]
    result = await HostedLifecycleController(store, transport).async_reconcile()
    assert result.state == "previous_retained"
    assert transport.confirm.await_count == 2
    assert store.snapshot.candidate.token == CANDIDATE.token


async def test_confirmation_budget_failure_cannot_send_post(automatic, monkeypatch):
    from custom_components.daylight_calendar_import.hosted_credentials import (
        HostedCredentialStorageError,
    )
    _policy, store, _controller = automatic
    old = store.snapshot
    await store.async_checkpoint(old, replace(old, candidate=CANDIDATE))
    transport = AsyncMock()
    transport.service_binding = SERVICE
    transport.installation_status.side_effect = HostedTransportError("unauthenticated", status=401)
    monkeypatch.setattr(store, "async_checkpoint", AsyncMock(side_effect=HostedCredentialStorageError("private")))
    with pytest.raises(HostedCredentialStorageError):
        await HostedLifecycleController(store, transport).async_reconcile()
    transport.confirm.assert_not_called()


async def test_exhausted_confirmation_can_still_bind_already_active_candidate(automatic):
    _policy, store, _controller = automatic
    old = store.snapshot
    await store.async_checkpoint(old, replace(old, candidate=replace(CANDIDATE, confirmation_attempts=2)))
    transport = AsyncMock()
    transport.service_binding = SERVICE
    transport.installation_status.return_value = status()
    assert (await HostedLifecycleController(store, transport).async_reconcile()).state == "connected"
    transport.confirm.assert_not_called()
    assert store.snapshot.current.token == CANDIDATE.token


async def test_stale_confirmation_bundle_cannot_overwrite_a_newer_checkpoint(automatic):
    from custom_components.daylight_calendar_import.hosted_credentials import (
        HostedCredentialConflict,
    )
    _policy, store, _controller = automatic
    old = store.snapshot
    await store.async_checkpoint(old, replace(old, candidate=replace(CANDIDATE, confirmation_attempts=1)))
    transport = AsyncMock()
    with pytest.raises(HostedCredentialConflict):
        await HostedLifecycleController(store, transport)._confirm(CANDIDATE)
    transport.confirm.assert_not_called()


@pytest.mark.parametrize("change", ["stopped", "cleared", "new_scope"])
async def test_completion_never_restores_an_obsolete_automatic_budget(automatic, change):
    policy, store, controller = automatic
    async def other_owner_progress():
        old = store.snapshot
        if change == "stopped":
            proposed = replace(old, local_disconnect="unconfirmed")
        elif change == "cleared":
            proposed = replace(old, automation=None)
        else:
            proposed = replace(old, automation=replace(old.automation, status=replace(old.automation.status, scope="0" * 64)))
        await store.async_checkpoint(old, proposed)
        return ACTIVE
    controller.async_reconcile.side_effect = other_owner_progress
    await policy.async_step(RECOVER)
    if change == "stopped":
        assert store.snapshot.local_disconnect == "unconfirmed"
    elif change == "cleared":
        assert store.snapshot.automation is None
    else:
        assert store.snapshot.automation.status.scope == "0" * 64


async def test_impossibly_distant_gate_requires_attention_instead_of_an_unbounded_timer(automatic):
    policy, store, _controller = automatic
    plan = policy.plan(RECOVER)
    old = store.snapshot
    await store.async_checkpoint(old, replace(old, automation=AutomationState(status=AutomationAttempt(plan.scope, 0, "9999-01-01T00:00:00Z"))))
    assert policy.plan(RECOVER).attention


async def test_confirmation_successful_disconnect_clears_automatic_metadata(automatic):
    _policy, store, _controller = automatic
    old = store.snapshot
    await store.async_checkpoint(old, replace(old, automation=AutomationState()))
    transport = AsyncMock()
    transport.service_binding = SERVICE
    transport.installation_status.return_value = status()
    transport.disconnect.return_value = {"installation_id": CURRENT.installation_id, "status": "disconnected"}
    assert (await HostedLifecycleController(store, transport).async_disconnect()).state == "disconnected"
    assert store.snapshot.automation is None


def test_invalid_automatic_state_type_cannot_enter_checkpoint():
    with pytest.raises(ValueError):
        CredentialCheckpoint(processing_consent=True, automation={})



def test_automatic_scope_is_exactly_domain_separated():
    """Changing the separator must not silently change persisted scope IDs."""
    import hashlib

    from custom_components.daylight_calendar_import.hosted_automation import _scope

    phase, identity = "status", "test-credential"
    expected = hashlib.sha256(b"status:test-credential").hexdigest()
    assert _scope(phase, identity) == expected
    assert _scope("rotation", identity) != expected


def test_retry_jitter_uses_exact_bounded_random_range(monkeypatch):
    from custom_components.daylight_calendar_import.hosted_automation import _jitter

    seen = []
    def randbelow(limit):
        seen.append(limit)
        return limit - 1

    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.hosted_automation.secrets.randbelow",
        randbelow,
    )
    assert _jitter() == 5
    assert seen == [5]


async def test_pairing_expiry_is_exclusive_at_exact_deadline(automatic, freezer):
    from custom_components.daylight_calendar_import.hosted_credentials import (
        PairingAttempt,
    )

    policy, store, controller = automatic
    deadline = "2026-10-24T00:10:00Z"
    attempt = PairingAttempt(
        "pairing_1234567890", deadline, 5, "2026-10-24T00:00:00Z"
    )
    old = store.snapshot
    await store.async_checkpoint(
        old, replace(old, current=None, pairing_verifier="v" * 43, pairing=attempt)
    )
    freezer.move_to(deadline)
    assert policy.plan(RECOVER).attention is True
    assert (await policy.async_step(RECOVER)).state == "automatic_attention_required"
    controller.async_poll_pairing.assert_not_awaited()



@pytest.mark.parametrize("state,phase,action,limit,due", [
    (RECOVER, "status", "reconcile", 6, "2026-10-24T00:00:00+00:00"),
    (ACTIVE, "rotation", "rotate", 6, "2026-11-24T00:00:00+00:00"),
    (NEAR, "rotation", "rotate", 6, "2026-10-24T00:00:00+00:00"),
    (replace(ACTIVE, credential_expires_at="2026-10-24T00:00:00Z"),
     "status", "reconcile", 6, "2026-10-24T00:00:00+00:00"),
    (HostedConnectionResult("previous_retained", "current", CURRENT.expires_at),
     "rotation", "rotate", 6, "2026-11-24T00:00:00+00:00"),
    (HostedConnectionResult("automatic_attention_required", "current", CURRENT.expires_at),
     "rotation", "rotate", 6, "2026-11-24T00:00:00+00:00"),
])
async def test_plan_exact_phase_action_limit_and_deadline(automatic, state, phase, action, limit, due):
    policy, _store, _controller = automatic
    plan = policy.plan(state)
    assert plan is not None
    assert plan.phase == phase
    assert plan.action == action
    assert plan.limit == limit
    assert plan.due_at == datetime.fromisoformat(due)
    assert plan.attention is False


@pytest.mark.parametrize("attempt_number,expected_delay", [
    (1, 16), (2, 31), (3, 61), (4, 121), (5, 241), (6, 301),
])
async def test_retry_backoff_is_exponential_and_capped(automatic, freezer, attempt_number, expected_delay):
    policy, store, controller = automatic
    controller.async_reconcile.side_effect = HostedTransportError(
        "service_unavailable", retryable=True,
    )
    for index in range(attempt_number):
        now = datetime.now(UTC)
        with pytest.raises(HostedTransportError):
            await policy.async_step(RECOVER)
        record = store.snapshot.automation.status
        assert record.attempts == index + 1
        assert datetime.fromisoformat(record.not_before) == now + timedelta(
            seconds=min(300, 15 * 2 ** index) + 1,
        )
        if index < attempt_number - 1:
            freezer.tick(400)
    assert controller.async_reconcile.await_count == attempt_number


async def test_rotation_uncertainty_plan_is_exact_and_requires_attention(automatic):
    policy, store, controller = automatic
    old = store.snapshot
    await store.async_checkpoint(
        old, replace(old, rotation_not_before="2026-10-25T00:00:00Z")
    )
    plan = policy.plan(ACTIVE)
    assert plan is not None
    assert plan.action == "rotate"
    assert plan.phase == "rotation"
    assert plan.limit == 6
    assert plan.due_at == datetime(2026, 10, 24, tzinfo=UTC)
    from custom_components.daylight_calendar_import.hosted_automation import _scope

    assert plan.scope == _scope("rotation", CURRENT.token)
    assert plan.attention is True
    result = await policy.async_step(ACTIVE)
    assert result.state == "automatic_attention_required"
    controller.async_rotate.assert_not_awaited()


@pytest.mark.parametrize("days,attention", [(90, False), (91, False), (92, True)])
async def test_automatic_gate_91_day_boundary(automatic, days, attention):
    policy, store, _controller = automatic
    plan = policy.plan(RECOVER)
    old = store.snapshot
    gate = datetime(2026, 10, 24, tzinfo=UTC) + timedelta(days=days)
    await store.async_checkpoint(old, replace(
        old, automation=AutomationState(
            status=AutomationAttempt(plan.scope, 0, gate.isoformat())
        ),
    ))
    actual = policy.plan(RECOVER)
    assert actual.due_at == gate
    assert actual.attention is attention


async def test_verified_status_rejects_missing_current_and_noncurrent_result(automatic):
    policy, store, _controller = automatic
    assert policy._verified_status(store.snapshot, RECOVER) is None
    old = store.snapshot
    await store.async_checkpoint(old, replace(old, current=None))
    assert policy._verified_status(store.snapshot, ACTIVE) is None


@pytest.mark.parametrize("state", [
    HostedConnectionResult("connected"),
    HostedConnectionResult("recovery_required", "current", CURRENT.expires_at),
])
async def test_rotation_requires_both_verified_status_and_eligible_state(automatic, state):
    policy, _store, controller = automatic
    plan = policy.plan(state)
    assert plan.phase == "status"
    assert plan.action == "reconcile"
    await policy.async_step(state)
    controller.async_reconcile.assert_awaited_once()
    controller.async_rotate.assert_not_awaited()


async def test_reservation_includes_positive_jitter_before_remote_work(automatic, hass):
    policy, _store, controller = automatic
    now = datetime.now(UTC)

    async def inspect_reservation():
        record = (await reload(hass)).snapshot.automation.status
        assert datetime.fromisoformat(record.not_before) == now + timedelta(seconds=16)
        return ACTIVE

    controller.async_reconcile.side_effect = inspect_reservation
    await policy.async_step(RECOVER)
    controller.async_reconcile.assert_awaited_once()


async def test_verified_overlap_records_restart_safe_status_cooldown(automatic, hass):
    from custom_components.daylight_calendar_import.hosted_automation import _scope

    policy, _store, _controller = automatic
    await policy.async_note_verified(replace(ACTIVE, credential_status="overlap"))
    record = (await reload(hass)).snapshot.automation.status
    assert record.scope == _scope("status", CURRENT.token)
    assert record.attempts == 0
    assert datetime.fromisoformat(record.not_before) == datetime.now(UTC) + timedelta(minutes=5)


async def test_manual_retry_after_is_durable_without_resetting_attempts(automatic, hass, freezer):
    policy, _store, controller = automatic
    unavailable = HostedConnectionResult("service_unavailable")
    error = HostedTransportError("rate_limited", status=429, retryable=True, retry_after=300)
    await policy.async_note_failure(unavailable, error)
    restarted = await reload(hass)
    record = restarted.snapshot.automation.status
    assert record.attempts == 0
    assert datetime.fromisoformat(record.not_before) == datetime.now(UTC) + timedelta(seconds=301)
    fresh = HostedAutomationPolicy(restarted, controller, SERVICE)
    assert await fresh.async_step(RECOVER) is None
    freezer.tick(300)
    assert await fresh.async_step(RECOVER) is None
    controller.async_reconcile.assert_not_called()
    freezer.tick(1)
    await fresh.async_step(RECOVER)
    controller.async_reconcile.assert_awaited_once()


async def test_rotation_rate_limit_carries_to_status_phase_and_restart(automatic, hass, freezer):
    policy, _store, controller = automatic
    controller.async_rotate.side_effect = HostedTransportError("rate_limited", status=429, retryable=True, retry_after=300)
    with pytest.raises(HostedTransportError):
        await policy.async_step(NEAR)
    restarted = await reload(hass)
    assert restarted.snapshot.automation.rotation.attempts == 1
    assert restarted.snapshot.automation.status.attempts == 0
    fresh = HostedAutomationPolicy(restarted, controller, SERVICE)
    assert fresh.plan(RECOVER).due_at == datetime.now(UTC) + timedelta(seconds=301)
    freezer.tick(300)
    assert await fresh.async_step(RECOVER) is None
    controller.async_reconcile.assert_not_called()
    freezer.tick(1)
    await fresh.async_step(RECOVER)
    controller.async_reconcile.assert_awaited_once()
    assert restarted.snapshot.automation.rotation.attempts == 1


@pytest.mark.parametrize("state", [HostedConnectionResult("repair_required"), RECOVER])
async def test_manual_terminal_or_attention_failure_cannot_reopen_budget(automatic, state):
    policy, store, controller = automatic
    if state == RECOVER:
        old = store.snapshot
        plan = policy.plan(state)
        await store.async_checkpoint(old, replace(old, automation=AutomationState(status=AutomationAttempt(plan.scope, 6, datetime.now(UTC).isoformat()))))
    before = store.snapshot
    await policy.async_note_failure(state, HostedTransportError("forbidden", status=403))
    assert store.snapshot is before
    assert not controller.mock_calls


async def test_manual_failure_preserves_later_gate_and_nonretryable_limit(automatic):
    policy, store, _controller = automatic
    await policy.async_note_verified(ACTIVE)
    prior = store.snapshot.automation.status
    await policy.async_note_failure(RECOVER, HostedTransportError("malformed_response"))
    record = store.snapshot.automation.status
    assert record.attempts == 6
    assert record.not_before == prior.not_before


async def test_live_pairing_attention_check_is_scheduled_at_expiry_before_retry_gate(automatic):
    from custom_components.daylight_calendar_import.hosted_credentials import (
        PairingAttempt,
    )
    policy, store, _controller = automatic
    old = store.snapshot
    attempt = PairingAttempt("pairing_1234567890", "2026-10-24T00:01:00Z", 5, "2026-10-24T00:00:00Z")
    await store.async_checkpoint(old, replace(old, current=None, pairing_verifier="v" * 43, pairing=attempt))
    await policy.async_note_failure(RECOVER, HostedTransportError("rate_limited", retryable=True, retry_after=300))
    assert policy.plan(RECOVER).due_at == datetime(2026, 10, 24, 0, 1, tzinfo=UTC)
