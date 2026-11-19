"""Composicion del edge de actuadores."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from alivia_actuator_edge.devices.domain.model import StaticDeviceTable
from alivia_actuator_edge.dispatch.application.service import ActuatorService
from alivia_actuator_edge.dispatch.infrastructure.mqtt import MqttGateway
from alivia_actuator_edge.dispatch.infrastructure.reporters import LogReporter
from alivia_actuator_edge.http.app import HealthContext, HttpServer
from alivia_actuator_edge.shared.diagnostics import CompositeReporter, Diagnostics
from alivia_actuator_edge.embedded.application.hub import EmbeddedHub
from alivia_actuator_edge.embedded.interfaces.transports.bluetooth import BluetoothConnector
from alivia_actuator_edge.embedded.interfaces.transports.websocket import serve_ws
from alivia_actuator_edge.events.domain import BatteryWatch, FailureFilter
from alivia_actuator_edge.events.reporter import BatteryEventReporter, EventReporter, FailureEventReporter
from alivia_actuator_edge.events.sender import HttpEventSender
from alivia_actuator_edge.shared.config import Config, load_config

log = logging.getLogger("alivia.actuator")


async def run(cfg: Config, stop: asyncio.Event | None = None, ready: asyncio.Event | None = None) -> None:
    table = StaticDeviceTable.from_config(cfg.devices)
    if not table.all():
        raise ValueError("config.toml no define ningun [[devices]]")
    hub = EmbeddedHub(table, cfg.server)
    mqtt_gw = MqttGateway(cfg.mqtt, hub.online_summary)
    diagnostics = Diagnostics(cfg.http.errors_kept)
    reporters = [LogReporter(), diagnostics]
    events = None
    if cfg.backend.enabled:  # fallas y batería baja llegan al familiar como notificación
        b = cfg.backend
        events = EventReporter(HttpEventSender(b.base_url, b.credential, b.request_timeout_s),
                               queue_max=b.queue_max, retry_max_s=b.retry_max_s)
        reporters.append(FailureEventReporter(events, FailureFilter(b.failure_cooldown_s)))
        hub.add_telemetry_listener(BatteryEventReporter(events, BatteryWatch(b.low_battery_pct, b.battery_rearm_pct)).on_telemetry)
        events.start()
        log.info("Eventos del hogar hacia %s (batería baja <= %d%%)", b.base_url, b.low_battery_pct)
    service = ActuatorService(table, hub, mqtt_gw, CompositeReporter(*reporters), cfg.dispatch)
    hub.add_listener(mqtt_gw.publish_status)  # el edge de voz se entera si un ESP32 entra o sale

    ws_server = await serve_ws(cfg.server, hub.serve_link)
    log.info("WebSocket del embebido en ws://%s:%d%s", cfg.server.host, cfg.server.port, cfg.server.path)
    bt = None
    if cfg.bluetooth.enabled and cfg.bluetooth.links:
        bt = BluetoothConnector(cfg.bluetooth, hub.serve_link, hub.wifi_alive)
        bt.start()
        log.info("Bluetooth: %d enlace(s) configurado(s)", len(cfg.bluetooth.links))
    await mqtt_gw.start(service.handle)
    http = None
    if cfg.http.enabled:
        http = HttpServer(HealthContext(table, diagnostics, hub.snapshot, lambda: mqtt_gw.connected, cfg.http))
        http.start()
        if ready is not None:
            ready.http = http  # type: ignore[attr-defined]  # los tests leen el puerto real
    if ready is not None:
        ready.set()
    stop = stop or asyncio.Event()
    try:
        await stop.wait()
    finally:
        if events:
            await events.stop()
        if http:
            http.stop()
        if bt:
            await bt.stop()
        await mqtt_gw.stop()
        ws_server.close()
        await hub.close_all()
        await ws_server.wait_closed()


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="alivia-actuator-edge", description="Edge de actuadores de Alivia")
    parser.add_argument("--config", default="config.toml", help="ruta del config.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    if not Path(args.config).exists():
        raise SystemExit(f"No existe {args.config}. Copia config.example.toml a config.toml y ajusta los valores.")
    try:
        asyncio.run(run(load_config(args.config)))
    except KeyboardInterrupt:
        pass
