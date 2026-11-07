"""BLE-test: volledige app-sequence met alle parameters tegen een Zendure-device.

Gebruik:  python tools/ble_full.py [bleName] [iotUrl]
WiFi.get -> deviceInfo -> token(random) -> station -> getInfo(deviceId). Logt alles.
LET OP: token+station provisiont het device echt naar iotUrl/wifi.
"""

import asyncio
import json
import random
import string
import sys
import time

from bleak import BleakClient, BleakScanner

CHAR_WRITE = "0000c304-0000-1000-8000-00805f9b34fb"
CHAR_READ = "0000c305-0000-1000-8000-00805f9b34fb"


def load_wifi() -> tuple[str, str]:
    ce = json.load(open("config/.storage/core.config_entries"))
    d = next(e["data"] for e in ce["data"]["entries"] if e["domain"] == "zenmaster")
    return d.get("wifissid", ""), d.get("wifipsw", "")


async def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "ZenE1_22"
    iot = sys.argv[2] if len(sys.argv) > 2 else "192.168.2.97"
    ssid, pwd = load_wifi()
    token = "".join(random.choices(string.ascii_letters + string.digits, k=16))
    device_id = {"v": ""}
    print(f"bleName={name} iotUrl={iot} ssid={ssid} token={token}")

    dev = await BleakScanner.find_device_by_filter(lambda d, a: (d.name or "").startswith(name), timeout=20.0)
    if dev is None:
        print("Device niet gevonden.")
        return
    client = BleakClient(dev)
    await client.connect()
    print("Verbonden:", dev.address)

    async def send(obj: dict) -> None:
        print("->", json.dumps(obj))
        await client.write_gatt_char(CHAR_WRITE, json.dumps(obj).encode(), response=False)

    def on_notify(_handle: int, data: bytearray) -> None:
        s = data.decode("utf-8", "ignore")
        print("<-", s)
        try:
            m = json.loads(s)
            if m.get("deviceId"):
                device_id["v"] = m["deviceId"]
            if m.get("method") == "BLESPP":
                asyncio.get_event_loop().create_task(send({"method": "BLESPP_OK"}))
        except Exception:
            pass

    await client.start_notify(CHAR_READ, on_notify)
    await asyncio.sleep(2.0)
    await send({"messageId": 1001, "method": "WiFi.get"})
    await asyncio.sleep(2.0)
    await send({"messageId": 101, "method": "deviceInfo", "slaveSN": "jump", "timeZone": "GMT+08:00"})
    await asyncio.sleep(2.0)
    await send({"AM": 3, "homeId": 64847, "iotUrl": iot, "messageId": 1002, "method": "token", "password": pwd, "ssid": ssid, "timeZone": "GMT+08:00", "token": token})
    await asyncio.sleep(2.0)
    await send({"messageId": 1003, "method": "station"})
    await asyncio.sleep(2.0)
    did = device_id["v"] or "9RUB1mW7"
    for _ in range(8):
        await send({"deviceId": did, "messageId": 10, "method": "getInfo", "timestamp": int(time.time() * 1000)})
        await asyncio.sleep(2.0)
    await asyncio.sleep(5.0)
    try:
        await client.disconnect()
    except Exception:
        pass
    print("Klaar.")


asyncio.run(main())
