from __future__ import annotations

import importlib
import logging

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor", "number", "binary_sensor", "select", "switch", "button", "time"]


async def async_remove_config_entry_device(hass, entry, device) -> bool:
    # HA asks this before it lets the user delete a device from the entry
    return True


async def async_setup_entry(hass, entry) -> bool:
    # Import in the executor, not on the loop: paho-mqtt/bleak do blocking I/O at import time.
    module = await hass.async_add_import_executor_job(importlib.import_module, ".coordinator", __package__)

    # Init the platforms  must be wired before Coordinator.__init__ registers Distribution's entities.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator = module.Coordinator(hass, entry)
    await coordinator.async_setup()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await _async_register_frontend(hass)
    return True


async def _async_register_frontend(hass) -> None:
    # Static path + websocket commands are registered once; the sidebar panel re-registers every setup with an mtime cache-buster.
    from pathlib import Path
    from homeassistant.components import panel_custom, frontend
    from homeassistant.components.http import StaticPathConfig
    from .const import PANEL_TITLE, PANEL_ICON, STATIC_URL
    from .websocket import async_register_ws

    frontend_dir = Path(__file__).resolve().parent / "frontend"
    bundle = frontend_dir / "ZenMaster-panel.js"
    version = await hass.async_add_executor_job(lambda: int(bundle.stat().st_mtime) if bundle.exists() else 0)  # cache-buster: bundle mtime -> ?v=; stat off the event loop

    if not hass.data.get(f"{DOMAIN}_frontend"):
        hass.data[f"{DOMAIN}_frontend"] = True
        await hass.http.async_register_static_paths([StaticPathConfig(STATIC_URL, str(frontend_dir), False)])
        async_register_ws(hass)

    frontend.async_remove_panel(hass, DOMAIN, warn_if_unknown=False)  # a reload registers it again
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=DOMAIN,
        webcomponent_name="zenmaster-panel",
        module_url=f"{STATIC_URL}/ZenMaster-panel.js?v={version}",
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        require_admin=False,
        config={},
    )


async def async_unload_entry(hass, entry) -> bool:
    coordinator = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if coordinator is not None:
        await coordinator.unload()
    if not hass.data.get(DOMAIN):
        from homeassistant.components import frontend

        frontend.async_remove_panel(hass, DOMAIN)
        hass.data.pop(f"{DOMAIN}_frontend", None)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
