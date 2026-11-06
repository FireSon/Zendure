from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta
from types import MappingProxyType

import voluptuous as vol

from uuid import uuid4

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SchemaAction
from .coordinator import SUBENTRY_TYPES, title_taken
from .solver.forecast import SolarPanel


HISTORY_DAYS = 30  # calendar days in the trend view


def _coordinator(hass: HomeAssistant):
    # The single Coordinator entry (has .distribution). None before setup.
    for value in hass.data.get(DOMAIN, {}).values():
        if hasattr(value, "distribution"):
            return value
    return None


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/get_schema"})
@websocket_api.async_response
async def ws_get_schema(hass, connection, msg):
    # UI-keyed field definitions (with their HA selector) + current values, all from the live coordinator.
    coord = _coordinator(hass)
    if coord is None:
        connection.send_result(msg["id"], {"schema": {}, "config": {}})
        return
    schema: dict = {}
    config: dict = {}
    for device in coord.devices:
        coord.config.serialize_poly(device, config, schema)

    # the settings tab: a hand-picked list, not a collection the coordinator keeps
    for item in (coord.distribution, coord.zendure, coord.prices):
        coord.config.serialize_poly(item, config, schema)

    # consumers and solar panels: keyed on their subentry id, named after their subentry title
    types = {cls: name for name, cls in SUBENTRY_TYPES.items()}
    config["consumers"] = []
    actions: dict = {}
    tiles: dict = {}
    for c in coord.consumers:
        item = {"type": types[type(c)], "name": c.name}
        coord.config.serialize(c, payload=item)  # its fields, plus the entity_id of its state sensor
        config["consumers"].append(item)
        schema[c.id] = [{k: v for k, v in asdict(f).items() if k != "cast"} for f in type(c).CONFIG]
        actions[c.id] = [asdict(a) for a in type(c).ACTIONS]  # the row buttons this consumer offers
        tiles[c.id] = type(c).TILES  # the entity fields its row shows as tiles
    fields = [{k: v for k, v in asdict(f).items() if k != "cast"} for f in SolarPanel.CONFIG]
    config["solar_panels"] = []
    for row in coord.manager_list("solar_panels"):
        config["solar_panels"].append({"id": row["id"], "name": row.get("name", ""), "power_entity": row.get("power_entity", ""), "derate_entity": row.get("derate_entity", "")})
        schema[f"solar_panel:{row['id']}"] = fields

    connection.send_result(msg["id"], {"schema": schema, "config": config, "actions": actions, "tiles": tiles})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/get_plan"})
@websocket_api.async_response
async def ws_get_plan(hass, connection, msg):
    coord = _coordinator(hass)
    if coord is None:
        connection.send_error(msg["id"], "not_ready", "ZenMaster plan not ready yet")
        return
    connection.send_result(msg["id"], coord.solver.plan())


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


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/save_schema", vol.Required("action"): vol.In([a.value for a in SchemaAction]),
    vol.Required("section"): str, vol.Optional("payload"): dict,
})
@websocket_api.async_response
async def ws_save_schema(hass, connection, msg):
    # the settings tab: distribution, Zendure driver and prices. Consumers and solar panels go through the subentry commands.
    coord = _coordinator(hass)
    if coord is None:
        connection.send_error(msg["id"], "not_ready", "ZenMaster not ready yet")
        return
    if (device := coord.config.deserialize_poly([*coord.devices, coord.zendure, coord.prices], msg.get("payload") or {})) is None:
        connection.send_error(msg["id"], "not_found", msg["section"])
        return
    if msg["action"] != SchemaAction.edit:
        connection.send_error(msg["id"], "invalid", "edit only; consumers and solar groups go through their subentry")
        return
    coord.config.serialize(device, msg["section"], coord.config.payload)
    await coord.config.save()
    if device is coord.zendure:
        await coord.zendure.connect()
    if device is coord.prices and (otp := str((msg.get("payload") or {}).get("otp") or "")):
        hass.async_create_task(coord.prices.authorise(hass, otp))  # the code rides along with this save and is never stored
    coord.rebuild_solver()  # a changed setting only reaches the LP through a rebuild
    if device is coord.distribution and coord.distribution.p1_device:
        coord._track_p1(coord.distribution.p1_device)  # a different P1 meter is read from now on
    hass.async_create_task(coord._tick())
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/subentry_add", vol.Required("subentry_type"): str, vol.Required("title"): str, vol.Optional("data"): dict})
@websocket_api.async_response
async def ws_subentry_add(hass, connection, msg):
    # the panel's "add": one subentry of the given type; the coordinator then builds the consumer or solar panel from it
    coord = _coordinator(hass)
    if coord is None or coord.entry is None:
        connection.send_error(msg["id"], "not_ready", "ZenMaster not ready yet")
        return
    if msg["subentry_type"] not in SUBENTRY_TYPES and msg["subentry_type"] != "solar_panel":
        connection.send_error(msg["id"], "not_found", msg["subentry_type"])
        return
    title = msg["title"].strip()
    if not title or title_taken(coord.entry, title):
        connection.send_error(msg["id"], "invalid", "name already exists" if title else "name missing")
        return

    # a solar group has no device, so it is a row in the manager list instead of a subentry of its own
    if msg["subentry_type"] == "solar_panel":
        rows = coord.manager_list("solar_panels")
        row = {"id": uuid4().hex, "name": title, **(msg.get("data") or {})}
        coord.manager_save("solar_panels", [*rows, row])
        await coord.resync()
        connection.send_result(msg["id"], {"ok": True, "subentry_id": row["id"]})
        return
    sub = ConfigSubentry(data=MappingProxyType(msg.get("data") or {}), subentry_type=msg["subentry_type"], title=title, unique_id=None)
    hass.config_entries.async_add_subentry(coord.entry, sub)
    await coord.resync()
    connection.send_result(msg["id"], {"ok": True, "subentry_id": sub.subentry_id})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/subentry_update", vol.Required("subentry_id"): str, vol.Required("patch"): dict})
