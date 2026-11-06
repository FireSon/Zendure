"""Losse BLE-test: verbind met een Zendure-device en probeer de WiFi.get-methode.

Gebruik:  python tools/ble_wifiget.py [bleName]
Default bleName = ZenE1_22 (Hyper 2000). Stuur BLESPP_OK na de handshake, daarna WiFi.get,
en print elk inkomend BLE-bericht.
"""

import asyncio
import json
import sys

from bleak import BleakClient, BleakScanner

SERVICE = "0000a002-0000-1000-8000-00805f9b34fb"
CHAR_WRITE = "0000c304-0000-1000-8000-00805f9b34fb"
CHAR_READ = "0000c305-0000-1000-8000-00805f9b34fb"


async def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "ZenE1_22"
    print(f"Scannen naar {name}… (max 20s)")
    dev = await BleakScanner.find_device_by_filter(lambda d, a: (d.name or "").startswith(name), timeout=20.0)
    if dev is None:
        print("Device niet gevonden. Staat de Hyper in pairing-mode en heeft deze PC BLE?")
        return
    print("Gevonden:", dev.name, dev.address)

    client = BleakClient(dev)
    await client.connect()
    print("Verbonden")

    async def send(obj: dict) -> None:
        print("->", json.dumps(obj))
        await client.write_gatt_char(CHAR_WRITE, json.dumps(obj).encode(), response=False)

    def on_notify(_handle: int, data: bytearray) -> None:
        s = data.decode("utf-8", "ignore")
        print("<-", s)
        try:
            if json.loads(s).get("method") == "BLESPP":
                asyncio.get_event_loop().create_task(send({"method": "BLESPP_OK"}))
        except Exception:
            pass

    await client.start_notify(CHAR_READ, on_notify)
    await asyncio.sleep(2.0)  # room for the BLESPP handshake
    await send({"messageId": 1001, "method": "WiFi.get"})
    await asyncio.sleep(15.0)  # antwoorden verzamelen
    await client.disconnect()
    print("Klaar.")


asyncio.run(main())
