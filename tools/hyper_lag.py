#!/usr/bin/env python3
"""Measure Hyper2000 command lag + ramp via local MQTT.

Reports:  /<prodkey>/<deviceid>/properties/report  (leading slash, partial props).
Commands: iot/<prodkey>/<deviceid>/function/invoke, wrapped with messageId/timestamp/deviceId
          (matches the format that actually actuates the device).
"""

from __future__ import annotations

import argparse
import json
import statistics
import time

import paho.mqtt.client as mqtt

BROKER, PORT, USER, PSW = "192.168.2.97", 1883, "zendure", "zendure"
PRODKEY, DEVICE = "gDa3tb", "9RUB1mW7"
CMD_TOPIC = f"iot/{PRODKEY}/{DEVICE}/function/invoke"
READ_TOPIC = f"iot/{PRODKEY}/{DEVICE}/properties/read"
REPORT_SUB = f"/+/{DEVICE}/#"

KEYS = ["gridInputPower", "outputHomePower", "packInputPower", "outputPackPower", "solarInputPower", "packPower"]

state: dict = {}
samples: list[tuple[float, dict]] = []
t0 = time.monotonic()
_msgid = 500000


def _wrap(body: dict) -> str:
    global _msgid
    _msgid += 1
    body["messageId"] = _msgid
    body["timestamp"] = int(time.time())
    body["deviceId"] = DEVICE
    return json.dumps(body)


def discharge(w: int) -> str:
    return _wrap({"function": "hemsEP", "arguments": {"outputPower": w, "freq": 0, "mode": 9}})


def off() -> str:
    return _wrap({"function": "hemsEP", "arguments": {"outputPower": 0, "freq": 0, "mode": 0}})


def on_connect(client, userdata, flags, reason_code, properties=None):
    print(f"[{time.monotonic()-t0:6.1f}s] connected rc={reason_code}, subscribing {REPORT_SUB}")
    client.subscribe(REPORT_SUB)


def on_message(client, userdata, msg):
    if DEVICE not in msg.topic:
        return
    raw = msg.payload.decode("utf-8", "ignore")
    t = time.monotonic()
    if not msg.topic.endswith("properties/report"):
        tail = msg.topic.split(DEVICE, 1)[-1]
        if "log" not in tail:
            print(f"[{t-t0:6.1f}s] MSG {tail}: {raw[:150]}")
        return
    try:
        payload = json.loads(raw)
    except Exception:
        return
    props = payload.get("properties")
    changed = False
    if isinstance(props, dict):
        for k, v in props.items():
            if isinstance(v, (int, float)):
                state[k] = float(v)
        changed = True
    pd = payload.get("packData")
    if isinstance(pd, list) and pd:
        state["packPower"] = sum(float(b.get("power", 0) or 0) for b in pd)
        changed = True
    if not changed:
        return
    samples.append((t, dict(state)))
    shown = " ".join(f"{k}={state[k]:+.0f}" for k in KEYS if k in state and state[k] != 0) or "idle"
    print(f"[{t-t0:6.1f}s] {shown}  soc={state.get('electricLevel')}")


def poll(client):
    client.publish(READ_TOPIC, _wrap({"properties": ["getAll"]}))


def wait_poll(client, seconds: float, every: float = 3.0):
    end = time.monotonic() + seconds
    nxt = time.monotonic()
    while time.monotonic() < end:
        if time.monotonic() >= nxt:
            poll(client)
            nxt += every
        time.sleep(0.05)


def command(client, payload: str, label: str) -> float:
    t = time.monotonic()
    client.publish(CMD_TOPIC, payload)
    print(f"[{t-t0:6.1f}s] >>> {label}: {payload}")
    return t


def analyse(t_cmd: float, target: float, label: str):
    base = [s for ts, s in samples if t_cmd - 12 <= ts <= t_cmd]
    after = [(ts, s) for ts, s in samples if ts > t_cmd]
    if not after or not base:
        print(f"  {label}: insufficient samples (base={len(base)} after={len(after)})")
        return
    keys = set().union(*[s.keys() for s in base + [s for _, s in after]])
    movers = []
    for k in keys:
        b = statistics.median([s.get(k, 0.0) for s in base])
        swing = max((abs(s.get(k, b) - b) for _, s in after), default=0.0)
        if swing >= 30:
            cross = next((ts for ts, s in after if abs(s.get(k, b) - b) >= max(30.0, 0.15 * target)), None)
            movers.append((swing, k, b, (cross - t_cmd) if cross else None))
    movers.sort(reverse=True)
    if not movers:
        print(f"  {label}: NO field moved >30W after command")
        return
    for swing, k, b, lag in movers[:6]:
        print(f"  {label}: {k} base={b:+.0f} swing={swing:.0f}W lag={_fmt(lag)}")


def _fmt(x, unit="s"):
    return "n/a" if x is None else f"{x:.1f}{unit}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--power", type=int, default=300)
    ap.add_argument("--power2", type=int, default=500)
    args = ap.parse_args()
    p1, p2 = min(500, args.power), min(500, args.power2)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="em_lag_test")
    client.username_pw_set(USER, PSW)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(BROKER, PORT, 30)
    client.loop_start()

    try:
        if not args.run:
            print(f"DRY-RUN: listening {args.seconds:.0f}s (with getAll polling).")
            wait_poll(client, args.seconds)
            print(f"\n{len(samples)} reports; soc={state.get('electricLevel')}%")
            return

        print("RUN: baseline -> discharge steps -> OFF (getAll polling throughout).")
        wait_poll(client, 15)
        t_cold = command(client, discharge(p1), f"discharge {p1}W (cold)")
        wait_poll(client, 35)
        t_warm = command(client, discharge(p2), f"discharge {p2}W (warm)")
        wait_poll(client, 30)
        command(client, off(), "OFF (return control)")
        wait_poll(client, 20)
        print("\n== discharge ==")
        analyse(t_cold, p1, f"cold {p1}W")
        analyse(t_warm, p2, f"warm {p2}W")
        print("\nDone.")
    finally:
        if args.run:
            client.publish(CMD_TOPIC, off())
            print("safety OFF sent.")
            time.sleep(1.0)
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
