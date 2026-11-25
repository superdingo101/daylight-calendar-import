"""Exercise the exact error and authority contract of parse absence recovery."""
from unittest.mock import AsyncMock

import pytest

from custom_components.daylight_calendar_import.hosted_parse_transport import (
    HostedParseError, HostedParseNotAdmitted,
)
from custom_components.daylight_calendar_import.hosted_parser import DaylightHostedParserProvider
from custom_components.daylight_calendar_import.sources import TextSourceAdapter
from custom_components.daylight_calendar_import.storage import PendingImportStore
from tests.test_hosted_operations import Backend, SERVICE, checkpoint


@pytest.mark.parametrize(("code", "status", "retryable", "uncertain", "probe"), [
    ("provider_unavailable", 503, False, False, "none"),
    ("provider_unavailable", 502, True, False, "none"),
    ("provider_unavailable", 503, True, True, "none"),
    ("configuration_error", 503, True, False, "none"),
    ("provider_unavailable", 503, True, False, "absent"),
    ("provider_unavailable", 503, True, False, "denied"),
])
async def test_parse_absence_recovery_preserves_failure_contract(
    hass, code, status, retryable, uncertain, probe,
):
    store = PendingImportStore(hass)
    store._store = Backend()
    saved = checkpoint()
    provider = DaylightHostedParserProvider(
        hass, service_binding=SERVICE, checkpoint=lambda: saved,
        active=lambda: True, operation_store=store, operation_namespace="entry:service",
    )
    failure = HostedParseError(
        code, status=status, retryable=retryable, uncertain=uncertain, retry_after=30,
    )
    post = AsyncMock(side_effect=failure)
    recover = AsyncMock(return_value=None)
    if probe == "denied":
        recover.side_effect = HostedParseError("forbidden", status=403)
    provider._transport.async_parse = post
    provider._transport.async_operation = recover
    source = TextSourceAdapter().create("Meeting", source_id="stable-source")
    with pytest.raises(HostedParseError) as caught:
        await provider.async_parse(
            source, reference_datetime=source.received_at.isoformat(), time_zone="UTC",
        )
    record = next(iter(store._hosted_operations.values()))
    assert record["accepted"] is False
    assert provider.last_operation_id == record["key"]
    post.assert_awaited_once()
    assert post.await_args.kwargs["operation_key"] == record["key"]
    error = caught.value
    if probe == "none":
        assert error is failure
        recover.assert_not_awaited()
    else:
        recover.assert_awaited_once_with(record["key"], token=saved.current.token, prove_absence=True)
        if probe == "absent":
            assert isinstance(error, HostedParseNotAdmitted)
            assert (error.code, error.status, error.retry_after) == ("provider_unavailable", 503, 30)
            assert error.retryable is True and error.uncertain is False
        else:
            assert not isinstance(error, HostedParseNotAdmitted)
            assert (error.code, error.status) == ("operation_recovery_required", 403)
            assert error.retryable is True and error.uncertain is True
