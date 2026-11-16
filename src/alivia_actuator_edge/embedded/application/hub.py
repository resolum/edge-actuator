"""Sesiones con los ESP32 de actuadores: saludo, autorizacion por tabla, envio de comandos y espera de resultados."""
from __future__ import annotations

import asyncio
import hmac
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from alivia_actuator_edge.devices.domain.model import Device, DeviceKind, DeviceTable, InvalidMacAddress, MacAddress
from alivia_actuator_edge.dispatch.application.ports import EmbeddedTimeout, EmbeddedUnavailable
from alivia_actuator_edge.dispatch.domain.models import CommandOrder, EmbeddedResult
from alivia_actuator_edge.embedded.interfaces.transports.base import Link, LinkClosed
from alivia_actuator_edge.shared.config import ServerCfg

log = logging.getLogger("alivia.actuator.embedded")


@dataclass
class Session:
    link: Link
    base_mac: str
    embedded_id: str
    fw: str
    channels: dict[int, Device]  # solo los canales autorizados: numero -> dispositivo de la tabla
    pending: dict[str, asyncio.Future] = field(default_factory=dict)  # correlationId -> futuro del resultado
    connected_at: float = field(default_factory=time.time)
    telemetry: dict = field(default_factory=dict)  # ultima telemetria del ESP32 (bateria, memoria, senal...)
    telemetry_at: float = 0.0


def _kind(value) -> DeviceKind | None:
    try:
        return DeviceKind(str(value).upper())
    except ValueError:
        return None


