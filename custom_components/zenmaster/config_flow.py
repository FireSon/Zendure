"""Config flow for ZenMaster integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, ConfigSubentryData, ConfigSubentryFlow, SubentryFlowResult
from homeassistant.core import callback
from homeassistant.helpers.selector import selector

from .const import DOMAIN, PANEL_TITLE
from .entity import unique_name
from .zendure.driver import ZendureDriver


DEVICE = vol.Schema({vol.Optional("ip"): selector({"text": {}}), vol.Optional("blemac"): selector({"text": {}})})
DEVICE_ADD = vol.Schema({vol.Required("sn"): str, vol.Required("product"): str, vol.Optional("device_id"): str, vol.Optional("prodkey"): str, **DEVICE.schema})  # a manual add asks for the identity as well


class ZenMasterConfigFlow(ConfigFlow, domain=DOMAIN):
    """Single-instance flow with no fields: everything is configured in the panel; the vendor devices are subentries."""

    VERSION = 2

    def __init__(self) -> None:
        self._found: list[ConfigSubentryData] = []  # devices identified over their local API or Bluetooth before the entry existed

    def _add_found(self, found: dict[str, Any] | None) -> None:
        # a device that told us who it is becomes a subentry of the entry about to be created
        if found is None or (family := ZendureDriver.device_family(found)) is None or any(s["data"]["sn"] == found["sn"] for s in self._found):
            return
        title = unique_name(family, found["sn"], {s["title"] for s in self._found})
        self._found.append(ConfigSubentryData(data={"ip": "", "blemac": "", **found}, subentry_type=ZendureDriver.id, title=title, unique_id=f"{ZendureDriver.id}_{found['sn']}"))

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(PANEL_TITLE)
        self._abort_if_unique_id_configured()
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=vol.Schema({}))
        return self.async_create_entry(title=PANEL_TITLE, data={}, subentries=self._found)

    async def async_step_zeroconf(self, discovery_info: Any) -> ConfigFlowResult:
        # only before the entry exists: the service is asked who it is, so the new entry starts with that device; the driver
        # listens itself once the fleet runs
        if (match := ZendureDriver.match_zeroconf(discovery_info)) is None:
            return self.async_abort(reason="not_supported")
        await self.async_set_unique_id(PANEL_TITLE)
        self._abort_if_unique_id_configured()
        self._add_found(await ZendureDriver.probe(self.hass, match["ip"]))
        return await self.async_step_user()

    async def async_step_bluetooth(self, discovery_info: Any) -> ConfigFlowResult:
        # an advertisement before the entry exists: the device is asked who it is over Bluetooth
        await self.async_set_unique_id(PANEL_TITLE)
        self._abort_if_unique_id_configured()
        self._add_found(await ZendureDriver.probe_ble(self.hass, str(discovery_info.address)))
        return await self.async_step_user()

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        # only the device flow: a consumer or a solar panel is added, edited and removed in the panel itself
        return {ZendureDriver.id: DeviceSubentryFlow}


class DeviceSubentryFlow(ConfigSubentryFlow):
    # A manual add asks for the identity the driver needs, a reconfigure edits ip and address.
    # The name is computed once from the family and the serial, exactly like a discovered device, and never changes.

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        driver = ZendureDriver
        errors: dict = {}
        if user_input is not None:
            data = {"ip": "", "blemac": "", **user_input}
            entry = self._get_entry()
            if any(s.data.get("sn") == data["sn"] for s in entry.subentries.values()):
                errors["sn"] = "device_exists"
            elif (family := driver.device_family(data)) is None:
                errors["product"] = "unknown_product"
            else:
                title = unique_name(family, data["sn"], {s.title for s in entry.subentries.values()})
                return self.async_create_entry(title=title, data=data)
        return self.async_show_form(step_id="user", data_schema=self.add_suggested_values_to_schema(DEVICE_ADD, user_input or {}), errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        sub = self._get_reconfigure_subentry()
        if user_input is not None:
            self.hass.config_entries.async_update_subentry(self._get_entry(), sub, data={**sub.data, **user_input})
            return self.async_abort(reason="reconfigure_successful")
        return self.async_show_form(step_id="reconfigure", data_schema=self.add_suggested_values_to_schema(DEVICE, {k: v for k, v in sub.data.items() if v}), description_placeholders={"name": sub.title})
