"""Publica una orden v2 como si fuera el edge de voz y espera el resultado del edge de actuadores.

  python -m tools.send_command --config config.toml --device-id 10 --action open
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import uuid

import paho.mqtt.client as mqtt

from alivia_actuator_edge.shared.config import MqttCfg, load_config


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config.toml")
    p.add_argument("--device-id", type=int, required=True)
    p.add_argument("--action", required=True)
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--timeout", type=float, default=8.0)
    p.add_argument("--ttl-ms", type=int, default=5000)
    p.add_argument("--mac", help="si no se da, se toma de la tabla del config")
    a = p.parse_args()
    cfg = load_config(a.config)
    dev = next((d for d in cfg.devices if d.device_id == a.device_id), None)
    if dev is None and not a.mac:
        print(f"deviceId {a.device_id} no esta en {a.config}; usa --mac", file=sys.stderr)
        return 2
    mq: MqttCfg = cfg.mqtt
    corr = uuid.uuid4().hex
    order = {"v": 2, "correlationId": corr, "orderId": "manual-" + corr[:6], "deviceId": a.device_id,
             "mac": a.mac or dev.mac, "deviceType": dev.type if dev else "", "roomKey": dev.room_key if dev else "",
             "action": a.action, "params": {}, "source": "send_command", "ts": int(time.time() * 1000), "ttlMs": a.ttl_ms}
    done = threading.Event()
    box: dict = {}

    def on_connect(c, u, f, rc, props=None):
        c.subscribe(mq.result_topic.format(device=a.device_id), qos=1)

    def on_subscribe(c, u, mid, rcs, props=None):
        c.publish(f"alivia/actuators/{a.device_id}/command", json.dumps(order), qos=1)

    def on_message(c, u, m):
        body = json.loads(m.payload)
        if body.get("correlationId") == corr:
            box.update(body)
            done.set()

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="send-command-" + corr[:6])
    if mq.username:
        c.username_pw_set(mq.username, mq.password)
    c.on_connect, c.on_subscribe, c.on_message = on_connect, on_subscribe, on_message
    c.connect(a.host or mq.host, a.port or mq.port)
    c.loop_start()
    t0 = time.monotonic()
    ok = done.wait(a.timeout)
    c.loop_stop()
    c.disconnect()
    if not ok:
        print("sin resultado en el plazo (el edge de actuadores no contesto)")
        return 1
    print(json.dumps(box, ensure_ascii=False), f"({int((time.monotonic() - t0) * 1000)} ms)")
    return 0 if box.get("ok") else 3


if __name__ == "__main__":
    sys.exit(main())
