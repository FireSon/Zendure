#!/usr/bin/env python3
"""Hyper2000 lag/ramp test using the deviceAutomation command (the lever that actually actuates).

Charge staircase by default (battery empty). Reports on /<prodkey>/<deviceid>/properties/report;
commands to iot/<prodkey>/<deviceid>/function/invoke.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time

import paho.mqtt.client as mqtt

BROKER, PORT, USER, PW = "192.168.2.97", 1883, "zendure", "zendure"
ACC, DEV = "gDa3tb", "9RUB1mW7"
INVOKE = f"iot/{ACC}/{DEV}/function/invoke"
READ = f"iot/{ACC}/{DEV}/properties/read"
REPORT = f"/+/{DEV}/#"

state: dict = {}
samples: list[tuple[float, dict]] = []
t0 = time.monotonic()
_mid = 600


def _wrap(body: dict) -> str:
    global _mid
    _mid += 1
    body["messageId"] = _mid
    body["deviceKey"] = DEV
    body["deviceId"] = DEV
    body["timestamp"] = int(time.time())
    return json.dumps(body)


def charge(w: int) -> str:
    return _wrap({"function": "deviceAutomation", "arguments": [{"autoModelProgram": 1, "autoModel": 8, "msgType": 1,
                 "autoModelValue": {"chargingType": 1, "price": 2, "chargingPower": w, "prices": [1] * 24, "outPower": 0, "freq": 0}}]})


def discharge(w: int) -> str:
    return _wrap({"function": "deviceAutomation", "arguments": [{"autoModelProgram": 2, "autoModel": 8, "msgType": 1,
                 "autoModelValue": {"chargingType": 0, "chargingPower": 0, "freq": 0, "outPower": w}}]})


def off() -> str:
    return _wrap({"function": "deviceAutomation", "arguments": [{"autoModelProgram": 0, "autoModel": 0, "msgType": 1,
                 "autoModelValue": {"chargingType": 0, "chargingPower": 0, "freq": 0, "outPower": 0}}]})


def on_connect(client, userdata, flags, reason_code, properties=None):
    print(f"[{time.monotonic()-t0:6.1f}s] connected rc={reason_code}")
    client.subscribe(REPORT)


def on_message(client, userdata, msg):
    if DEV not in msg.topic or not msg.topic.endswith("properties/report"):
        return
    try:
        p = json.loads(msg.payload.decode("utf-8", "ignore")).get("properties", {})
    except Exception:
        return
    changed = False
    for k in ("gridInputPower", "outputHomePower", "outputPackPower", "packInputPower", "electricLevel"):
        if k in p:
            state[k] = float(p[k]); changed = True
    if not changed:
        return
    t = time.monotonic()
    samples.append((t, dict(state)))
    print(f"[{t-t0:6.1f}s] grid={state.get('gridInputPower',0):.0f} packOut={state.get('outputPackPower',0):.0f} "
          f"packIn={state.get('packInputPower',0):.0f} soc={state.get('electricLevel')}")


def poll(client):
    client.publish(READ, json.dumps({"messageId": int(time.time() * 1000) % 1000000, "deviceKey": DEV, "timestamp": int(time.time()), "properties": ["getAll"]}))


def wait_poll(client, seconds, every=2.5):
    end = time.monotonic() + seconds
    nxt = time.monotonic()
    while time.monotonic() < end:
        if time.monotonic() >= nxt:
            poll(client); nxt += every
        time.sleep(0.05)


def cmd(client, payload, label):
    t = time.monotonic()
    client.publish(INVOKE, payload)
    print(f"[{t-t0:6.1f}s] >>> {label}")
    return t


def analyse(t_cmd, target, field, label):
    base = [s.get(field, 0) for ts, s in samples if t_cmd - 10 <= ts <= t_cmd]
    baseline = statistics.median(base) if base else 0.0
    after = [(ts, s.get(field, baseline)) for ts, s in samples if ts > t_cmd]
    thr = max(50.0, 0.3 * abs(target))
    cross = next((ts for ts, v in after if abs(v - baseline) >= thr), None)
    lag = (cross - t_cmd) if cross else None
    print(f"  {label}: field={field} base={baseline:.0f} target~{target} lag={'n/a' if lag is None else f'{lag:.1f}s'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", choices=["charge", "discharge"], default="charge")
    args = ap.parse_args()
    step = charge if args.dir == "charge" else discharge
    field = "outputPackPower"  # charge power into pack; also grid mirrors it

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="em_da_lag")
    client.username_pw_set(USER, PW)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(BROKER, PORT, 30)
    client.loop_start()
    try:
        print(f"{args.dir} lag test (deviceAutomation).")
        cmd(client, off(), "OFF (settle cold)"); wait_poll(client, 35)
        t1 = cmd(client, step(300), f"{args.dir} 300 (cold)"); wait_poll(client, 40)
        t2 = cmd(client, step(600), f"{args.dir} 600 (warm)"); wait_poll(client, 30)
        t3 = cmd(client, step(300), f"{args.dir} 300 (warm down)"); wait_poll(client, 30)
        cmd(client, off(), "OFF"); wait_poll(client, 25)
        print("\n== results ==")
        analyse(t1, 300, field, "cold 0->300")
        analyse(t2, 600, field, "warm 300->600")
        analyse(t3, 300, field, "warm 600->300")
    finally:
        client.publish(INVOKE, off())
        print("safety OFF sent.")
        time.sleep(1.0)
        client.loop_stop(); client.disconnect()


if __name__ == "__main__":
    main()
