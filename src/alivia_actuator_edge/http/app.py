"""API HTTP de salud y diagnostico (Flask). Solo lectura: nunca mueve un actuador.

Corre en un hilo aparte (werkzeug) dentro del mismo proceso del edge y lee vistas seguras del estado:
  GET /health        vivo/no (200 o 503) para monitoreo; no pide token
  GET /devices       tabla de dispositivos con estado en linea, ultimo resultado y contadores
  GET /embedded      ESP32 conectados: enlace, canales, bateria, memoria, senal
  GET /diagnostics   contadores globales, fallos por causa, estado del broker
  GET /errors        errores recientes (?limit=50&deviceId=10)
"""
from __future__ import annotations

import hmac
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from flask import Flask, jsonify, request
from werkzeug.serving import make_server

from alivia_actuator_edge import __version__
from alivia_actuator_edge.devices.domain.model import DeviceTable
from alivia_actuator_edge.shared.config import HttpCfg
from alivia_actuator_edge.shared.diagnostics import Diagnostics

log = logging.getLogger("alivia.actuator.http")


@dataclass
class HealthContext:
    table: DeviceTable
    diagnostics: Diagnostics
    embedded_snapshot: Callable[[], tuple[dict, ...]]
    mqtt_connected: Callable[[], bool]
    cfg: HttpCfg


def create_app(ctx: HealthContext) -> Flask:
    app = Flask("alivia-actuator-edge")
    app.json.ensure_ascii = False
    app.json.sort_keys = False

    @app.before_request
    def _auth():
        if ctx.cfg.token and request.path != "/health":
            got = request.headers.get("Authorization", "")
            if not hmac.compare_digest(got.encode(), f"Bearer {ctx.cfg.token}".encode()):
                return jsonify(error="no autorizado"), 401
        return None

    @app.get("/health")
    def health():
        embedded = ctx.embedded_snapshot()
        mqtt_ok = ctx.mqtt_connected()
        body = {"status": "ok" if mqtt_ok else "degraded", "version": __version__, "mqtt": mqtt_ok,
                "embedded": len(embedded), "uptimeS": ctx.diagnostics.summary()["uptimeS"]}
        if not mqtt_ok:
            body["problems"] = ["sin conexion con el broker MQTT"]
        elif not embedded:
            body["status"] = "degraded"
            body["problems"] = ["ningun ESP32 conectado"]
        return jsonify(body), (200 if mqtt_ok else 503)

    @app.get("/devices")
    def devices():
        online = {}
        for e in ctx.embedded_snapshot():
            for ch, d in e["channels"].items():
                online[d["deviceId"]] = {"embedded": e["deviceId"], "channel": ch, "link": e["link"]}
        items = []
        for d in ctx.table.all():
            last = ctx.diagnostics.last(d.device_id)
            items.append({"deviceId": d.device_id, "mac": str(d.mac), "type": d.kind.value, "status": d.status.value,
                          "roomKey": d.room_key, "function": d.function, "online": d.device_id in online,
                          "via": online.get(d.device_id), "lastResult": last, "counts": ctx.diagnostics.device_counts(d.device_id)})
        return jsonify(devices=items)

    @app.get("/embedded")
    def embedded():
        now = time.time()
        items = []
        for e in ctx.embedded_snapshot():
            t = e["telemetry"]
            items.append({**e, "connectedForS": int(now - e["connectedAt"]),
                          "telemetryAgeS": int(now - e["telemetryAt"]) if e["telemetryAt"] else None,
                          "battery": {"mv": t.get("batteryMv"), "pct": t.get("batteryPct")} if ("batteryMv" in t or "batteryPct" in t) else None})
        return jsonify(embedded=items)

    @app.get("/diagnostics")
    def diagnostics():
        s = ctx.diagnostics.summary()
        return jsonify(**s, mqtt=ctx.mqtt_connected(), embedded=len(ctx.embedded_snapshot()),
                       devicesInTable=len(ctx.table.all()))

    @app.get("/errors")
    def errors():
        try:
            limit = int(request.args.get("limit", 50))
            dev = request.args.get("deviceId")
            dev_id = int(dev) if dev is not None else None
        except ValueError:
            return jsonify(error="limit y deviceId deben ser numeros"), 400
        return jsonify(errors=ctx.diagnostics.errors(limit, dev_id))

    return app


class HttpServer:
    """Arranca y detiene el servidor de Flask en un hilo."""

    def __init__(self, ctx: HealthContext) -> None:
        self._ctx = ctx
        self._server = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        cfg = self._ctx.cfg
        if cfg.host not in ("127.0.0.1", "localhost", "::1") and not cfg.token:
            log.warning("La API de salud escucha en %s sin token: cualquiera en la red puede leer el estado. Define [http].token.", cfg.host)
        self._server = make_server(cfg.host, cfg.port, create_app(self._ctx), threaded=True)
        self._thread = threading.Thread(target=self._server.serve_forever, name="http-health", daemon=True)
        self._thread.start()
        log.info("API de salud en http://%s:%d (GET /health, /devices, /embedded, /diagnostics, /errors)", cfg.host, cfg.port)

    @property
    def port(self) -> int:
        return self._server.server_port if self._server else self._ctx.cfg.port

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None