@websocket_api.async_response
async def ws_subentry_update(hass, connection, msg):
    # the panel's "save" on a consumer or solar panel: "name" is the title, the other fields merge into the subentry data
    coord = _coordinator(hass)
    if coord is not None:
        rows = coord.manager_list("solar_panels")
        if (row := next((r for r in rows if r["id"] == msg["subentry_id"]), None)) is not None:
            patch = {k: v for k, v in msg["patch"].items() if k != "id"}
            coord.manager_save("solar_panels", [{**r, **patch} if r is row else r for r in rows])
            await coord.resync()
            connection.send_result(msg["id"], {"ok": True})
            return
    sub = coord.entry.subentries.get(msg["subentry_id"]) if coord is not None and coord.entry is not None else None
    if sub is None:
        connection.send_error(msg["id"], "not_found", msg["subentry_id"])
        return
    patch = dict(msg["patch"])
    patch.pop("id", None)  # the key field the form carries along; the subentry id is the identity already
    title = str(patch.pop("name", sub.title)).strip()
    if not title or title_taken(coord.entry, title, sub.subentry_id):
        connection.send_error(msg["id"], "invalid", "name already exists" if title else "name missing")
        return
    hass.config_entries.async_update_subentry(coord.entry, sub, data={**sub.data, **patch}, title=title)
    await coord.resync()
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/subentry_delete", vol.Required("subentry_id"): str})
@websocket_api.async_response
async def ws_subentry_delete(hass, connection, msg):
    # HA removes the subentry's device and entities along with it; the coordinator drops the consumer or panel
    coord = _coordinator(hass)
    if coord is not None:
        rows = coord.manager_list("solar_panels")
        if any(r["id"] == msg["subentry_id"] for r in rows):
            coord.manager_save("solar_panels", [r for r in rows if r["id"] != msg["subentry_id"]])
            await coord.resync()
            connection.send_result(msg["id"], {"ok": True})
            return
    if coord is None or coord.entry is None or msg["subentry_id"] not in coord.entry.subentries:
        connection.send_error(msg["id"], "not_found", msg["subentry_id"])
        return
    hass.config_entries.async_remove_subentry(coord.entry, msg["subentry_id"])
    await coord.resync()
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/consumer_action", vol.Required("consumer"): str, vol.Required("action"): str})
@websocket_api.async_response
async def ws_consumer_action(hass, connection, msg):
    # the action buttons on a consumer row: start, stop, skip, and the arm_off/arm_on that hands it back
    coord = _coordinator(hass)
    consumer = next((c for c in coord.consumers if c.id == msg["consumer"]), None) if coord else None
    if consumer is None or not hasattr(consumer, "command"):
        connection.send_error(msg["id"], "not_found", msg["consumer"])
        return
    await coord.consumer_command(consumer, msg["action"])  # the same path the button entities take
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/price_auth"})
@websocket_api.async_response
async def ws_price_auth(hass, connection, msg):
    # the "vraag code aan" button on the price card; the patch travels back so the card can show the code field
    coord = _coordinator(hass)
    if coord is None:
        connection.send_error(msg["id"], "not_found", "coordinator")
        return
    await coord.prices.request_code(hass)
    connection.send_result(msg["id"], {"ok": coord.prices.needcode, "patch": {"needcode": coord.prices.needcode}})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/get_history"})