class EmbeddedHub:
    def __init__(self, table: DeviceTable, cfg: ServerCfg) -> None:
        self._table = table
        self._cfg = cfg
        self._sessions: dict[str, Session] = {}  # por MAC base del ESP32
        self._by_device: dict[int, Session] = {}  # deviceId -> sesion que lo maneja
        self._listeners: list[Callable[[], None]] = []
        self._snapshot: tuple[dict, ...] = ()  # vista inmutable para otros hilos (API HTTP): se reemplaza entera

    # ---- consultas ----
    def add_listener(self, fn: Callable[[], None]) -> None:
        self._listeners.append(fn)

    def snapshot(self) -> tuple[dict, ...]:
        """Estado de los ESP32 conectados, seguro de leer desde otro hilo (se reemplaza entero, nunca se muta)."""
        return self._snapshot

    def _refresh(self) -> None:
        self._snapshot = tuple(
            {"deviceId": s.embedded_id, "mac": s.base_mac, "link": s.link.kind, "fw": s.fw, "connectedAt": s.connected_at,
             "channels": {c: {"deviceId": d.device_id, "mac": str(d.mac), "kind": d.kind.value} for c, d in sorted(s.channels.items())},
             "telemetry": dict(s.telemetry), "telemetryAt": s.telemetry_at or None, "pending": len(s.pending)}
            for s in self._sessions.values()
        )

    def online_summary(self) -> list[dict]:
        return [
            {"deviceId": s.embedded_id, "mac": s.base_mac, "link": s.link.kind,
             "devices": sorted(d.device_id for d in s.channels.values())}
            for s in self._sessions.values()
        ]

    def online_device_ids(self) -> set[int]:
        return set(self._by_device)

    def wifi_alive(self, base_mac: str) -> bool:
        """Lo usa el conector Bluetooth: si el ESP32 ya esta por WiFi no abre el puerto serie."""
        try:
            key = MacAddress(base_mac).value
        except InvalidMacAddress:
            return False
        s = self._sessions.get(key)
        return s is not None and s.link.kind == "ws" and not s.link.closed

    def _notify(self) -> None:
        self._refresh()
        for fn in self._listeners:
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("fallo un oyente de cambios de conexion")

    # ---- ciclo de vida de un enlace ----
    async def serve_link(self, link: Link) -> None:
        session = await self._handshake(link)
        if session is None:
            return
        try:
            while True:
                msg = await link.recv()
                self._on_message(session, msg)
        except LinkClosed as exc:
            log.info("[%s] enlace cerrado: %s", session.embedded_id, exc)
        finally:
            self._drop(session)

    async def _handshake(self, link: Link) -> Session | None:
        try:
            hello = await asyncio.wait_for(link.recv(), timeout=self._cfg.hello_timeout_s)
        except (asyncio.TimeoutError, LinkClosed):
            log.warning("[%s] no llego un hello a tiempo", link.peer)
            await link.close("sin hello")
            return None
        reason = self._check_hello(hello)
        if isinstance(reason, str):
            log.warning("[%s] hello rechazado: %s", link.peer, reason)
            try:
                await link.send_json({"type": "error", "reason": reason})
            except LinkClosed:
                pass
            await link.close(reason)
            return None
        base_mac, embedded_id, fw, channels = reason
        old = self._sessions.get(base_mac)
        if old is not None and not old.link.closed:
            if old.link.priority > link.priority:
                msg = "ya conectado por un transporte de mayor prioridad"
                log.info("[%s] %s", embedded_id, msg)
                try:
                    await link.send_json({"type": "error", "reason": msg})
                except LinkClosed:
                    pass
                await link.close(msg)
                return None
            log.info("[%s] reconexion: se reemplaza el enlace anterior (%s)", embedded_id, old.link.kind)
            self._drop(old)
            await old.link.close("reemplazado")
        session = Session(link, base_mac, embedded_id, fw, channels)
        self._sessions[base_mac] = session
        for d in channels.values():
            self._by_device[d.device_id] = session
        try:
            await link.send_json({"type": "ready", "channels": sorted(channels)})
        except LinkClosed:
            self._drop(session)
            return None
        log.info("[%s] conectado por %s con %d canales autorizados: %s", embedded_id, link.kind, len(channels),
                 {c: d.device_id for c, d in sorted(channels.items())})
        self._notify()
        return session

    def _check_hello(self, hello: dict):
        if hello.get("type") != "hello":
            return "se esperaba un hello"
        token = str(hello.get("token", ""))
        if not hmac.compare_digest(token.encode(), self._cfg.token.encode()):
            return "token invalido"
        try:
            base_mac = MacAddress(str(hello.get("mac", ""))).value
        except InvalidMacAddress:
            return "mac base invalida"
        raw = hello.get("channels")
        if not isinstance(raw, list) or not raw:
            return "faltan los canales"
        channels: dict[int, Device] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                number = int(item["channel"])
                mac = MacAddress(str(item["mac"]))
            except (KeyError, ValueError, TypeError, InvalidMacAddress):
                continue
            kind = _kind(item.get("kind"))
            device = self._table.find_by_mac(mac)
            if device is None or kind is None or device.kind is not kind or not device.usable:
                log.warning("canal %s (%s %s) no autorizado: no esta en la tabla, cambio de tipo o no esta IN_USE",
                            number, mac, item.get("kind"))
                continue
            channels[number] = device
        if not channels:
            return "ningun canal autorizado"
        return base_mac, str(hello.get("deviceId", base_mac)), str(hello.get("fw", "")), channels

    def _drop(self, session: Session) -> None:
        changed = False
        if self._sessions.get(session.base_mac) is session:
            del self._sessions[session.base_mac]
            changed = True
        for dev_id in [i for i, s in self._by_device.items() if s is session]:
            del self._by_device[dev_id]
            changed = True
        for fut in session.pending.values():
            if not fut.done():
                fut.set_exception(EmbeddedUnavailable("el enlace con el embebido se cayo"))
        session.pending.clear()
        if changed:
            self._notify()

    def _on_message(self, session: Session, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "telemetry":
            allowed = ("uptimeMs", "freeHeap", "rssi", "batteryMv", "batteryPct", "resetReason")
            session.telemetry = {k: msg[k] for k in allowed if isinstance(msg.get(k), (int, float, str))}
            session.telemetry_at = time.time()
            self._refresh()
            return
        if kind != "result":
            log.debug("[%s] mensaje %r ignorado", session.embedded_id, kind)
            return
        corr = str(msg.get("correlationId", ""))
        fut = session.pending.get(corr)
        if fut is None or fut.done():
            log.info("[%s] resultado tardio o desconocido %s, se descarta", session.embedded_id, corr)
            return
        try:
            res = EmbeddedResult(
                ok=bool(msg.get("ok")), detail=str(msg.get("detail", "") or ""), state=str(msg.get("state", "") or ""),
                angle=int(msg.get("angle", -1)), elapsed_ms=int(msg.get("elapsedMs", 0)),
            )
        except (ValueError, TypeError):
            res = EmbeddedResult(ok=False, detail="resultado mal formado del embebido")
        fut.set_result(res)

    # ---- puerto EmbeddedGateway ----
    async def execute(self, device: Device, order: CommandOrder, timeout_s: float) -> EmbeddedResult:
        session = self._by_device.get(device.device_id)
        if session is None or session.link.closed:
            raise EmbeddedUnavailable("sin ESP32 para este dispositivo")
        channel = next((c for c, d in session.channels.items() if d.device_id == device.device_id), None)
        if channel is None:
            raise EmbeddedUnavailable("el ESP32 no maneja este dispositivo")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        session.pending[order.correlation_id] = fut
        try:
            try:
                await session.link.send_json({
                    "type": "command", "correlationId": order.correlation_id, "channel": channel,
                    "mac": str(device.mac), "kind": device.kind.value, "action": order.action,
                    "ttlMs": int(timeout_s * 1000),
                })
            except LinkClosed as exc:
                raise EmbeddedUnavailable(str(exc)) from None
            try:
                return await asyncio.wait_for(fut, timeout=timeout_s)
            except asyncio.TimeoutError:
                raise EmbeddedTimeout() from None
        finally:
            session.pending.pop(order.correlation_id, None)

    async def close_all(self) -> None:
        for s in list(self._sessions.values()):
            await s.link.close("apagado")
