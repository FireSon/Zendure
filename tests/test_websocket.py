"""The panel's add command for devices: the driver's form, its identify step and the checks the HA subentry flow used to make."""

import asyncio
import importlib
import sys
import types

import homeassistant.components.websocket_api as websocket_api

from custom_components.zenmaster.anker.driver import AnkerDriver
from custom_components.zenmaster.const import DOMAIN
from test_subentries import _coord

# the decorators register the command with HA; in a test the plain coroutine is what we call
websocket_api.websocket_command = lambda schema: (lambda f: f)
websocket_api.async_response = lambda f: f
sys.modules.pop("custom_components.zenmaster.websocket", None)
websocket = importlib.import_module("custom_components.zenmaster.websocket")


class Connection:
    """Collects what the command answers."""

    def __init__(self) -> None:
        self.results: list = []

    def send_result(self, _id, result=None) -> None:
        self.results.append(result)

    def send_error(self, _id, code, message) -> None:
        self.results.append({"error": code, "message": message})


async def add(coord, kind: str, data: dict) -> dict:
    # one add command from the panel, the answer it gets
    connection = Connection()
    hass = types.SimpleNamespace(data={DOMAIN: {"entry": coord}})
    await websocket.ws_subentry_add(hass, connection, {"id": 1, "subentry_type": kind, "title": "", "data": data})
    return connection.results[-1]


def _devices(entry) -> list:
    return [s for s in entry.subentries.values() if s.subentry_type != "energy_manager"]  # the coordinator files its own manager subentry


def test_a_marstek_is_added_by_its_ip_once() -> None:
    async def scenario() -> None:
        coord, entry = _coord()
        coord.resync = lambda changed=False: asyncio.sleep(0)  # the solver is not under test here
        coord.hass.async_create_task = asyncio.get_running_loop().create_task

        # the ip identifies a Marstek: it becomes the serial, the generation names it
        result = await add(coord, "marstek", {"product": "v3", "ip": "10.0.0.17", "port": 502})
        (sub,) = _devices(entry)
        assert result == {"ok": True, "subentry_id": sub.subentry_id}
        assert (sub.subentry_type, sub.title, sub.data["sn"]) == ("marstek", "VenusE_17", "10.0.0.17")
        assert "10.0.0.17" in coord.drivers["marstek"].devices

        # the same ip again, and a generation the driver does not know
        assert await add(coord, "marstek", {"product": "v3", "ip": "10.0.0.17"}) == {"ok": False, "errors": ["device_exists"]}
        assert await add(coord, "marstek", {"product": "v9", "ip": "10.0.0.18"}) == {"ok": False, "errors": ["unknown_product"]}
        assert len(_devices(entry)) == 1

    asyncio.run(scenario())


def test_an_mdns_find_reaches_only_the_drivers_of_its_service_type(monkeypatch) -> None:
    async def scenario() -> None:
        import custom_components.zenmaster.coordinator as coordinator_module

        coord, _entry = _coord()
        heard: list = []
        for driver in coord.drivers.values():
            monkeypatch.setattr(driver, "zeroconf_found", lambda name, ip, props, kind=driver.id: asyncio.sleep(0, result=heard.append((kind, name, ip, props))), raising=False)

        class Info:
            # a resolved advertisement: its address and its properties
            def __init__(self, service_type, name) -> None:
                self.name = name

            async def async_request(self, _zc, _timeout) -> bool:
                return True

            def parsed_addresses(self) -> list:
                return ["10.0.0.9"]

            decoded_properties = {"sn": "AZVDMWH1234567890"}

        monkeypatch.setattr(coordinator_module, "AsyncServiceInfo", Info)
        await coord._resolve_find(None, "_anker_power._udp.local.", "solarbank._anker_power._udp.local.")
        await coord._resolve_find(None, "_zendure._tcp.local.", "zendure-hyper-ABC._zendure._tcp.local.")
        assert heard == [("anker", "solarbank._anker_power._udp.local.", "10.0.0.9", {"sn": "AZVDMWH1234567890"}), ("zendure", "zendure-hyper-ABC._zendure._tcp.local.", "10.0.0.9", {"sn": "AZVDMWH1234567890"})]

    asyncio.run(scenario())


def test_an_anker_is_identified_by_the_serial_it_reports(monkeypatch) -> None:
    async def scenario() -> None:
        coord, entry = _coord()
        coord.resync = lambda changed=False: asyncio.sleep(0)
        coord.hass.async_create_task = asyncio.get_running_loop().create_task

        # nothing answers: the error comes back and nothing is filed
        monkeypatch.setattr(AnkerDriver, "probe", classmethod(lambda cls, ip, port=502: asyncio.sleep(0, result=None)))
        assert await add(coord, "anker", {"ip": "10.0.0.20"}) == {"ok": False, "errors": ["cannot_connect"]}

        # the serial it reports (17 characters, the product code at 4-7) is its identity and picks its model
        monkeypatch.setattr(AnkerDriver, "probe", classmethod(lambda cls, ip, port=502: asyncio.sleep(0, result="AZVDMWH1234567890")))
        result = await add(coord, "anker", {"ip": "10.0.0.20"})
        (sub,) = _devices(entry)
        assert result["ok"] and (sub.data["sn"], sub.data["product"], sub.data["ip"]) == ("AZVDMWH1234567890", "DMWH", "10.0.0.20")

    asyncio.run(scenario())