@websocket_api.async_response
async def ws_get_history(hass, connection, msg):
    # Realised history per calendar day, from the recorder: the kWh aggregators give their daily delta,
    # the savings sensor its daily gain, and savings_today its own first/last hour (forecast vs fact).
    coord = _coordinator(hass)
    if coord is None:
        connection.send_error(msg["id"], "not_ready", "ZenMaster not ready yet")
        return
    dist = coord.distribution
    ids = {key: getattr(dist.entities.get(key), "entity_id", None) for key in
           ("discharge_power_aggr", "charge_power_aggr", "grid_import_aggr", "grid_export_aggr", "savings", "savings_today")}
    panels = {p.power_entity for p in coord.forecast.solar_panels if p.power_entity}
    wanted = {e for e in ids.values() if e} | panels
    if not wanted:
        connection.send_result(msg["id"], {"days": []})
        return
    # the day and hour statistics of the window, then one row per calendar day from them
    daily, hourly = await _history_stats(hass, ids, wanted)
    connection.send_result(msg["id"], {"days": _history_days(daily, hourly, ids, panels)})


async def _history_stats(hass, ids, wanted):
    # the day statistics of every wanted entity, and the hours of savings_today for its first and last value of a day
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period

    end = dt_util.now()
    start = (end - timedelta(days=HISTORY_DAYS)).replace(hour=0, minute=0, second=0, microsecond=0)
    daily = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, end, wanted, "day", None, {"sum", "state", "mean"}
    )
    hourly = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, end, {ids["savings_today"]} if ids["savings_today"] else set(), "hour", None, {"mean"}
    )
    return daily, hourly


def _history_days(daily, hourly, ids, panels):
    # per calendar day the solar, battery and grid energy, the autarky, the savings and how well the forecast held
    def day_of(row):
        st = row.get("start")
        stamp = dt_util.utc_from_timestamp(st) if isinstance(st, (int, float)) else st
        return dt_util.as_local(stamp).date()

    def gains(entity, field):
        # a monotone counter: the gain of a day is the step to the next day
        by_day = {}
        for row in daily.get(entity, []):
            if (value := row.get(field)) is not None:
                by_day[day_of(row)] = float(value)
        days = sorted(by_day)
        return {d: max(0.0, by_day[n] - by_day[d]) for d, n in zip(days, days[1:])}

    solar = {}
    for entity in panels:  # power in W: the day mean over 24 h is the day total in kWh
        for row in daily.get(entity, []):
            if (value := row.get("mean")) is not None:
                solar[day_of(row)] = solar.get(day_of(row), 0.0) + float(value) * 24 / 1000.0
    accu = gains(ids["discharge_power_aggr"], "sum")
    charge = gains(ids["charge_power_aggr"], "sum")
    net = gains(ids["grid_import_aggr"], "sum")
    export = gains(ids["grid_export_aggr"], "sum")
    savings = gains(ids["savings"], "state")

    first_last = {}
    for row in hourly.get(ids["savings_today"] or "", []):
        if (value := row.get("mean")) is None:
            continue
        slot = first_last.setdefault(day_of(row), [None, None])
        if slot[0] is None:
            slot[0] = float(value)
        slot[1] = float(value)

    out = []
    for day in sorted(set(solar) | set(accu) | set(net) | set(savings)):
        imported = net.get(day, 0.0)
        # energy balance: what the house used is what came in minus what left again
        consumed = solar.get(day, 0.0) + imported + accu.get(day, 0.0) - export.get(day, 0.0) - charge.get(day, 0.0)
        planned, realised = first_last.get(day, (None, None))
        out.append({
            "day": day.isoformat(),
            "solar": round(solar.get(day, 0.0), 1),
            "accu": round(accu.get(day, 0.0), 1),
            "net": round(imported, 1),
            "autarky": round(max(0.0, min(100.0, (consumed - imported) / consumed * 100)), 0) if consumed > 0 else None,
            "savings": round(savings.get(day, 0.0), 2),
            "accuracy": round(min(200.0, abs(realised / planned) * 100), 0) if planned else None,
        })
    return out[-HISTORY_DAYS:]


def async_register_ws(hass: HomeAssistant) -> None:
    if hass.data.get(f"{DOMAIN}_ws"):
        return
    hass.data[f"{DOMAIN}_ws"] = True
    for handler in (ws_get_schema, ws_get_plan, ws_get_history, ws_price_auth, ws_consumer_action, ws_save_schema, ws_subentry_add, ws_subentry_update, ws_subentry_delete, ws_subscribe_plan):
        websocket_api.async_register_command(hass, handler)
