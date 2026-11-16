"""Broker MQTT local para pruebas (amqtt). En produccion local usa Mosquitto; esto solo evita instalarlo.

  python -m tools.dev_broker [--port 1883]
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from amqtt.broker import Broker


def config(port: int) -> dict:
    return {
        "listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{port}"}},
        "sys_interval": 0,
        "auth": {"allow-anonymous": True, "plugins": ["auth_anonymous"]},
        "topic-check": {"enabled": False},
    }


async def _main(port: int) -> None:
    broker = Broker(config(port))
    await broker.start()
    print(f"broker MQTT en 127.0.0.1:{port} (Ctrl+C para salir)")
    await asyncio.Event().wait()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=1883)
    logging.basicConfig(level=logging.WARNING)
    try:
        asyncio.run(_main(ap.parse_args().port))
    except KeyboardInterrupt:
        pass
