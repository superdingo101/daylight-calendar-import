"""Mutation-sensitive contracts for the durable retry service handlers.

Exercise the registered HA handlers, rather than merely calling storage helpers,
so authorization, claim ownership and parser-to-review routing are observable.
"""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeassistant.core import Context, SupportsResponse
from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.exceptions import ServiceValidationError, Unauthorized
import pytest

from custom_components.daylight_calendar_import import (
    RETRY_JOB_SCHEMA, async_setup_entry,
)
from custom_components.daylight_calendar_import.const import (
    ATTR_JOB_ID, DOMAIN,
    SERVICE_LIST_RETRY_JOBS, SERVICE_RETRY_JOB,
    SERVICE_CANCEL_RETRY_JOB, SERVICE_DELETE_RETRY_JOB,
)
from custom_components.daylight_calendar_import.parser import ParseOutcome
from custom_components.daylight_calendar_import.providers import ProviderError
from custom_components.daylight_calendar_import.retry_jobs import RetryJob
from custom_components.daylight_calendar_import.storage import (
    PendingImport, PendingImportAddResult, RetryJobConflict,
)
from tests.test_init import FakeHass, FakePermissions, draft, entry


def saved_job(**overrides):
    original = RetryJob.create(
        activity_id="saved-1",
        source_text="Christmas dinner on December 25 at 6 PM",
        source_id="manual-1",
        received_at=datetime(2026, 11, 18, 20, 35, tzinfo=UTC),
        provider_entity_id="ai_task.test",
        calendar_entity="calendar.family",
        owner_user_id="owner",
        reimport_duplicates=True,
    )
    return replace(original, **overrides)


@pytest.fixture
def service_setup(monkeypatch):
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.async_register_review_panel",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.async_track_time_interval",
        Mock(return_value=Mock()),
    )
    # The service harness does not implement HA event-loop timers; timer
    # ownership and execution are covered in test_retry_scheduler.py.
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.retry_scheduler.async_track_time_interval",
        Mock(return_value=Mock()),
    )
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.ir.async_create_issue", Mock(),
    )
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.ir.async_delete_issue", Mock(),
    )

    async def start(job=None, *, user_id="owner", is_admin=False, allowed=True):
        permissions = FakePermissions(allowed=allowed)
        hass = FakeHass(
            user=SimpleNamespace(permissions=permissions, is_admin=is_admin),
        )
        waiting = job or saved_job(state="waiting", claim=None)
        claimed = replace(waiting, state="processing", claim="new-claim", attempts=waiting.attempts + 1)
        store = SimpleNamespace(
            async_load=AsyncMock(),
            list_notifications=Mock(return_value=()),
            list_retry_jobs=Mock(return_value=[waiting]),
            get_retry_job=Mock(return_value=waiting),
            async_claim_retry=AsyncMock(return_value=claimed),
            async_cancel_retry=AsyncMock(return_value=replace(waiting, state="cancelled", claim=None)),
            async_delete_retry=AsyncMock(return_value=True),
            async_record_parse_failure=AsyncMock(),
            async_add=AsyncMock(return_value=PendingImportAddResult(
                pending=PendingImport.create(
                    source_text=waiting.source_text, events=[draft()],
                ),
                duplicate_source=False, duplicate_events=0,
            )),
        )
        monkeypatch.setattr(
            "custom_components.daylight_calendar_import.PendingImportStore",
            lambda _hass: store,
        )
        assert await async_setup_entry(hass, entry())
        def invoke(service, *, job_id=None, caller=user_id):
            data = {} if job_id is None else {ATTR_JOB_ID: job_id}
            return hass.services.handlers[(DOMAIN, service)][0](
                SimpleNamespace(data=data, context=Context(user_id=caller)),
            )
        return SimpleNamespace(
            hass=hass, store=store, job=waiting, claimed=claimed,
            permissions=permissions, invoke=invoke,
        )

    return start


