"""Register the local review panel with Home Assistant."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN

PANEL_PATH = "daylight-calendar-import"
PANEL_ASSETS_URL = f"/{DOMAIN}/static"
_STATIC_REGISTERED = f"{DOMAIN}_panel_static_registered"


async def async_register_review_panel(hass: HomeAssistant) -> None:
    """Serve a bundled panel and expose it in the Home Assistant sidebar."""
    if frontend.async_panel_exists(hass, PANEL_PATH):
        return
    if not hass.data.get(_STATIC_REGISTERED):
        await hass.http.async_register_static_paths([
            StaticPathConfig(PANEL_ASSETS_URL, str(Path(__file__).with_name("frontend")), cache_headers=False)
        ])
        hass.data[_STATIC_REGISTERED] = True
    await panel_custom.async_register_panel(
        hass, frontend_url_path=PANEL_PATH,
        webcomponent_name="daylight-import-panel",
        module_url=f"{PANEL_ASSETS_URL}/panel.js",
        sidebar_title="Daylight imports",
        sidebar_icon="mdi:calendar-import",
    )


def async_remove_review_panel(hass: HomeAssistant) -> None:
    """Remove the sidebar entry when the integration unloads."""
    frontend.async_remove_panel(hass, PANEL_PATH, warn_if_unknown=False)
