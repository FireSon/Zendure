from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging

import voluptuous as vol

from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.auth.providers import homeassistant as auth_ha
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .config import EDIT_TYPE
from .const import DOMAIN, SchemaAction

_LOGGER = logging.getLogger(__name__)


def _coordinator(hass: HomeAssistant):
    # The single Coordinator entry (has .solver). None before setup.
    for value in hass.data.get(DOMAIN, {}).values():
        if hasattr(value, "solver"):
            return value
    return None


_EDIT_TYPE_NAME = {
    EDIT_TYPE.int: "int", EDIT_TYPE.float: "double", EDIT_TYPE.string: "string",
    EDIT_TYPE.bool: "boolean", EDIT_TYPE.entity: "entity", EDIT_TYPE.dropdown: "dropdown",
    EDIT_TYPE.command: "command", EDIT_TYPE.instance: "list", EDIT_TYPE.list: "list",
    EDIT_TYPE.listpoly: "list", EDIT_TYPE.phase: "dropdown", EDIT_TYPE.device: "device",
}

_PHASE_OPTIONS = {"0": "Alle", "1": "L1", "2": "L2", "3": "L3"}


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/get_schema"})
@websocket_api.async_response
async def ws_get_schema(hass, connection, msg):
    # UI-keyed field definitions (with edit_type) + current values, all from the live coordinator.
    coord = _coordinator(hass)
    if coord is None:
        connection.send_result(msg["id"], {"schema": {}, "config": {}})
        return
    schema = {
        key: [
            {
                **dataclasses.asdict(f), "type": int(f.type), "edit_type": _EDIT_TYPE_NAME.get(f.type, "string"),
                "options": f.options or (_PHASE_OPTIONS if f.type == EDIT_TYPE.phase else None),
            }
            for f in fields
        ]
        for key, fields in coord.schema().items()
    }
    connection.send_result(msg["id"], {"schema": schema, "config": coord.serialize()})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/get_plan"})
@websocket_api.async_response
async def ws_get_plan(hass, connection, msg):
    coord = _coordinator(hass)
    if coord is None or coord.solver is None:
        connection.send_error(msg["id"], "not_ready", "EnergyMaster plan not ready yet")
        return
    connection.send_result(msg["id"], coord.solver.plan())


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/save_schema", vol.Required("action"): vol.In([a.value for a in SchemaAction]),
    vol.Required("section"): str, vol.Optional("payload"): dict,
})
@websocket_api.async_response
async def ws_save_schema(hass, connection, msg):
    coord = _coordinator(hass)
    if coord is None:
        connection.send_error(msg["id"], "not_ready", "EnergyMaster not ready yet")
        return
    await coord.save_schema(msg["action"], msg["section"], msg.get("payload"))
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/register_token", vol.Required("device_id"): str, vol.Optional("timeout"): vol.Coerce(float)})
@websocket_api.async_response
async def ws_register_token(hass, connection, msg):
    coord = _coordinator(hass)
    if coord is None:
        connection.send_error(msg["id"], "not_ready", "EnergyMaster not ready yet")
        return
    driver = next((d for d in coord.drivers if hasattr(d, "on_register")), None)
    if driver is None:
        connection.send_error(msg["id"], "not_ready", "No driver configured")
        return
    device_id = msg["device_id"]
    timeout = msg.get("timeout", 55.0)
    _LOGGER.info("register_token: start voor %s (timeout %ss)", device_id, timeout)
    # Local HA-user for the device so it can authenticate to the (HA-auth) MQTT broker.
    try:
        psw = hashlib.md5(device_id.encode()).hexdigest().upper()[8:24]
        provider = auth_ha.async_get_provider(hass)
        creds = await provider.async_get_or_create_credentials({"username": device_id.lower()})
        if await hass.auth.async_get_user_by_credentials(creds) is None:
            user = await hass.auth.async_create_user(device_id, group_ids=[GROUP_ID_USER], local_only=True)
            await provider.async_add_auth(device_id.lower(), psw)
            await hass.auth.async_link_user(user, creds)
            _LOGGER.info("register_token: MQTT-user %s aangemaakt", device_id.lower())
        else:
            await provider.async_change_password(device_id.lower(), psw)
            _LOGGER.info("register_token: MQTT-user %s wachtwoord bijgewerkt", device_id.lower())
    except Exception:
        _LOGGER.warning("register_token: MQTT-user toevoegen mislukt voor %s", device_id)
    fut = hass.loop.create_future()

    def on_register(did, token):
        _LOGGER.info("register_token: register-melding van %s, token=%s", did, token)
        if did == device_id and not fut.done():
            fut.set_result(token)

    driver.on_register = on_register
    _LOGGER.info("register_token: wachten op register-melding van %s…", device_id)
    try:
        token = await asyncio.wait_for(fut, timeout)
    except (TimeoutError, asyncio.TimeoutError):
        token = None
        _LOGGER.warning("register_token: timeout, geen register van %s binnen %ss", device_id, timeout)
    finally:
        driver.on_register = None
    _LOGGER.info("register_token: resultaat voor %s -> token=%s", device_id, token)
    connection.send_result(msg["id"], {"token": token})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/command", vol.Required("device_id"): str, vol.Required("action"): str})
@websocket_api.async_response
async def ws_command(hass, connection, msg):
    coord = _coordinator(hass)
    if coord is None:
        connection.send_error(msg["id"], "not_ready", "EnergyMaster not ready yet")
        return
    consumer = next((c for c in coord.consumers if getattr(c, "id", None) == msg["device_id"]), None)
    if consumer is None or not hasattr(consumer, "command"):
        connection.send_error(msg["id"], "not_found", f"no consumer {msg['device_id']}")
        return
    await consumer.command(hass, msg["action"])
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/subscribe_plan"})
@callback
def ws_subscribe_plan(hass, connection, msg):
    # push: re-emit a "plan updated" event whenever the coordinator finishes a solve
    @callback
    def forward() -> None:
        try:
            connection.send_message(websocket_api.event_message(msg["id"], {}))
        except Exception:  # connection may already be closing; drop the notification
            pass

    connection.subscriptions[msg["id"]] = async_dispatcher_connect(hass, f"{DOMAIN}_plan", forward)
    connection.send_result(msg["id"])


def async_register_ws(hass: HomeAssistant) -> None:
    if hass.data.get(f"{DOMAIN}_ws"):
        return
    hass.data[f"{DOMAIN}_ws"] = True
    for handler in (ws_get_schema, ws_get_plan, ws_save_schema, ws_register_token, ws_command, ws_subscribe_plan):
        websocket_api.async_register_command(hass, handler)