async def test_retry_service_registration_exact_schema_and_response_contract(service_setup):
    s = await service_setup()
    for service in (SERVICE_RETRY_JOB, SERVICE_CANCEL_RETRY_JOB, SERVICE_DELETE_RETRY_JOB):
        _, kwargs = s.hass.services.handlers[(DOMAIN, service)]
        assert kwargs["schema"] is RETRY_JOB_SCHEMA
        assert kwargs["supports_response"] is SupportsResponse.ONLY
        assert kwargs["schema"]({ATTR_JOB_ID: "saved-1"}) == {ATTR_JOB_ID: "saved-1"}
        with pytest.raises(Exception):
            kwargs["schema"]({})
        with pytest.raises(Exception):
            kwargs["schema"]({ATTR_JOB_ID: ""})
    _, kwargs = s.hass.services.handlers[(DOMAIN, SERVICE_LIST_RETRY_JOBS)]
    assert kwargs["supports_response"] is SupportsResponse.ONLY


@pytest.mark.parametrize(("is_admin", "expected_ids"), [
    (False, ["owned"]), (True, ["owned", "other", "unowned"]),
])
async def test_retry_list_hides_other_users_and_private_source(service_setup, is_admin, expected_ids):
    s = await service_setup(is_admin=is_admin)
    owned = saved_job(id="owned", owner_user_id="owner")
    other = saved_job(id="other", owner_user_id="another-user")
    no_owner = saved_job(id="unowned", owner_user_id=None)
    s.store.list_retry_jobs.return_value = [owned, other, no_owner]
    result = await s.invoke(SERVICE_LIST_RETRY_JOBS)
    assert result == {"jobs": [job.summary() for job in (owned, other, no_owner) if job.id in expected_ids]}
    assert "source_text" not in str(result)
    assert "manual-1" not in str(result)
    assert s.store.async_claim_retry.await_count == 0
    assert s.permissions.calls == [
        ("ai_task.test", POLICY_CONTROL),
        ("calendar.family", POLICY_CONTROL),
    ]


@pytest.mark.parametrize("service", [
    SERVICE_RETRY_JOB, SERVICE_CANCEL_RETRY_JOB, SERVICE_DELETE_RETRY_JOB,
])
async def test_retry_actions_reject_non_owner_without_touching_job(service_setup, service):
    s = await service_setup(job=saved_job(owner_user_id="another-user"))
    with pytest.raises(Unauthorized):
        await s.invoke(service, job_id=s.job.id)
    assert s.hass.auth.calls[-1] == "owner"
    s.store.async_claim_retry.assert_not_awaited()
    s.store.async_cancel_retry.assert_not_awaited()
    s.store.async_delete_retry.assert_not_awaited()


@pytest.mark.parametrize("service", [
    SERVICE_LIST_RETRY_JOBS, SERVICE_RETRY_JOB,
    SERVICE_CANCEL_RETRY_JOB, SERVICE_DELETE_RETRY_JOB,
])
async def test_retry_actions_reject_anonymous_or_calendar_denied(service_setup, service):
    s = await service_setup()
    with pytest.raises(Unauthorized):
        await s.invoke(service, job_id=s.job.id, caller=None)
    s.permissions.allowed = {"ai_task.test": True, "calendar.family": False}
    with pytest.raises(Unauthorized):
        await s.invoke(service, job_id=s.job.id)
    s.store.async_claim_retry.assert_not_awaited()
    s.store.async_cancel_retry.assert_not_awaited()
    s.store.async_delete_retry.assert_not_awaited()


@pytest.mark.parametrize("service", [
    SERVICE_RETRY_JOB, SERVICE_CANCEL_RETRY_JOB, SERVICE_DELETE_RETRY_JOB,
])
async def test_retry_actions_missing_job_return_exact_error(service_setup, service):
    s = await service_setup()
    s.store.get_retry_job.return_value = None
    with pytest.raises(ServiceValidationError) as exc:
        await s.invoke(service, job_id="does-not-exist")
    assert str(exc.value) == "Saved import not found"
    s.store.get_retry_job.assert_called_once_with("does-not-exist")


async def test_admin_may_cancel_other_owner_and_cancellation_is_private(service_setup):
    s = await service_setup(job=saved_job(owner_user_id="different"), is_admin=True)
    result = await s.invoke(SERVICE_CANCEL_RETRY_JOB, job_id=s.job.id)
    cancelled = replace(s.job, state="cancelled", claim=None)
    assert result == {"job": cancelled.summary()}
    assert "source_text" not in result["job"]
    s.store.async_cancel_retry.assert_awaited_once_with(s.job.id)
    s.store.async_claim_retry.assert_not_awaited()


