"""Shared pytest fixtures."""

from hashlib import sha256
from importlib.util import find_spec
from unittest.mock import Mock

import pytest
from homeassistant.core import HomeAssistant

import custom_components.daylight_calendar_import.retry_scheduler as retry_scheduler


@pytest.fixture(autouse=True)
def fake_hass_scheduler_timer_boundary(monkeypatch):
    """Use real HA timers in integration tests, but not in FakeHass stubs.

    The lightweight service-unit harnesses cannot run Home Assistant timer
    internals. Scheduler-specific tests supply their own observed callbacks.
    """
    original_track = retry_scheduler.async_track_time_interval

    def track(hass, action, interval):
        if isinstance(hass, HomeAssistant):
            return original_track(hass, action, interval)
        return Mock()

    monkeypatch.setattr(retry_scheduler, "async_track_time_interval", track)


# Pytest's default parameter IDs include raw bytes and long strings verbatim.
# Size-boundary fixtures can contain megabytes of payload, and printing those
# IDs under -vv can stall GitHub Actions log handling before the test executes.
def pytest_make_parametrize_id(config, val, argname):
    """Give large literal parameters deterministic, bounded collection IDs."""
    if not isinstance(val, (bytes, str)) or len(val) <= 128:
        return None
    raw = val if isinstance(val, bytes) else val.encode("utf-8", "surrogatepass")
    return f"{argname}-{type(val).__name__}-{len(val)}-{sha256(raw).hexdigest()[:12]}"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow Home Assistant to load this repository's custom integration."""

# The official HA 2026.11 plugin mocks Store's new _FileStoreIO transport.
# Daylight's safety-critical pending-store adapter intentionally inspects the
# original serialized disk envelope before HA migration. Use isolated real
# files with that plugin so restart and corruption tests exercise the same
# on-disk boundary as production instead of an incompatible in-memory shim.
# Older HA fixture packages retain their existing mock-storage behavior.
if find_spec("pytest_homeassistant") is not None:

    @pytest.fixture
    def hass_storage():
        """Use real serialized storage with the official HA fixture package."""
        return {}

    @pytest.fixture
    def hass_config_dir(hass_tmp_config_dir):
        """Keep real Home Assistant storage private to each test."""
        return hass_tmp_config_dir
