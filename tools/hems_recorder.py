"""Passive cloud-MQTT recorder: log every message for all devices (plus the P1) to a jsonl file.

usage: python tools/hems_recorder.py <out.jsonl>   (runs until killed; never publishes)
"""
from __future__ import annotations

import base64
import hashlib
import json
import random
import sys
import time
import urllib.request
from pathlib import Path

import paho.mqtt.client as mqtt

ROOT = Path(__file__).resolve().parents[1]
SF_HAKEY = "C*dafwArEOXK"


def cloud_config() -> tuple[dict, list[str]]:
    store = json.loads((ROOT / "config" / ".storage" / "zenmaster.devices").read_text(encoding="utf-8"))["data"]["zendure"]
    api_url, app_key = base64.b64decode(store["apptoken"]).decode().rsplit(".", 1)
    ts, nonce = int(time.time()), str(random.randint(10000, 99999))
    body = "".join(f"{k}{v}" for k, v in sorted({"appKey": app_key, "timestamp": ts, "nonce": nonce}.items()))
    sign = hashlib.sha1(f"{SF_HAKEY}{body}{SF_HAKEY}".encode()).hexdigest().upper()
    req = urllib.request.Request(f"{api_url}/api/ha/deviceList", data=json.dumps({"appKey": app_key}).encode(),
        headers={"Content-Type": "application/json", "timestamp": str(ts), "nonce": nonce, "clientid": "zenHa", "sign": sign})
    data = json.load(urllib.request.urlopen(req, timeout=10))["data"]
    return data["mqtt"], [d["deviceKey"] for d in data.get("deviceList", [])]


def main() -> None:
    out = open(sys.argv[1], "a", encoding="utf-8", buffering=1)

    def log(kind: str, **fields) -> None:
        out.write(json.dumps({"t": round(time.time(), 3), "kind": kind, **fields}, separators=(",", ":")) + "\n")

    cfg, devices = cloud_config()
    devices = sorted(set(devices) | {"A0r9v5UC"})  # the P1 is not in the deviceList but reports on the cloud
    host, port = cfg["url"].rsplit(":", 1)

    def on_connect(client, u, f, rc, p=None):
        log("connect", rc=str(rc))
        for dev in devices:
            client.subscribe(f"/+/{dev}/#")
            client.subscribe(f"iot/+/{dev}/#")

    def on_message(client, u, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8", "ignore"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {"_raw": msg.payload.decode("utf-8", "ignore")}
        log("msg", topic=msg.topic, payload=payload)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, "rec" + str(random.randint(10000, 99999)))
    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = lambda *a: log("disconnect")
    client.username_pw_set(cfg.get("username", ""), cfg.get("password", ""))
    client.reconnect_delay_set(1, 10)
    client.connect(host, int(port))
    client.loop_start()
    print(f"recording {len(devices)} devices from {host}:{port} -> {sys.argv[1]}", flush=True)
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