async def test_owner_delete_returns_exact_identity_and_never_parses(service_setup):
    s = await service_setup()
    assert await s.invoke(SERVICE_DELETE_RETRY_JOB, job_id=s.job.id) == {
        "job_id": s.job.id, "deleted": True,
    }
    s.store.async_delete_retry.assert_awaited_once_with(s.job.id)
    s.store.async_claim_retry.assert_not_awaited()


@pytest.mark.parametrize(("overrides", "message"), [
    ({"provider_entity_id": "ai_task.changed"}, "The saved AI Task or calendar changed. Verify settings before retrying."),
    ({"calendar_entity": "calendar.no_longer_allowed"}, "The saved AI Task or calendar changed. Verify settings before retrying."),
    ({"source_kind": "image"}, "Retrying this source type is not supported yet"),
])
async def test_retry_preflight_blocks_stale_or_unsupported_source(
    service_setup, monkeypatch, overrides, message,
):
    s = await service_setup(job=saved_job(**overrides))
    parser = AsyncMock()
    monkeypatch.setattr("custom_components.daylight_calendar_import._async_parse_source", parser)
    with pytest.raises(ServiceValidationError) as exc:
        await s.invoke(SERVICE_RETRY_JOB, job_id=s.job.id)
    assert str(exc.value) == message
    s.store.async_claim_retry.assert_not_awaited()
    parser.assert_not_awaited()
    s.store.async_add.assert_not_awaited()


async def test_retry_claim_and_success_restore_original_source_and_review_contract(
    service_setup, monkeypatch,
):
    s = await service_setup()
    outcome = ParseOutcome([draft()], ["AI uncertainty"])
    parse = AsyncMock(return_value=outcome)
    monkeypatch.setattr("custom_components.daylight_calendar_import._async_parse_source", parse)
    result = await s.invoke(SERVICE_RETRY_JOB, job_id=s.job.id)
    assert result == {
        "status": "pending_review",
        "activity_id": s.job.id,
        "pending": s.store.async_add.return_value.pending.as_service_dict(),
        "duplicate": False,
        "duplicate_source": False,
        "duplicate_events": 0,
        "warnings": ["AI uncertainty"],
    }
    s.store.async_claim_retry.assert_awaited_once_with(s.job.id, automatic=False)
    parse.assert_awaited_once()
    args, kwargs = parse.await_args
    assert args[0] is s.hass
    assert args[2] == "ai_task.test"
    assert args[1].text == s.job.source_text
    assert args[1].upstream_source_id == s.job.source_id
    assert args[1].received_at.isoformat() == s.job.received_at
    assert kwargs == {"reference_datetime": datetime.fromisoformat(s.job.received_at)}
    s.store.async_add.assert_awaited_once_with(
        source_text=s.claimed.source_text,
        events=outcome.events,
        event_assumptions=None,
        source_id=s.claimed.source_id,
        calendar_entity=s.claimed.calendar_entity,
        routing_unresolved=False,
        reimport_duplicates=True,
        warnings=["AI uncertainty"],
        activity_id=s.job.id,
        retry_claim=s.claimed.claim,
    )
    assert s.store.active_submissions == set()
    s.store.async_record_parse_failure.assert_not_awaited()


async def test_retry_provider_failure_returns_saved_status_and_releases_claim(
    service_setup, monkeypatch,
):
    s = await service_setup()
    failure = ProviderError("provider_unavailable", "private provider details")
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import._async_parse_source",
        AsyncMock(side_effect=failure),
    )
    stopped = replace(s.claimed, state="waiting", claim=None, last_failure_code="provider_unavailable")
    # Authorization, execution and final failure response each read the job.
    s.store.get_retry_job.side_effect = [s.job, s.job, stopped]
    assert await s.invoke(SERVICE_RETRY_JOB, job_id=s.job.id) == {
        "status": "saved_for_retry",
        "activity_id": s.job.id, "pending": None,
        "duplicate": False, "duplicate_source": False,
        "duplicate_events": 0, "warnings": [],
        "retry_job": stopped.summary(),
    }
    s.store.async_record_parse_failure.assert_awaited_once_with(
        s.job.id, provider_error_code="provider_unavailable", retry_claim=s.claimed.claim,
    )
    assert s.store.get_retry_job.call_count == 3
    s.store.async_add.assert_not_awaited()
    assert s.store.active_submissions == set()


