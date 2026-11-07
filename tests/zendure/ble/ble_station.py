"""BLE-test: stuur ALLEEN het station-commando en log wat het device doet.

Gebruik:  python tools/ble_station.py [bleName]
LET OP: als er net een token-commando gestaged is, past station dat toe (device schakelt om).
"""

import asyncio
import json
import sys

from bleak import BleakClient, BleakScanner

CHAR_WRITE = "0000c304-0000-1000-8000-00805f9b34fb"
CHAR_READ = "0000c305-0000-1000-8000-00805f9b34fb"


async def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "ZenE1_22"
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
            if json.loads(s).get("method") == "BLESPP":
                asyncio.get_event_loop().create_task(send({"method": "BLESPP_OK"}))
        except Exception:
            pass

    await client.start_notify(CHAR_READ, on_notify)
    await asyncio.sleep(2.0)
    await send({"messageId": 1003, "method": "station"})
    await asyncio.sleep(20.0)
    try:
        await client.disconnect()
    except Exception:
        pass
    print("Klaar.")


asyncio.run(main())
