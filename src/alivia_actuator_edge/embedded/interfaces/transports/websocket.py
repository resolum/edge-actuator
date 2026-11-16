"""Servidor WebSocket para el ESP32 de actuadores (cliente de ws://EDGE:8766/ws).

El ESP32 solo habla cuando hay una orden, así que NO se puede detectar la caída por falta de datos como en el nodo de
voz: aquí el servidor hace ping WebSocket (la librería del ESP32 contesta sola) y cierra si no hay pong.
Sin subprotocolo y sin permessage-deflate; mensajes de texto sin fragmentar.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from http import HTTPStatus

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from alivia_actuator_edge.embedded.interfaces.transports.base import Link, LinkClosed
from alivia_actuator_edge.shared.config import ServerCfg

log = logging.getLogger(__name__)


class WsLink(Link):
    kind = "ws"
    priority = 2

    def __init__(self, ws: ServerConnection) -> None:
        self._ws = ws
        self.peer = str(ws.remote_address)

    @property
    def closed(self) -> bool:
        return self._ws.close_code is not None

    async def recv(self) -> dict:
        while True:
            try:
                msg = await self._ws.recv()
            except ConnectionClosed:
                raise LinkClosed("conexion cerrada") from None
            if isinstance(msg, bytes):
                log.warning("[%s] mensaje binario ignorado (este enlace solo usa JSON)", self.peer)
                continue
            try:
                obj = json.loads(msg)
            except ValueError:
                log.warning("[%s] JSON invalido ignorado: %r", self.peer, msg[:80])
                continue
            if isinstance(obj, dict):
                return obj
            log.warning("[%s] mensaje que no es un objeto ignorado", self.peer)

    async def send_json(self, obj: dict) -> None:
        try:
            await self._ws.send(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))
        except ConnectionClosed:
            raise LinkClosed("conexion cerrada") from None

    async def close(self, reason: str = "") -> None:
        try:
            await self._ws.close(1000, reason[:100])
        except Exception:  # noqa: BLE001 - cierre best-effort
            pass


async def serve_ws(cfg: ServerCfg, on_link: Callable[[Link], Awaitable[None]]):
    async def handler(ws: ServerConnection) -> None:
        await on_link(WsLink(ws))

    def process_request(connection: ServerConnection, request):
        if request.path.split("?")[0] != cfg.path:
            return connection.respond(HTTPStatus.NOT_FOUND, "ruta no valida\n")
        return None

    return await serve(
        handler, cfg.host, cfg.port, process_request=process_request, compression=None,
        ping_interval=cfg.ping_interval_s, ping_timeout=cfg.ping_timeout_s, max_size=16 * 1024,
    )
