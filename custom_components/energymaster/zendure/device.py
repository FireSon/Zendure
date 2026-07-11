"""ZendureDevice, DistributionDevice subclass with MQTT/HTTP/BLE control.

Also contains: ZendureModel enum, PROFILES matrix, _PRODKEY lookup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from enum import Enum

import paho.mqtt.client as mqtt
from aiohttp import ClientSession, ClientTimeout
from bleak import BleakClient

from homeassistant.core import HomeAssistant


from ..const import Phase
from ..device import DeviceConnection, PowerDevice, PowerProfile, PowerProfileCharge, PowerProfileDischarge
from .battery import ZendureBattery
from ..binary_sensor import EmBinarySensor
from ..number import EmNumber
from ..sensor import EmSensor

_LOGGER = logging.getLogger(__name__)

SF_COMMAND_CHAR = "0000c304-0000-1000-8000-00805f9b34fb"
SF_READ_CHAR = "0000c305-0000-1000-8000-00805f9b34fb"
SF_HTTP_HEADER = {"content-type": "application/json; charset=UTF-8"}
SF_HTTP_TIMEOUT = ClientTimeout(total=4)


# Zendure models and profiles


class ZendureModel(Enum):
    UNKNOWN = 0
    SF2400AC = 1
    SF2400AC_PLUS = 2
    SF2400PRO = 3
    SF1600AC = 4
    SF1600AC_PLUS = 5
    SF800 = 6
    SF800PRO = 7
    SF800PLUS = 8
    HUB1200 = 9
    HUB2000 = 10
    HYPER2000 = 11
    ACE1500 = 12


_BLEHTTP = [
    '{"properties":{"smartMode":0,"acMode":2,"outputLimit":0,"inputLimit":0}}',
    '{"properties":{"smartMode":1,"acMode":1,"outputLimit":0,"inputLimit":%i}}',
    '{"properties":{"smartMode":1,"acMode":2,"outputLimit":%i,"inputLimit":0}}',
]

_HEMSEP = (
    (10.0, 4.0, 0, 3.0, 0.7, 0.3),
    "function/invoke",
    '{"function":"hemsEP","arguments":{"outputPower":0,"chargeState":0,"chargePower":0,"mode":0,"freq":0}}',
    '{"function":"hemsEP","arguments":{"outputPower":0,"chargeState":1,"chargePower":%i,"mode":9,"freq":0}}',
    '{"function":"hemsEP","arguments":{"outputPower":%i,"chargeState":0,"chargePower":0,"mode":9,"freq":0}}',
)

_HYPER = (
    (12.0, 5.0, 200.0, 3.0, 0.7, 0.3),
    "function/invoke",
    '{"function":"deviceAutomation","arguments":[{"autoModelProgram":0,"autoModel":0,"msgType":1,"autoModelValue":{"chargingType":0,"chargingPower":0,"freq":0,"outPower":0}}]}',
    '{"function":"deviceAutomation","arguments":[{"autoModelProgram":1,"autoModel":8,"msgType":1,"autoModelValue":{"chargingType":1,"price":2,"chargingPower":%i,"prices":[1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1],"outPower":0,"freq":0}}]}',
    '{"function":"deviceAutomation","arguments":[{"autoModelProgram":2,"autoModel":8,"msgType":1,"autoModelValue":{"chargingType":0,"chargingPower":0,"freq":0,"outPower":%i}}]}',
)

PROFILES = {
    ZendureModel.SF2400AC: (800, -2400, 0, _HEMSEP, ["bc8b7f"]),
    ZendureModel.SF2400AC_PLUS: (800, -2400, 0, _HEMSEP, ["5fg27j"]),
    ZendureModel.SF2400PRO: (800, -2400, 0, _HEMSEP, ["2qe7c9"]),
    ZendureModel.SF1600AC: (800, -1600, 0, _HEMSEP, []),
    ZendureModel.SF1600AC_PLUS: (800, -1600, 0, _HEMSEP, ["65174u"]),
    ZendureModel.SF800: (800, -1000, 0, _HEMSEP, ["a4ss5p", "b1nhmc", "n8sky9"]),
    ZendureModel.SF800PRO: (800, -1000, -1200, _HEMSEP, ["r3mn8u"]),
    ZendureModel.SF800PLUS: (800, -800, 0, _HEMSEP, ["8n77v3"]),
    ZendureModel.HUB1200: (800, -1200, 0, _HEMSEP, ["73bktv"]),
    ZendureModel.HUB2000: (800, -2000, 0, _HEMSEP, ["a8yh63"]),
    ZendureModel.HYPER2000: (800, -1200, -1600, _HYPER, ["gda3tb", "b3dxda", "ja72u0"]),
    ZendureModel.ACE1500: (800, -1500, 0, _HEMSEP, ["8bm93h"]),
}

_PRODKEY = {key: (model, profile) for model, profile in ((m, PROFILES[m]) for m in PROFILES) for key in profile[4]}


# ZendureDevice


class ZendureDevice(PowerDevice):
    """Zendure battery device with MQTT/HTTP/BLE communication."""

    def __init__(self, hass: HomeAssistant, prodkey: str, config: dict, driver) -> None:
        key = prodkey.lower()  # identification is case-insensitive; self.prodkey keeps the real case for the command topic
        if key not in _PRODKEY:
            _LOGGER.warning("Unknown prodkey: %s (device %s)", prodkey, config.get("deviceid", ""))
            model = ZendureModel.UNKNOWN
            topic, offtpl, ctpl, dtpl = "", "", "", ""
            discharge, charge = PowerProfileDischarge(), PowerProfileCharge()
        else:
            model, (pwr_max, pwr_min, pwr_solar, (timing, topic, offtpl, ctpl, dtpl), _) = _PRODKEY[key]
            discharge = PowerProfileDischarge(pwr_max, 0, *timing)
            charge = PowerProfileCharge(pwr_min, pwr_solar, *timing)

        self.sn: str = config.get("sn") or ""
        self.ble_name: str = ""
        sn = self.sn
        if model.name.startswith("SF") and len(sn) >= 4:
            self.ble_name = f"Zen{sn[0]}{sn[2]}{sn[3]}_{sn[-1]}"  # SolarFlow
        elif len(sn) >= 3:
            self.ble_name = f"Zen{sn[0]}{sn[2]}_{sn[-2:]}"  # Hyper e.a.
        name = self.ble_name.replace("Zen", model.name + "_")

        super().__init__(
            hass,
            config.get("deviceid", ""),
            name,
            capacity_kwh=config.get("capacity_kwh", 0),
            discharge=discharge,
            charge=charge,
            model=model.name,
            manufacturer="Zendure",
            phase=Phase(config.get("phase")) if config.get("phase") not in (None, "") else None,
        )
        self.zendure_model = model
        self._cmd_topic = topic
        self._cmd_power = [offtpl, ctpl, dtpl]
        self.prodkey = prodkey
        self.ip: str | None = config.get("ip") or None
        self.blemac: str | None = config.get("blemac") or None
        self._mqtt: mqtt.Client | None = None
        self._ble_client: BleakClient | None = None
        self._ble_response_event = asyncio.Event()
        self._ble_last_response: dict = {}
        self._ble_response_method: str = ""
        self._msg_id: int = 100000
        self._driver = driver
        self.batteries: dict[str, ZendureBattery | None] = {}
        self.on_update = driver.on_update

    def device_entities(self):
        super().device_entities()  # all slots default to EmEntity.Empty

        self.soc = EmSensor(id="electricLevel", uom="%", device_class="battery", state_class="measurement")
        self.soc_min = EmNumber(id="minSoc", uom="%", minimum=0, maximum=100, factor=0.1, on_write=self.entity_write, restore=True)
        self.soc_max = EmNumber(id="socSet", uom="%", minimum=0, maximum=100, factor=0.1, on_write=self.entity_write, restore=True)

        self.home_input = EmSensor(id="gridInputPower", uom="W", device_class="power", state_class="measurement", aggr=True)
        self.home_output = EmSensor(id="outputHomePower", uom="W", device_class="power", state_class="measurement", aggr=True)
        self.battery_input = EmSensor(id="outputPackPower", uom="W", device_class="power", state_class="measurement", aggr=True)
        self.battery_output = EmSensor(id="packInputPower", uom="W", device_class="power", state_class="measurement", aggr=True)
        self.solar = EmSensor(id="solarInputPower", uom="W", device_class="power", state_class="measurement", icon="mdi:solar-panel", aggr=True)
        self.temp = EmSensor(id="hyperTmp", uom="°C", device_class="temperature", state_class="measurement", convert=lambda v: float(v) * 0.01)
        self.offgrid = EmSensor(id="offgrid", uom="W", device_class="power", state_class="measurement", aggr=True)

        self.bypass = EmBinarySensor(id="pass")
        self.heat_state = EmBinarySensor(id="heatState")
        self.hems_state = EmBinarySensor(id="hemsState")

        self.available_kwh = EmSensor(id="available_kwh", uom="kWh", device_class="energy_storage", state_class="measurement")
        self.total_kwh = EmSensor(id="total_kwh", uom="kWh", device_class="energy")
        self.remaining_time = EmSensor(id="remainingTime", uom="h", device_class="duration", state_class="measurement")
        # device_efficiency is computed locally from charge/discharge aggregates, no MQTT alias.
        # self.device_efficiency = EmSensor(uom="%", state_class="measurement", precision=1)
        # self.device_efficiency._state = self._default_efficiency * 100  # direct seed; runs before register

    def entity_write(self, entity, value):
        self.mqtt_publish("properties/write", {"properties": {entity.id: value}})

    def update_properties(self, payload: dict, now: float) -> None:
        """Translate Zendure MQTT keys to generic entity keys, then route."""
        if batprops := payload.get("packData", None):
            for bp in batprops:
                if (sn := bp.pop("sn", None)) is not None:
                    if (bat := self.batteries.get(sn, None)) is None:
                        bat = ZendureBattery._registry.get(sn) or ZendureBattery(self.hass, sn, self)
                        self.batteries[sn] = bat
                        self.capacity_kwh = sum(0 if b is None else b.kWh for b in self.batteries.values())
                        self.on_update(self)
                    if bat and bp:
                        bat.update_properties(bp, now)

        for key, value in payload.items():
            if key == "properties":
                for k, v in value.items():
                    self.entity_update(k, v)
            elif key in ["ip", "blemac", "sn"] and not getattr(self, key):
                setattr(self, key, value)
            elif key == "inverseMaxPower":
                self.profile[1].power = value

        if "gridInputPower" in payload.get("properties", {}) or "outputHomePower" in payload.get("properties", {}):
            if hasattr(self, "home_output") and hasattr(self, "home_input"):
                if self.available_kwh.update_value((self.soc.asNumber - self.soc_min.asNumber) / 100 * self.capacity_kwh):
                    self.on_update(self)
                self.on_telemetry(int(self.home_output.asNumber) - int(self.home_input.asNumber), now)

    async def on_setpoint(self, watts: int, p: PowerProfile, now: float) -> int:
        """Send setpoint to device via MQTT/HTTP/BLE."""
        watts = await super().on_setpoint(watts, p, now)

        match self.connection.asInt:
            case DeviceConnection.MQTT_CLOUD | DeviceConnection.MQTT_LOCAL:
                # via mqtt_publish so the command gets the messageId/timestamp/deviceId envelope the device expects
                payload = self._cmd_power[0] if watts == 0 else self._cmd_power[1 if watts < 0 else 2] % abs(watts)
                self.mqtt_publish(self._cmd_topic, json.loads(payload))

            case DeviceConnection.HTTP:
                try:
                    payload = _BLEHTTP[0] if watts == 0 else _BLEHTTP[1 if watts < 0 else 2] % abs(watts)
                    async with ClientSession() as session:
                        await session.post(f"http://{self.ip}/properties/write", data=payload, headers=SF_HTTP_HEADER, timeout=SF_HTTP_TIMEOUT)
                except Exception:
                    _LOGGER.warning("HTTP write failed for %s (%s)", self.name, self.ip)

            case DeviceConnection.BLE:
                payload = _BLEHTTP[0] if watts == 0 else _BLEHTTP[1 if watts < 0 else 2] % abs(watts)
                await self.ble_command(json.loads(payload), "write")
        return watts

    async def _recover_fallback(self) -> None:
        """Fallback polling: HTTP -> BLE connect -> BLE poll."""
        interval = 3.0
        try:
            while True:
                now = time.time()
                self.connection.update_value(DeviceConnection.OFFLINE)

                self.mqtt_publish("properties/read", {"properties": ["getAll"]})

                if self._ble_client:
                    if self._ble_client.is_connected or await self.ble_connect():
                        try:
                            payload = _BLEHTTP[1] % 100
                            await self.ble_command(json.loads(payload), "write")
                            await self.ble_wait_for("write_reply", timeout=3)
                        except Exception as ex:
                            _LOGGER.warning("BLE write failed for %s (%s): %s", self.name, self.blemac, ex)

                        props = ["getAll"] if self._msg_id % 10 == 0 else ["gridInputPower", "outputHomePower", "outputPackPower", "packInputPower", "solarInputPower"]
                        await self.ble_command({"properties": props, "method": "read"})
                        try:
                            while True:
                                resp = await self.ble_wait_for(timeout=interval)
                                self.update_properties(resp, time.time())
                                self.connection.update_value(DeviceConnection.BLE)
                        except asyncio.TimeoutError:
                            pass
                elif self.ip:
                    try:
                        async with ClientSession() as session:
                            async with session.get(f"http://{self.ip}/properties/report", headers=SF_HTTP_HEADER, timeout=SF_HTTP_TIMEOUT) as resp:
                                self.update_properties(await resp.json(), now)
                                self.connection.update_value(DeviceConnection.HTTP)
                    except Exception:
                        _LOGGER.warning("HTTP read failed for %s (%s)", self.name, self.ip)
                elif self.blemac:
                    await self.ble_connect()

                interval = 3.0 if self.connection.asInt != DeviceConnection.OFFLINE else min(interval * 2, 1800.0)
                if self.connection.asInt != DeviceConnection.BLE:
                    await asyncio.sleep(interval)
        except asyncio.CancelledError:
            if self._ble_client:
                await self.ble_disconnect()

    def mqtt_publish(self, topic: str, command: dict) -> None:
        """Send a JSON command to the device via MQTT."""
        if not self._mqtt:
            return
        self._msg_id += 1
        command["messageId"] = self._msg_id
        command["timestamp"] = int(time.time())
        command["deviceId"] = self.device_id
        self._mqtt.publish(f"iot/{self.prodkey}/{self.device_id}/{topic}", json.dumps(command))

    async def ble_connect(self) -> bool:
        """Connect to the device via BLE, perform BLESPP handshake."""
        if not self.blemac:
            return False
        try:
            self._ble_client = BleakClient(self.blemac)
            await self._ble_client.connect()
            await self._ble_client.start_notify(SF_READ_CHAR, self._ble_on_notify)
            await self.ble_wait_for("BLESPP", timeout=10)
            await self.ble_command({"method": "BLESPP_OK"})
            return True
        except Exception as ex:
            _LOGGER.warning("BLE connect failed for %s (%s): %s", self.name, self.blemac, ex)
            self._ble_client = None
            return False

    def _ble_on_notify(self, sender, data: bytearray) -> None:
        try:
            self._ble_last_response = json.loads(data.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._ble_last_response = {"_raw": data.hex()}
        self._ble_response_method = self._ble_last_response.get("method", "")
        self._ble_response_event.set()

    async def ble_wait_for(self, method: str | None = None, timeout: float = 10.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._ble_response_event.is_set():
                try:
                    await asyncio.wait_for(self._ble_response_event.wait(), deadline - time.time())
                except asyncio.TimeoutError:
                    break
            if self._ble_response_method == method or method is None:
                return self._ble_last_response
            self._ble_response_event.clear()
        raise asyncio.TimeoutError(f"Timeout waiting for BLE '{method}'")

    async def ble_disconnect(self) -> None:
        if self._ble_client and self._ble_client.is_connected:
            await self._ble_client.disconnect()
        self._ble_client = None

    async def ble_command(self, command: dict, method: str | None = None) -> bool:
        if not self._ble_client or not self._ble_client.is_connected:
            _LOGGER.warning("BLE command not connected for %s (%s)", self.name, self.blemac)
            return False
        try:
            self._msg_id += 1
            if method is not None:
                command["method"] = method
            if "messageId" not in command:
                command["messageId"] = self._msg_id
                command["timestamp"] = int(time.time())
                command["deviceId"] = self.device_id
            await self._ble_client.write_gatt_char(SF_COMMAND_CHAR, json.dumps(command).encode(), response=False)
            return True
        except Exception:
            _LOGGER.warning("BLE command failed for %s (%s)", self.name, self.blemac)
            return False
