"""Exercise the two HA cache APIs without relying on the installed version."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.daylight_calendar_import.storage_compat import invalidate_store_cache


@pytest.mark.parametrize("has_io", [False, True])
def test_legacy_cache_invalidation(has_io):
    manager = Mock()
    store = SimpleNamespace(_manager=manager, key="daylight.test")
    if has_io:
        store._io = None
    invalidate_store_cache(store)
    manager.async_invalidate.assert_called_once_with("daylight.test")
    assert manager.mock_calls == [("async_invalidate", ("daylight.test",), {})]


def test_current_cache_invalidation_prefers_io():
    io, manager = Mock(), Mock()
    store = SimpleNamespace(_io=io, _manager=manager, key="daylight.test")
    invalidate_store_cache(store)
    io.async_invalidate_cache.assert_called_once_with()
    assert not manager.mock_calls
    assert io.mock_calls == [("async_invalidate_cache", (), {})]


@pytest.mark.parametrize("modern", [False, True])
def test_invalidation_failure_propagates(modern):
    manager, io = Mock(), Mock()
    failure = OSError("cache failed")
    if modern:
        io.async_invalidate_cache.side_effect = failure
        store = SimpleNamespace(_io=io, _manager=manager, key="daylight.test")
    else:
        manager.async_invalidate.side_effect = failure
        store = SimpleNamespace(_manager=manager, key="daylight.test")
    with pytest.raises(OSError) as raised:
        invalidate_store_cache(store)
    assert raised.value is failure
    if modern:
        io.async_invalidate_cache.assert_called_once_with()
        assert not manager.mock_calls
    else:
        manager.async_invalidate.assert_called_once_with("daylight.test")
        assert not io.mock_calls


@pytest.mark.parametrize("store", [
    SimpleNamespace(key="daylight.test"),
    SimpleNamespace(_io=SimpleNamespace(), _manager=Mock(), key="daylight.test"),
])
def test_unsupported_shape_fails_closed(store):
    with pytest.raises(AttributeError):
        invalidate_store_cache(store)
    if hasattr(store, "_manager"):
        assert not store._manager.mock_calls