async def test_retry_provider_failure_after_deletion_returns_cancelled(service_setup, monkeypatch):
    s = await service_setup()
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import._async_parse_source",
        AsyncMock(side_effect=ProviderError("provider_unavailable", "private error")),
    )
    s.store.get_retry_job.side_effect = [s.job, s.job, None]
    assert await s.invoke(SERVICE_RETRY_JOB, job_id=s.job.id) == {
        "status": "cancelled", "job_id": s.job.id,
    }
    s.store.async_record_parse_failure.assert_awaited_once_with(
        s.job.id, provider_error_code="provider_unavailable", retry_claim=s.claimed.claim,
    )


async def test_late_parser_result_after_cancellation_cannot_create_review(
    service_setup, monkeypatch,
):
    s = await service_setup()
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import._async_parse_source",
        AsyncMock(return_value=ParseOutcome([draft()], [])),
    )
    s.store.async_add.side_effect = RetryJobConflict("Retry is cancelled")
    assert await s.invoke(SERVICE_RETRY_JOB, job_id=s.job.id) == {
        "status": "cancelled", "job_id": s.job.id,
    }
    s.store.async_add.assert_awaited_once()
    s.store.async_record_parse_failure.assert_not_awaited()
    assert s.store.active_submissions == set()


async def test_retry_unexpected_parser_failure_records_exact_claim_and_reraises(
    service_setup, monkeypatch,
):
    s = await service_setup()
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import._async_parse_source",
        AsyncMock(side_effect=RuntimeError("unexpected parse failure")),
    )
    with pytest.raises(RuntimeError, match="unexpected parse failure"):
        await s.invoke(SERVICE_RETRY_JOB, job_id=s.job.id)
    s.store.async_record_parse_failure.assert_awaited_once_with(
        s.job.id, retry_claim=s.claimed.claim,
    )
    assert s.store.active_submissions == set()


async def test_retry_route_change_is_rejected_before_claim(service_setup, monkeypatch):
    """A saved source must not silently target a different calendar on retry."""
    from custom_components.daylight_calendar_import.source_routing import SourceRoutingPlan

    s = await service_setup()
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.plan_source_routing",
        lambda source, **kwargs: SourceRoutingPlan(source, "calendar.other"),
    )
    with pytest.raises(ServiceValidationError) as exc:
        await s.invoke(SERVICE_RETRY_JOB, job_id=s.job.id)
    assert str(exc.value) == "Calendar routing changed. Review the saved import."
    s.store.async_claim_retry.assert_not_awaited()
    s.store.async_add.assert_not_awaited()


async def test_retry_rejects_competing_claim_and_surfaces_exact_conflict(
    service_setup, monkeypatch,
):
    s = await service_setup()
    s.store.async_claim_retry.side_effect = RetryJobConflict("Retry is already processing")
    parser = AsyncMock()
    monkeypatch.setattr("custom_components.daylight_calendar_import._async_parse_source", parser)
    with pytest.raises(ServiceValidationError) as exc:
        await s.invoke(SERVICE_RETRY_JOB, job_id=s.job.id)
    assert str(exc.value) == "Retry is already processing"
    s.store.async_claim_retry.assert_awaited_once_with(s.job.id, automatic=False)
    parser.assert_not_awaited()


async def test_retry_preserves_nonempty_time_assumptions_and_route_disclosures(
    service_setup, monkeypatch,
):
    """Timezone uncertainty and unresolved calendar routing must survive retry."""
    from custom_components.daylight_calendar_import.source_routing import SourceRoutingPlan

    s = await service_setup()
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.plan_source_routing",
        lambda source, **kwargs: SourceRoutingPlan(
            source, "calendar.family", ("Confirm the destination",), True,
        ),
    )
    assumptions = [("Timezone requires review",)]
    outcome = ParseOutcome([draft()], ["AI needs review"], assumptions)
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import._async_parse_source",
        AsyncMock(return_value=outcome),
    )
    response = await s.invoke(SERVICE_RETRY_JOB, job_id=s.job.id)
    assert response["status"] == "pending_review"
    assert response["warnings"] == ["AI needs review", "Confirm the destination"]
    args, kwargs = s.store.async_add.await_args
    assert args == ()
    assert kwargs["event_assumptions"] == assumptions
    assert kwargs["warnings"] == ["AI needs review", "Confirm the destination"]
    assert kwargs["routing_unresolved"] is True
    assert kwargs["activity_id"] == s.job.id
    assert kwargs["retry_claim"] == s.claimed.claim
