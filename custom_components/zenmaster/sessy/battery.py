"""SessyBattery: a Sessy home battery, steered with the API power strategy and a signed setpoint over the dongle's local HTTP API."""

from __future__ import annotations

import base64
import logging
import time
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from ..config import FLEET, IDENTITY, ConfigField
from ..const import DOMAIN, PhaseModi
from ..device import DeviceState, PolledDevice, ProfileCharge, ProfileDischarge
from ..number import HaNumber
from ..select import HaSelect
from ..sensor import HaSensor

if TYPE_CHECKING:
    from ..distribution import DistributionMode
    from .driver import Product, SessyDriver

_LOGGER = logging.getLogger(__name__)

SLOW_EVERY = 12  # the active strategy once per this many polls
POWER_SET = 40  # W: the measured power is this close to the setpoint, the command is in
POWER_MAX_TIME = 65  # s: or this long after the command; from standby a Sessy takes up to 60 s to start (Omnibattery discussion 129)
TIMEOUT = aiohttp.ClientTimeout(total=10)
API = "POWER_STRATEGY_API"  # the strategy in which the setpoint applies; on a lost network connection Sessy sets the setpoint to 0 itself (Sessy API docs)
SOC_LIMIT = {"SYSTEM_STATE_BATTERY_FULL": 1, "SYSTEM_STATE_BATTERY_EMPTY": 2}  # system_state to the device's own verdict, like Zendure's socLimit
# states in which the battery cannot follow a setpoint: starting up, a fault, off the grid (names from Sessy's API docs, which do not explain them)
BLOCKED = {"SYSTEM_STATE_INIT", "SYSTEM_STATE_WAIT_FOR_PERIPHERALS", "SYSTEM_STATE_ERROR", "SYSTEM_STATE_DISCONNECT", "SYSTEM_STATE_RECONNECT"}


def basic_auth(username: str, password: str) -> str:
    # basic auth as a plain header: aiohttp deprecates its BasicAuth helper
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


