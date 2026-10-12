"""Typed failure classification, durable recovery advice, and privacy boundaries."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aiohttp import ClientConnectionError, ClientResponseError
from homeassistant.exceptions import HomeAssistantError
from httpx import ConnectError, ReadTimeout, HTTPStatusError, Request, Response
import pytest

from custom_components.daylight_calendar_import import providers
from custom_components.daylight_calendar_import.provider_failures import (
    PROVIDER_FAILURE_GUIDANCE, classify_provider_failure,
)
from custom_components.daylight_calendar_import.sources import TextSourceAdapter
from tests.test_storage import FakeStoreBackend, make_store
from tests.test_source_failure_notifications import begin, enable


def http_error(status):
    return ClientResponseError(Mock(), (), status=status, message="private token")


@pytest.mark.parametrize(("error", "code"), [
    (TimeoutError("private email"), "provider_timeout"),
    (ConnectionError("private host"), "provider_unavailable"),
    (ClientConnectionError("private host"), "provider_unavailable"),
    (ReadTimeout("private request"), "provider_timeout"),
    (ConnectError("private request"), "provider_unavailable"),
    (HTTPStatusError("private response", request=Request("GET", "https://private.test"),
                     response=Response(429)), "provider_rate_limited"),
    (http_error(429), "provider_rate_limited"),
    (http_error(401), "provider_authentication"),
    (http_error(403), "provider_authentication"),
    (http_error(500), "provider_unavailable"),
    (http_error(599), "provider_unavailable"),
    (http_error(400), "provider_error"),
    (http_error(600), "provider_error"),
    (HomeAssistantError("timeout rate limit 429 private"), "provider_error"),
])
async def test_typed_errors_are_wrapped_without_exposing_messages(monkeypatch, error, code):
    generate = AsyncMock(side_effect=error)
    monkeypatch.setattr(providers.ai_task, "async_generate_data", generate)
    with pytest.raises(providers.ProviderError) as caught:
        await providers.AITaskParserProvider(object(), "ai_task.test").async_parse(
            TextSourceAdapter().create("private meeting"),
            reference_datetime="now", time_zone="UTC",
        )
    assert caught.value.code == code
    assert str(caught.value) == "AI Task could not parse the source"
    import traceback
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert "private" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("status", [429, 401, 503, 400, "429", True, None])
def test_sdk_structured_status_in_explicit_cause(status):
    cause = RuntimeError("private response")
    cause.status_code = status
    outer = HomeAssistantError("private wrapper")
    outer.__cause__ = cause
    expected = {429: "provider_rate_limited", 401: "provider_authentication", 503: "provider_unavailable"}
    assert classify_provider_failure(outer) == expected.get(status, "provider_error")


def test_cause_cycles_depth_and_implicit_context_are_bounded():
    outer = HomeAssistantError("private wrapper")
    outer.__cause__ = outer
    assert classify_provider_failure(outer) == "provider_error"
    outer.__cause__ = None
    outer.__context__ = TimeoutError()
    assert classify_provider_failure(outer) == "provider_error"
    current = outer
    for _ in range(16):
        current.__cause__ = HomeAssistantError()
        current = current.__cause__
    current.__cause__ = TimeoutError()
    assert classify_provider_failure(outer) == "provider_error"
    outer.__cause__ = TimeoutError()
    assert classify_provider_failure(outer) == "provider_timeout"


async def test_explicit_entity_unavailable_skips_text_provider(monkeypatch):
    entity = SimpleNamespace(available=False, supported_features=0)
    provider = providers.AITaskParserProvider(object(), "ai_task.private")
    monkeypatch.setattr(provider, "_entity", lambda: entity)
    generate = AsyncMock()
    monkeypatch.setattr(providers.ai_task, "async_generate_data", generate)
    with pytest.raises(providers.ProviderError) as caught:
        await provider.async_parse(TextSourceAdapter().create("private"), reference_datetime="now", time_zone="UTC")
    assert caught.value.code == "provider_unavailable"
    generate.assert_not_awaited()


async def test_invalid_top_level_response_has_safe_category(monkeypatch):
    monkeypatch.setattr(providers.ai_task, "async_generate_data", AsyncMock(return_value=SimpleNamespace(data={"private": "secret"})))
    with pytest.raises(providers.ProviderError) as caught:
        await providers.AITaskParserProvider(object(), "ai_task.test").async_parse(
            TextSourceAdapter().create("private"), reference_datetime="now", time_zone="UTC",
        )
    assert caught.value.code == "provider_invalid_response"
    assert str(caught.value) == "AI Task returned an invalid result"


async def test_provider_cancellation_still_propagates(monkeypatch):
    monkeypatch.setattr(providers.ai_task, "async_generate_data", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await providers.AITaskParserProvider(object(), "ai_task.test").async_parse(
            TextSourceAdapter().create("private"), reference_datetime="now", time_zone="UTC",
        )


@pytest.mark.parametrize("code", [*PROVIDER_FAILURE_GUIDANCE, "private untrusted code", {"private": "code"}])
async def test_provider_category_commits_atomically_with_failure_and_notification(monkeypatch, code):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    enable(store)
    await begin(store, source_id="different source")
    identifier = await begin(store)
    expected = code if isinstance(code, str) and code in PROVIDER_FAILURE_GUIDANCE else "provider_error"
    await store.async_record_parse_failure(identifier, provider_error_code=code)
    record = store.get_activity(identifier)
    assert record["status"] == "failed"
    assert record["failure_code"] == expected
    assert record["guidance"] == PROVIDER_FAILURE_GUIDANCE[expected]
    assert backend.saved[-1]["activity"][-1] == record
    assert len(store.list_notifications()) == 1
    assert "private" not in str(store.list_notifications()[0].as_dict())
    backend.load_result = backend.saved[-1]
    restored = make_store(monkeypatch, backend)
    await restored.async_load()
    assert restored.get_activity(identifier)["failure_code"] == expected
    # Late completion must not replace a previously finalized category.
    await store.async_record_parse_failure(identifier, provider_error_code="provider_timeout")
    assert store.get_activity(identifier) == record


async def test_provider_failure_save_failure_is_not_published(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    identifier = await begin(store)
    backend.save_error = OSError("private")
    with pytest.raises(OSError):
        await store.async_record_parse_failure(identifier, provider_error_code="provider_rate_limited")
    assert store.get_activity(identifier)["status"] == "processing"
    assert "failure_code" not in store.get_activity(identifier)


async def test_cancelled_provider_failure_transaction_finishes_committing(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    identifier = await begin(store)
    started, release = asyncio.Event(), asyncio.Event()
    save = backend.async_save
    async def blocked(data):
        started.set()
        await release.wait()
        await save(data)
    backend.async_save = blocked
    task = asyncio.create_task(store.async_record_parse_failure(identifier, provider_error_code="provider_timeout"))
    await started.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.get_activity(identifier)["failure_code"] == "provider_timeout"


async def test_manual_submission_persists_provider_category_and_preserves_exception(monkeypatch):
    from homeassistant.core import Context
    from custom_components.daylight_calendar_import import async_setup_entry
    from custom_components.daylight_calendar_import.const import DOMAIN, SERVICE_SUBMIT_TEXT
    from tests.test_init import FakeHass, entry

    error = providers.ProviderError("provider_rate_limited", "AI Task could not parse the source")
    parse = AsyncMock(side_effect=error)
    store = SimpleNamespace(async_load=AsyncMock(), list_notifications=Mock(return_value=()),
                            async_begin_submission=AsyncMock(return_value="activity-id"),
                            async_record_parse_failure=AsyncMock())
    monkeypatch.setattr("custom_components.daylight_calendar_import.parse_source_with_provider", parse)
    monkeypatch.setattr("custom_components.daylight_calendar_import.PendingImportStore", lambda _hass: store)
    monkeypatch.setattr("custom_components.daylight_calendar_import.async_register_review_panel", AsyncMock())
    monkeypatch.setattr("custom_components.daylight_calendar_import.async_track_time_interval", Mock(return_value=Mock()))
    hass = FakeHass()
    await async_setup_entry(hass, entry())
    handler = hass.services.handlers[(DOMAIN, SERVICE_SUBMIT_TEXT)][0]
    with pytest.raises(providers.ProviderError) as caught:
        await handler(SimpleNamespace(data={"text": "private details"}, context=Context(user_id=None)))
    assert caught.value is error
    store.async_record_parse_failure.assert_awaited_once_with("activity-id", provider_error_code="provider_rate_limited")


async def test_email_failure_retains_retry_and_acknowledgement_semantics():
    from tests.test_email_polling import FakeSource, FakeStore, _envelope
    from custom_components.daylight_calendar_import.email_polling import async_poll_email_source

    source = FakeSource([[_envelope("private@example.test")]])
    store = FakeStore()
    store.async_record_parse_failure = AsyncMock()
    async def processor(document, activity_id):
        raise providers.ProviderError("provider_authentication", "AI Task could not parse the source")
    result = await async_poll_email_source(source, store, processor)
    assert result.processing_failures == 1
    assert result.terminal_failures == 0
    assert result.acknowledged == 0
    store.async_record_parse_failure.assert_awaited_once_with("activity-1", provider_error_code="provider_authentication")


async def test_deterministic_validation_precedes_resolved_provider_availability(monkeypatch):
    provider = providers.AITaskParserProvider(object(), "ai_task.private")
    monkeypatch.setattr(provider, "_entity", lambda: SimpleNamespace(available=False, supported_features=0))
    generate = AsyncMock()
    monkeypatch.setattr(providers.ai_task, "async_generate_data", generate)
    source = TextSourceAdapter().create("seed")
    from dataclasses import replace
    with pytest.raises(providers.SourceValidationError) as caught:
        await provider.async_parse(replace(source, text=""), reference_datetime="now", time_zone="UTC")
    assert caught.value.code == "empty_source"
    generate.assert_not_awaited()


async def test_failed_provider_failure_save_preserves_category_when_source_reclaimed(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    enable(store)
    identifier = await begin(store, source_id="private retry source")
    backend.save_error = OSError("private storage failure")
    with pytest.raises(OSError):
        await store.async_record_parse_failure(identifier, provider_error_code="provider_rate_limited")
    assert store.get_activity(identifier)["status"] == "processing"
    assert not store.list_notifications()
    backend.save_error = None
    replacement = await begin(store, source_id="private retry source")
    assert replacement != identifier
    record = store.get_activity(identifier)
    assert record["status"] == "failed"
    assert record["failure_code"] == "provider_rate_limited"
    assert record["guidance"] == PROVIDER_FAILURE_GUIDANCE["provider_rate_limited"]
    assert len(store.list_notifications()) == 1
    assert not store._source_claim_release_provider_codes


class ThrowingStatusError(Exception):
    @property
    def status_code(self):
        raise ValueError("private SDK descriptor response")


class ThrowingResult:
    @property
    def data(self):
        raise ValueError("private provider result body")


@pytest.mark.parametrize(("result", "error", "expected"), [
    (None, None, "provider_invalid_response"),
    (SimpleNamespace(), None, "provider_invalid_response"),
    (ThrowingResult(), None, "provider_invalid_response"),
    (None, ThrowingStatusError("private original provider response"), "provider_error"),
])
async def test_actual_submission_boundary_sanitizes_malformed_provider_contracts(monkeypatch, result, error, expected):
    import traceback
    from homeassistant.core import Context
    from custom_components.daylight_calendar_import import async_setup_entry
    from custom_components.daylight_calendar_import.const import DOMAIN, SERVICE_SUBMIT_TEXT
    from tests.test_init import FakeHass, entry

    generate = AsyncMock(return_value=result, side_effect=error)
    monkeypatch.setattr(providers.ai_task, "async_generate_data", generate)
    store = SimpleNamespace(async_load=AsyncMock(), list_notifications=Mock(return_value=()),
                            async_begin_submission=AsyncMock(return_value="activity-id"),
                            async_record_parse_failure=AsyncMock())
    monkeypatch.setattr("custom_components.daylight_calendar_import.PendingImportStore", lambda _hass: store)
    monkeypatch.setattr("custom_components.daylight_calendar_import.async_register_review_panel", AsyncMock())
    monkeypatch.setattr("custom_components.daylight_calendar_import.async_track_time_interval", Mock(return_value=Mock()))
    hass = FakeHass()
    await async_setup_entry(hass, entry())
    handler = hass.services.handlers[(DOMAIN, SERVICE_SUBMIT_TEXT)][0]
    with pytest.raises(providers.ProviderError) as caught:
        await handler(SimpleNamespace(data={"text": "source details"}, context=Context(user_id=None)))
    assert caught.value.code == expected
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert "private" not in "".join(traceback.format_exception(caught.value))
    store.async_record_parse_failure.assert_awaited_once_with("activity-id", provider_error_code=expected)


@pytest.mark.parametrize(("error", "expected"), [
    (ValueError("private availability details"), "provider_error"),
    (TimeoutError("private availability request"), "provider_timeout"),
])
async def test_actual_submission_sanitizes_throwing_entity_availability(monkeypatch, error, expected):
    import traceback
    from homeassistant.core import Context
    from custom_components.daylight_calendar_import import async_setup_entry
    from custom_components.daylight_calendar_import.const import DOMAIN, SERVICE_SUBMIT_TEXT
    from tests.test_init import FakeHass, entry

    class Entity:
        supported_features = 0
        @property
        def available(self):
            raise error

    generate = AsyncMock()
    monkeypatch.setattr(providers.ai_task, "async_generate_data", generate)
    store = SimpleNamespace(async_load=AsyncMock(), list_notifications=Mock(return_value=()),
                            async_begin_submission=AsyncMock(return_value="activity-id"),
                            async_record_parse_failure=AsyncMock())
    monkeypatch.setattr("custom_components.daylight_calendar_import.PendingImportStore", lambda _hass: store)
    monkeypatch.setattr("custom_components.daylight_calendar_import.async_register_review_panel", AsyncMock())
    monkeypatch.setattr("custom_components.daylight_calendar_import.async_track_time_interval", Mock(return_value=Mock()))
    hass = FakeHass()
    hass.data[providers.DATA_COMPONENT] = SimpleNamespace(get_entity=lambda _entity_id: Entity())
    await async_setup_entry(hass, entry())
    handler = hass.services.handlers[(DOMAIN, SERVICE_SUBMIT_TEXT)][0]
    with pytest.raises(providers.ProviderError) as caught:
        await handler(SimpleNamespace(data={"text": "source details"}, context=Context(user_id=None)))
    assert caught.value.code == expected
    assert str(caught.value) == "AI Task availability could not be checked"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert "private" not in "".join(traceback.format_exception(caught.value))
    store.async_record_parse_failure.assert_awaited_once_with("activity-id", provider_error_code=expected)
    generate.assert_not_awaited()
