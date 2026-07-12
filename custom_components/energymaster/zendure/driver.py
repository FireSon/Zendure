"""ZendureDriver: connects to MQTT, discovers devices, routes telemetry."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import random
import time
from datetime import datetime
from typing import Any, Callable

import paho.mqtt.client as mqtt
from aiohttp import ClientTimeout

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..device import DeviceConnection, EmDevice, PowerDevice
from .battery import ZendureBattery
from .device import ZendureDevice, _PRODKEY
from .smartmeter import ZendureSmartMeter

_LOGGER = logging.getLogger(__name__)

SF_HAKEY = "C*dafwArEOXK"

_SMARTMETER_PRODKEYS = {
    "c3yt68": "SmartMeter 3CT",
    "y6hvtw": "SmartMeter 3CT-S",
    "q331b1": "SmartMeter P1",
    "1dmcr8": "SmartPlug",
    "vv1wd7": "Smart CT",
}


class ZendureDriver:
    """Connects to MQTT, discovers devices, routes telemetry."""

    def __init__(self) -> None:
        # no-arg: created by Config.readlist as a polymorphic collection member; deps come via attach()
        self.id: str = ""
        self.apptoken: str = ""
        self.mqtt_host: str = ""
        self.mqtt_port: int = 1883
        self.mqtt_user: str = ""
        self.mqtt_psw: str = ""
        self.wifi_ssid: str = ""
        self.wifi_psw: str = ""
        self.hass: HomeAssistant | None = None
        self.devices: dict[str, ZendureDevice] = {}
        self.smartmeters: dict[str, ZendureSmartMeter] = {}
        self._all_devices: list[EmDevice] | None = None  # coordinator.devices (HA entity registration)
        self._power_devices: list[PowerDevice] | None = None  # coordinator.power_devices (HA entity registration)
        self._save_config: Callable[[], None] | None = None
        self._config: dict = {}
        self._mqtt_clients: dict[DeviceConnection, mqtt.Client] = {}
        self._cloud_names: dict[str, str] = {}
        self.on_update: Callable[[PowerDevice], None] | None = None
        self.on_register: Callable[[str, str], None] | None = None  # set by ws_register_token while a BLE-onboard waits

    def attach(self, hass: HomeAssistant, all_devices: list[EmDevice], power_devices: list[PowerDevice], on_update: Callable[[PowerDevice], None], save_config: Callable[[], None]) -> None:
        self.hass = hass
        self._all_devices = all_devices
        self._power_devices = power_devices
        self.on_update = on_update
        self._save_config = save_config

    def restore(self, item: dict) -> None:
        # machine-managed nested state from the persisted driver item (scalar fields are set by readlist)
        self._config = {
            "devices": item.get("devices", {}) or {},
            "smartmeters": item.get("smartmeters", {}) or {},
            "apikeys": item.get("apikeys", {}) or {},
            "cloud_url": item.get("cloud_url", ""),
        }

    def add_device(self, dev_id: str, payload: dict) -> None:
        self.devices[dev_id] = ZendureDevice(self.hass, payload.get("prodkey", ""), {**payload, "deviceid": dev_id}, self)

    def restore_devices(self, config: dict) -> None:
        """Create devices + smartmeters from config (sync, no MQTT); named entries join the LP, unnamed await acceptance."""
        self._config = config
        self._config.setdefault("devices", {})
        self._config.setdefault("smartmeters", {})

        # One-time migration: apikeys was a list of strings, now a dict keyed by apikey.
        apikeys = self._config.get("apikeys")
        if isinstance(apikeys, list):
            self._config["apikeys"] = {k: {"last_sync_ts": 0, "http_code": 0, "devices": []} for k in apikeys if isinstance(k, str) and k}
        elif apikeys is None:
            self._config["apikeys"] = {}

        for dev_id, dev_cfg in self._config["devices"].items():
            if dev_id in self.devices:
                continue  # already restored; recreating would duplicate entities and the LP fleet
            if prodkey := dev_cfg.get("prodkey", ""):
                d = ZendureDevice(self.hass, prodkey, {**dev_cfg, "deviceid": dev_id}, self)
                self.devices[dev_id] = d
                if d.name:
                    self._all_devices.append(d)
                    self._power_devices.append(d)

        for sm_id, sm_cfg in self._config["smartmeters"].items():
            if sm_id in self.smartmeters:
                continue
            sm = ZendureSmartMeter(self.hass, sm_id, sm_cfg.get("name", ""), model=sm_cfg.get("model_name", ""), prodkey=sm_cfg.get("prodkey", ""))
            self.smartmeters[sm_id] = sm
            if sm.name:
                self._all_devices.append(sm)

    async def connect(self) -> bool:
        """Build apikeys/mqtt from own fields + restored nested state, then connect to local MQTT."""
        cfg = self._config
        if self.apptoken:
            cfg.setdefault("apikeys", {}).setdefault(self.apptoken, {"last_sync_ts": 0, "http_code": 0, "devices": []})
        if self.mqtt_host:
            cfg["mqtt"] = {"host": self.mqtt_host, "port": self.mqtt_port, "username": self.mqtt_user, "password": self.mqtt_psw}
        self.restore_devices(cfg)
        mqtt_cfg = self._config.get("mqtt", {})
        if mqtt_cfg:
            self._add_mqtt_client(
                DeviceConnection.MQTT_LOCAL,
                mqtt_cfg.get("host", "localhost"),
                mqtt_cfg.get("port", 1883),
                mqtt_cfg.get("username", "zendure"),
                mqtt_cfg.get("password", "zendure"),
            )
        return True

    async def cloud_connect(self) -> None:
        """Fetch device list and MQTT credentials from Zendure cloud, per apikey.

        Each apikey's entry is updated with the raw cloud response (last_sync_ts,
        http_code, device list, optional error msg) so the UI can show what the cloud
        returned and why a device might have been dropped.
        """
        apikeys = self._config.get("apikeys") or {}
        for apikey, meta in list(apikeys.items()):
            meta = meta if isinstance(meta, dict) else {}
            try:
                base64_url = base64.b64decode(apikey).decode("utf-8")
                api_url, app_key = base64_url.rsplit(".", 1)

                timestamp = int(time.time())
                nonce = str(random.randint(10000, 99999))

                sign_params = {"appKey": app_key, "timestamp": timestamp, "nonce": nonce}
                body_str = "".join(f"{k}{v}" for k, v in sorted(sign_params.items()))
                sign = hashlib.sha1(f"{SF_HAKEY}{body_str}{SF_HAKEY}".encode()).hexdigest().upper()

                headers = {"Content-Type": "application/json", "timestamp": str(timestamp), "nonce": nonce, "clientid": "zenHa", "sign": sign}

                session = async_get_clientsession(self.hass)  # HA's shared session, never closed here
                async with session.post(
                    f"{api_url}/api/ha/deviceList",
                    json={"appKey": app_key},
                    headers=headers,
                    timeout=ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()

                code = data.get("code")
                meta.update({"last_sync_ts": int(time.time()), "http_code": code, "msg": data.get("msg", ""), "devices": []})

                if code == 200 and data.get("success"):
                    result = data["data"]
                    device_list = result.get("deviceList", []) or []
                    # Store the raw device list verbatim so the UI can show
                    # every device the cloud returned, including any we skip.
                    meta["devices"] = device_list
                    _LOGGER.info("Cloud: apikey %s... -> %d device(s), code=%s", apikey[:10], len(device_list), code)

                    for dev in device_list:
                        dev_id = dev.get("deviceKey", "")
                        prodkey = dev.get("productKey", "")  # keep real case for the command topic
                        if not dev_id:
                            continue
                        if prodkey.lower() not in _PRODKEY:
                            _LOGGER.warning("Cloud: skipping %s: unknown prodkey %s (model %s)", dev_id, prodkey, dev.get("productModel", ""))
                            continue
                        cloud_name = (dev.get("deviceName") or "").strip()
                        existing = self.devices.get(dev_id)
                        if existing is not None:
                            existing.prodkey = prodkey  # cloud is source of truth for the case-sensitive prodkey (command topic)
                            if not existing.name and cloud_name:
                                self._cloud_names[dev_id] = cloud_name
                                _LOGGER.info("Cloud: queued name %r for %s (effective on next reload)", cloud_name, dev_id)
                            continue
                        d = ZendureDevice(self.hass, prodkey, {"deviceid": dev_id, "name": cloud_name, "sn": dev.get("snNumber", "")}, self)
                        self.devices[dev_id] = d
                        if d.name:
                            # Named on discovery: join the optimizer + entity-registration lists now
                            # (mirrors restore_devices; entities are already registered at construction).
                            self._power_devices.append(d)
                            self._all_devices.append(d)
                            _LOGGER.info("Cloud: discovered %s as %r (%s)", dev_id, cloud_name, dev.get("productModel", ""))
                        else:
                            _LOGGER.info("Cloud: discovered %s (%s), awaiting acceptance", dev_id, dev.get("productModel", ""))

                    mqtt_cfg = result.get("mqtt", {})
                    if mqtt_cfg and mqtt_cfg.get("url") and self.devices:
                        url = mqtt_cfg["url"]
                        self._config["cloud_url"] = url
                        host, port = url.rsplit(":", 1) if ":" in url else (url, "1883")
                        self._add_mqtt_client(
                            DeviceConnection.MQTT_CLOUD,
                            host,
                            int(port),
                            mqtt_cfg.get("username", ""),
                            mqtt_cfg.get("password", ""),
                            mqtt_cfg.get("clientId"),
                        )
                else:
                    _LOGGER.warning("Cloud: apikey %s... rejected, code=%s msg=%s", apikey[:10], code, data.get("msg", ""))
            except Exception as err:
                meta.update({"last_sync_ts": int(time.time()), "http_code": 0, "msg": str(err)})
                _LOGGER.warning("Cloud API failed for key %s... (%s)", apikey[:10], err)

            apikeys[apikey] = meta
        self._config["apikeys"] = apikeys
        self._save_devices()

    async def disconnect(self) -> None:
        """Disconnect all MQTT clients."""
        for client in self._mqtt_clients.values():
            client.disconnect()
            client.loop_stop()
        self._mqtt_clients.clear()
        ZendureBattery._registry.clear()  # class-level registry must not hand out stale instances after a reload

    def _save_devices(self) -> None:
        """Snapshot devices and smartmeters into shared config and persist.

        Unnamed entries (name == "") are persisted too, they're the discovered-
        but-awaiting-acceptance state. Cloud-queued names from self._cloud_names
        are used as a fallback when the in-memory instance still has no name; this
        is what propagates a Zendure-deviceName backfill across a reload.
        """
        self._config["devices"] = {
            d_id: {
                "name": d.name or self._cloud_names.get(d_id, ""),
                "prodkey": d.prodkey,
                "model": d.zendure_model.name,
                "sn": d.sn,
                "ip": d.ip,
                "blemac": d.blemac,
                "phase": int(d.phase) if d.phase is not None else None,
            }
            for d_id, d in self.devices.items()
        }
        self._config["smartmeters"] = {
            sm_id: {
                "name": sm.name,
                "prodkey": getattr(sm, "prodkey", ""),
                "model_name": getattr(sm, "model", ""),
            }
            for sm_id, sm in self.smartmeters.items()
        }
        self._config.pop("pending", None)
        self._save_config()

    def _add_mqtt_client(self, conn: DeviceConnection, host: str, port: int, username: str, password: str, client_id: str | None = None) -> None:
        if conn in self._mqtt_clients:
            return  # already connected; a config-save reload must not stack a second client
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id, userdata=conn)
            client.on_connect = self._on_mqtt_connect
            client.on_disconnect = self._on_mqtt_disconnect
            client.on_message = self._on_mqtt_message
            client.username_pw_set(username, password)
            client.connect_async(host, port)
            client.loop_start()
            self._mqtt_clients[conn] = client
            _LOGGER.info("MQTT connecting to %s:%s", host, port)
        except (ConnectionRefusedError, OSError):
            _LOGGER.warning("MQTT connection failed to %s:%s", host, port)

    def _on_mqtt_connect(self, client: Any, userdata: Any, _flags: Any, rc: Any, _props: Any) -> None:
        _LOGGER.info("MQTT [%s] connected, rc=%s", userdata, rc)
        if userdata == DeviceConnection.MQTT_LOCAL:
            client.subscribe("/#")  # local broker: everything, discovery needs it
        else:
            for d in self.devices.values():  # cloud broker is multi-tenant: only our own devices
                client.subscribe(f"/{d.prodkey}/{d.device_id}/#")

    def _on_mqtt_disconnect(self, client: Any, userdata: Any, _flags: Any, rc: Any, _props: Any) -> None:
        _LOGGER.info("MQTT [%s] disconnected, rc=%s", userdata, rc)

    def _on_mqtt_message(self, client: Any, userdata: Any, msg: Any) -> None:
        """Paho callback (MQTT thread): hop onto the HA event loop.

        All downstream work (device/entity state mutation, hass.async_create_task)
        must run on the loop thread; paho runs this on its own network thread.
        """
        try:
            if msg.payload is None or not msg.payload:
                return
            asyncio.run_coroutine_threadsafe(self._handle_message(client, userdata, msg), self.hass.loop)
        except RuntimeError:
            # Loop closed during shutdown, drop the message silently.
            _LOGGER.exception("Unexpected error in MQTT on_message")

    async def _handle_message(self, client: Any, conn: DeviceConnection, msg: Any) -> None:
        """Route incoming MQTT telemetry to the correct device or smart meter.

        Runs on the HA event loop (bounced from the paho thread by _on_mqtt_message).

        Discovery filters:
          - Auto-discovery of new devices/smartmeters only on the "local" MQTT
            broker (cloud MQTT is multi-tenant; we receive other users' topics).
          - Battery auto-discovery additionally requires the device_id to appear
            in some apikey's cloud-returned device list (defense in depth).
          - Telemetry for already-known devices is accepted from any broker.
        """
        is_local = conn == DeviceConnection.MQTT_LOCAL
        try:
            topics = msg.topic.split("/", 3)
            if len(topics) < 4:
                return

            deviceId = topics[2]
            prodkey = topics[1]  # keep the real case: the command topic is case-sensitive; lookups lowercase

            # Smart meter?
            if prodkey.lower() in _SMARTMETER_PRODKEYS:
                sm = self.smartmeters.get(deviceId)
                if sm is None:
                    if not is_local:
                        return  # Only local MQTT can discover new smartmeters.
                    model_name = _SMARTMETER_PRODKEYS[prodkey.lower()]
                    sm = ZendureSmartMeter(self.hass, deviceId, "", model=model_name, prodkey=prodkey)
                    self.smartmeters[deviceId] = sm
                    self._save_devices()
                    _LOGGER.info("Discovered smart meter %s (%s), awaiting acceptance", deviceId, model_name)
                if not sm.name:
                    # Unnamed: no entities, no distribution link, drop telemetry.
                    return
                if topics[3] == "properties/report":
                    sm.update_properties(json.loads(msg.payload.decode("utf-8", "ignore")), time.time())
                return

            # Battery device
            d = self.devices.get(deviceId)
            if d is None:
                # Exit is we are not local, or no serial number is in the payload
                if not is_local or b'"log":' not in msg.payload or b'"sn":' not in msg.payload:
                    return
                sn = json.loads(msg.payload.decode("utf-8", "ignore")).get("log").get("sn")
                d = ZendureDevice(self.hass, prodkey, {"deviceid": deviceId, "sn": sn}, self)
                self.devices[deviceId] = d
                d.start_task()
                self._save_devices()
                _LOGGER.info("Discovered device %s (%s), awaiting acceptance", deviceId, prodkey)
            if d._mqtt is None:
                d._mqtt = client  # a device lives on exactly one broker: first contact decides, no flapping

            match topics[3]:
                case "properties/report":
                    d.update_properties(json.loads(msg.payload.decode("utf-8", "ignore")), time.time())
                    d.connection.update_value(conn)

                case "function/invoke/reply":
                    payload = json.loads(msg.payload.decode("utf-8", "ignore"))
                    if payload.get("messageId") == d._cmd_msg_id:
                        d.command_reply()  # device confirms receipt of the last power command

                case "register":
                    payload = json.loads(msg.payload.decode("utf-8", "ignore"))
                    _LOGGER.info("MQTT register van %s: %s", deviceId, payload)
                    if (params := payload.get("params")) is not None and (token := params.get("token")) is not None:
                        reply = {"messageId": payload.get("messageId", "123"), "params": {"token": token, "result": 0}}
                        client.publish(f"iot{msg.topic}/replay", json.dumps(reply))
                        _LOGGER.info("MQTT register: reply gestuurd voor %s, token=%s, register-venster actief=%s", deviceId, token, self.on_register is not None)
                        if self.on_register is not None:
                            self.on_register(deviceId, token)

                case "time-sync":
                    payload = json.loads(msg.payload.decode("utf-8", "ignore"))
                    z = datetime.now().astimezone().strftime("%z")
                    reply = json.dumps({"zoneOffset": f"{z[:3]}:{z[3:]}", "timestamp": int(time.time()), "messageId": payload.get("messageId", "none")})
                    client.publish(f"iot{msg.topic}/reply", reply)

        except Exception as ex:
            _LOGGER.warning("Unexpected error in MQTT message handler: %s", ex)