class SessyBattery(PolledDevice):
    """A Sessy dongle with its battery; positive power is delivered to the house, negative charges (Sessy API docs)."""

    POLL_S = 5.0  # power status every 5 s, like ha-sessy; the dongle's HTTP server seems fragile
    RESEND_S = 90  # s: an accepted setpoint goes again only after this long, past the start from standby; on a lost network the dongle resets it to 0 (Sessy API docs)

    CONFIG = [*IDENTITY, ConfigField("ip", {"text": {}}, cast=str), ConfigField("username", {"text": {}}, cast=str), ConfigField("password", {"text": {"type": "password"}}, cast=str), *FLEET, *(ConfigField(k, {"entity": {}}, readonly=True) for k in ("system_state", "strategy_overridden"))]

    def __init__(self, hass: HomeAssistant, driver: SessyDriver, subentry_id: str | None, name: str, data: dict[str, Any], product: Product) -> None:
        self.driver = driver
        self.sn: str = data["sn"]  # the identity: the host it was added with
        self.model: str = data["product"]
        self.ip: str = data["ip"]
        self.username: str = data.get("username", "")
        self.password: str = data.get("password", "")
        self.on_config_changed: Any = None
        self.home_input = self.home_output = self.battery_input = self.battery_output = 0
        self.strategy: str | None = data.get("strategy")  # the strategy before we took control, put back when we hand it back; kept in the subentry, the API offers no way to read it back after a crash

        # the registry entry first: the entities find their device there
        dr.async_get(hass).async_get_or_create(config_entry_id=driver.entry_id, config_subentry_id=subentry_id, identifiers={(DOMAIN, self.sn)}, name=name, manufacturer=driver.manufacturer, model=f"Sessy {self.model}")
        PolledDevice.__init__(self, hass, self.sn, [ProfileDischarge(product.discharge_w), ProfileCharge(-product.charge_w)], name, subentry_id)
        self.kwhsensor.update_value(product.kwh)

        # a fresh connection per request: the dongle closes kept-alive sockets without saying so (Omnibattery sessy.py)
        self.session = aiohttp.ClientSession(headers={"Authorization": basic_auth(self.username, self.password)}, connector=aiohttp.TCPConnector(force_close=True), timeout=TIMEOUT)

    def subentry_data(self) -> dict[str, Any]:
        return {"product": self.model, "sn": self.sn, "ip": self.ip, "username": self.username, "password": self.password, "strategy": self.strategy}

    async def disconnect(self) -> None:
        await self.session.close()

    def entity_create(self) -> None:
        HaSensor(self, "soc", uom="%", device_class="battery", state_class="measurement", precision=1)
        HaNumber(self, "soc_min", uom="%", minimum=0, maximum=100, restore=True, on_changed=self.soc_changed)
        HaNumber(self, "soc_max", uom="%", minimum=0, maximum=100, restore=True, on_changed=self.soc_changed)
        HaSensor(self, "soc_limit", state_class="measurement")
        HaNumber(self, "individual", uom="W", minimum=-10000, maximum=10000, on_changed=lambda _entity, _value: None)  # the setpoint of the individual mode, EM's own: nothing goes to the device

        HaSensor(self, "home_input", uom="W", device_class="power", state_class="measurement", aggr=True)
        HaSensor(self, "home_output", uom="W", device_class="power", state_class="measurement", aggr=True)
        HaSensor(self, "battery_input", uom="W", device_class="power", state_class="measurement", aggr=True)
        HaSensor(self, "battery_output", uom="W", device_class="power", state_class="measurement", aggr=True)
        self.setpoint = HaSensor(self, "power_setpoint", uom="W", device_class="power", state_class="measurement")
        HaSelect(self, "power_phase", PhaseModi, on_changed=self.update_phase, restore=True)
        HaSensor(self, "system_state")
        HaSensor(self, "strategy_overridden")  # shown only: what sets it is not documented, a tester tells us before we steer on it

        self.kwhsensor = HaSensor(self, "kwh", uom="kWh", device_class="energy", restore=True)
        HaSensor(self, "rte", uom="%", state_class="measurement", precision=1)

    def soc_changed(self, entity: Any, value: Any) -> None:
        # soc_min and soc_max are ZenMaster's own limits, nothing goes to the device; the fleet order follows them
        setattr(self, entity.attr, value)
        if self.on_invalidate is not None:
            self.on_invalidate()

    async def get(self, path: str) -> dict[str, Any]:
        async with self.session.get(f"http://{self.ip}/api/v1/{path}") as response:
            response.raise_for_status()
            return await response.json(content_type=None)

    async def post(self, path: str, body: dict[str, Any]) -> None:
        async with self.session.post(f"http://{self.ip}/api/v1/{path}", json=body) as response:
            await response.read()  # an unread reply would poison the connection
            response.raise_for_status()

    async def send(self, kind: str, watts: int) -> None:
        # a setpoint: the API strategy, then the setpoint; off: setpoint 0 and the old strategy back; a battery we never took stays untouched
        if kind == "off" and not self._control:
            return
        try:
            if kind == "power":
                # the battery's own strategy before we take it, if no poll caught it yet: without it the hand back leaves the battery in API at 0 W
                if not self._control and self.strategy is None:
                    await self.learn_strategy()
                await self.post("power/active_strategy", {"strategy": API})
                self._control = True
                await self.post("power/setpoint", {"setpoint": watts})
            else:
                await self.post("power/setpoint", {"setpoint": 0})
                if self.strategy is not None:
                    await self.post("power/active_strategy", {"strategy": self.strategy})
                self._control = False
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.warning("Sessy %s: %s not sent (%s)", self.name, kind, err)
            return
        if kind == "power" and self.state == DeviceState.SENT:
            self.set_state(DeviceState.CONFIRM)

    async def poll(self) -> None:
        # the power status every round; the strategy now and then, and only while it is the battery's own
        self._polls += 1
        try:
            status = await self.get("power/status")
            if self._polls % SLOW_EVERY == 1 and not self._control:
                await self.learn_strategy()
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            self.poll_failed(err)
            return
        self._silent = 0
        self.update_status(status.get("sessy") or {})

    async def learn_strategy(self) -> None:
        # the battery's own strategy into the subentry; API is ours and never learned
        if (strategy := (await self.get("power/active_strategy")).get("strategy")) not in (None, API, self.strategy):
            self.strategy = strategy
            self.config_changed()

    def update_status(self, sessy: dict[str, Any]) -> None:
        if (soc := sessy.get("state_of_charge")) is not None:
            self.entity_update("soc", round(soc * 100, 1))
        if (state := sessy.get("system_state")) is not None:
            self.entity_update("system_state", state)
            self.blocked = state in BLOCKED
            if self.entity_update("soc_limit", SOC_LIMIT.get(state, 0)) and self.on_invalidate is not None:
                self.on_invalidate()
        if (overridden := sessy.get("strategy_overridden")) is not None:
            self.entity_update("strategy_overridden", overridden)

        # power is positive towards the house; the API reports no pack power apart from it
        if (p := sessy.get("power")) is None:
            return
        p = int(p)
        self.entity_update("home_output", max(0, p))
        self.entity_update("home_input", max(0, -p))
        self.entity_update("battery_output", max(0, p))
        self.entity_update("battery_input", max(0, -p))
        self.power_actual = p

        # a battery that cannot follow is left out of the fleet until it can again
        if self.blocked:
            self.set_state(DeviceState.EXTERNAL)
        elif abs(self.power_actual - self.power_setpoint) < POWER_SET or (time.time() - self.state_since) > POWER_MAX_TIME:
            self.set_state(DeviceState.ACTIVE)
