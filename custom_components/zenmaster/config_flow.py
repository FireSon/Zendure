"""Config flow for ZenMaster integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, ConfigSubentryData, ConfigSubentryFlow
from homeassistant.core import callback

from .const import DOMAIN, PANEL_TITLE
from .coordinator import DRIVERS
from .entity import unique_name


class ZenMasterConfigFlow(ConfigFlow, domain=DOMAIN):
    """Single-instance flow with no fields: everything is configured in the panel; the vendor devices are subentries."""

    VERSION = 2

    def __init__(self) -> None:
        self._found: list[ConfigSubentryData] = []  # devices identified over their local API or Bluetooth before the entry existed

    async def _find(self, discovery_info: Any) -> None:
        # every driver that can is asked whether the find is its device; one that told us who it is becomes a subentry of the entry about to be created
        for cls in DRIVERS:
            if not hasattr(cls, "first_find") or (found := await cls.first_find(self.hass, discovery_info)) is None:
                continue
            if (family := cls.device_family(found)) is not None and not any(s["data"]["sn"] == found["sn"] for s in self._found):
                title = unique_name(family, found["sn"], {s["title"] for s in self._found})
                self._found.append(ConfigSubentryData(data=found, subentry_type=cls.id, title=title, unique_id=f"{cls.id}_{found['sn']}"))

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(PANEL_TITLE)
        self._abort_if_unique_id_configured()
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=vol.Schema({}))
        return self.async_create_entry(title=PANEL_TITLE, data={}, subentries=self._found)

    async def async_step_zeroconf(self, discovery_info: Any) -> ConfigFlowResult:
        # only before the entry exists, so the new entry starts with that device; once the fleet runs the coordinator listens itself
        await self.async_set_unique_id(PANEL_TITLE)
        self._abort_if_unique_id_configured()
        await self._find(discovery_info)
        return await self.async_step_user()

    async def async_step_bluetooth(self, discovery_info: Any) -> ConfigFlowResult:
        # an advertisement before the entry exists, asked the same way
        await self.async_set_unique_id(PANEL_TITLE)
        self._abort_if_unique_id_configured()
        await self._find(discovery_info)
        return await self.async_step_user()

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        # none: devices, consumers and solar groups are added, edited and removed in the panel itself
        return {}
