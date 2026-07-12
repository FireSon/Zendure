from __future__ import annotations

import importlib
import logging

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor", "number", "binary_sensor", "select", "switch"]


async def async_setup_entry(hass, entry) -> bool:
    # Coordinator import runs in the import executor, not on the loop: it pulls in pandas/numpy,
    # whose import does blocking I/O (zoneinfo open + numexpr). Lazy so out-of-HA never loads the glue.
    module = await hass.async_add_import_executor_job(importlib.import_module, ".coordinator", __package__)
    Coordinator = module.Coordinator

    # Forward platforms first so EmSensor/Number/Select/BinarySensor.add is wired before any device
    # registers its entities (Distribution registers in Coordinator.__init__).
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator = Coordinator(hass, {}, entry)
    await coordinator.async_setup()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await _async_register_frontend(hass)
    return True


async def _async_register_frontend(hass) -> None:
    # Serve the built panel + fonts as a static path, register the websocket commands (both once), and
    # (re)register the sidebar panel every setup with a cache-buster so a rebuilt bundle reloads on reload.
    from pathlib import Path
    from homeassistant.components import panel_custom, frontend
    from homeassistant.components.http import StaticPathConfig
    from .const import PANEL_TITLE, PANEL_ICON, STATIC_URL
    from .websocket import async_register_ws

    frontend_dir = Path(__file__).resolve().parent / "frontend"
    bundle = frontend_dir / "EnergyMaster-panel.js"
    version = await hass.async_add_executor_job(lambda: int(bundle.stat().st_mtime) if bundle.exists() else 0)  # cache-buster: bundle mtime -> ?v=; stat off the event loop

    if not hass.data.get(f"{DOMAIN}_frontend"):
        hass.data[f"{DOMAIN}_frontend"] = True
        await hass.http.async_register_static_paths([StaticPathConfig(STATIC_URL, str(frontend_dir), False)])
        async_register_ws(hass)

    try:
        frontend.async_remove_panel(hass, DOMAIN)
    except Exception:  # not registered yet
        pass
    await panel_custom.async_register_panel(
        hass, frontend_url_path=DOMAIN, webcomponent_name="energymaster-panel",
        module_url=f"{STATIC_URL}/EnergyMaster-panel.js?v={version}",
        sidebar_title=PANEL_TITLE, sidebar_icon=PANEL_ICON, require_admin=False, config={})


async def async_unload_entry(hass, entry) -> bool:
    coordinator = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if coordinator is not None:
        await coordinator.unload()
    if not hass.data.get(DOMAIN):
        from homeassistant.components import frontend
        frontend.async_remove_panel(hass, DOMAIN)
        hass.data.pop(f"{DOMAIN}_frontend", None)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
