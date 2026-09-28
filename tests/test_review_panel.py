"""Panel registration and lifecycle contract."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.daylight_calendar_import import review_panel


@pytest.mark.asyncio
async def test_panel_static_files_register_once_and_panel_can_reopen(monkeypatch):
    static = AsyncMock()
    register = AsyncMock()
    remove = Mock()
    exists = Mock(return_value=False)
    hass = SimpleNamespace(data={}, http=SimpleNamespace(async_register_static_paths=static))
    monkeypatch.setattr(review_panel.frontend, "async_panel_exists", exists)
    monkeypatch.setattr(review_panel.panel_custom, "async_register_panel", register)
    monkeypatch.setattr(review_panel.frontend, "async_remove_panel", remove)

    await review_panel.async_register_review_panel(hass)
    path = static.await_args.args[0][0]
    assert path.url_path == review_panel.PANEL_ASSETS_URL
    assert Path(path.path, "panel.js").is_file()
    assert path.cache_headers is False
    assert register.await_args.kwargs == {
        "frontend_url_path": review_panel.PANEL_PATH,
        "webcomponent_name": "daylight-import-panel",
        "module_url": f"{review_panel.PANEL_ASSETS_URL}/panel.js",
        "sidebar_title": "Daylight imports",
        "sidebar_icon": "mdi:calendar-import",
    }
    review_panel.async_remove_review_panel(hass)
    remove.assert_called_once_with(hass, review_panel.PANEL_PATH, warn_if_unknown=False)

    await review_panel.async_register_review_panel(hass)
    static.assert_awaited_once()
    assert register.await_count == 2
    exists.return_value = True
    await review_panel.async_register_review_panel(hass)
    assert register.await_count == 2
