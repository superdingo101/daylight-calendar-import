"""Tests for config flow metadata."""

from custom_components.daylight_calendar_import.config_flow import DaylightCalendarImportConfigFlow


def test_config_flow_version():
    assert DaylightCalendarImportConfigFlow.VERSION == 1
