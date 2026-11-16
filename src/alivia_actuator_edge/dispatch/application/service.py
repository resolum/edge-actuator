"""Caso de uso: recibir una orden del edge de voz, moverla al embebido y contestar siempre (ok o fallo)."""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Callable

from alivia_actuator_edge.devices.domain.model import STATE_AFTER, DeviceTable, MacAddress, InvalidMacAddress
from alivia_actuator_edge.dispatch.application.ports import (
    EmbeddedGateway, EmbeddedTimeout, EmbeddedUnavailable, ResultPublisher, ResultReporter,
)
from alivia_actuator_edge.dispatch.domain.models import CommandOrder, InvalidOrder, Outcome, parse_order
from alivia_actuator_edge.shared.config import DispatchCfg

log = logging.getLogger("alivia.actuator.dispatch")


def _now_ms() -> int:
    return int(time.time() * 1000)


class ActuatorService:
    def __init__(self, table: DeviceTable, gateway: EmbeddedGateway, publisher: ResultPublisher,
                 reporter: ResultReporter, cfg: DispatchCfg, now_ms: Callable[[], int] = _now_ms) -> None:
        self._table = table
        self._gateway = gateway
        self._publisher = publisher
        self._reporter = reporter
        self._cfg = cfg
        self._now = now_ms
        self._done: OrderedDict[str, tuple[Outcome, int]] = OrderedDict()  # correlationId -> (resultado, vence)
        self._inflight: set[str] = set()

    # ---- entrada ----
    async def handle(self, topic_device: str, payload: bytes) -> None:
        try:
            order = parse_order(topic_device, payload)
        except InvalidOrder as exc:
            log.warning("orden invalida (%s): %s", topic_device, exc.reason)
            if exc.correlation_id:  # sin correlationId nadie esperaria la respuesta
                dev = int(topic_device) if str(topic_device).isdigit() else 0
                await self._finish(Outcome(exc.correlation_id, dev, "", False, f"orden invalida: {exc.reason}"), save=False)
            return
        cached = self._lookup(order.correlation_id)
        if cached is not None:  # QoS 1 duplicó el mensaje: se repite el resultado sin mover nada
            log.info("duplicado %s: se reenvia el resultado guardado", order.correlation_id)
            await self._publisher.publish_result(cached)
            return
        if order.correlation_id in self._inflight:
            log.info("duplicado %s en curso: se ignora", order.correlation_id)
            return
        self._inflight.add(order.correlation_id)
        try:
            outcome = await self._run(order)
        except Exception as exc:  # nunca dejar al edge de voz sin respuesta
            log.exception("error inesperado con %s", order.correlation_id)
            outcome = self._fail(order, f"error interno: {type(exc).__name__}")
        finally:
            self._inflight.discard(order.correlation_id)
        await self._finish(outcome, save=True)

    # ---- logica ----
    async def _run(self, order: CommandOrder) -> Outcome:
        age = self._now() - order.ts
        if age > order.ttl_ms:
            return self._fail(order, "caducado")
        device = self._table.get(order.device_id)
        if device is None:
            return self._fail(order, "dispositivo no autorizado")
        try:
            mac_ok = MacAddress(order.mac) == device.mac
        except InvalidMacAddress:
            mac_ok = False
        if not mac_ok or not device.usable or (order.device_type and order.device_type != device.kind.value):
            log.warning("orden rechazada para %s: mac/estado/tipo no coinciden con la tabla", order.device_id)
            return self._fail(order, "dispositivo no autorizado")
        if order.action not in device.kind.actions:
            return self._fail(order, f"accion invalida para {device.kind.value}")
        remaining = order.ttl_ms - age
        timeout = min(self._cfg.exec_timeout_s, max(0.5, remaining / 1000.0))
        try:
            res = await self._gateway.execute(device, order, timeout)
        except EmbeddedUnavailable:
            return self._fail(order, "embebido sin conexion")
        except EmbeddedTimeout:
            return self._fail(order, "sin respuesta del embebido")
        if res.ok:
            state = res.state or STATE_AFTER[order.action]
            return Outcome(order.correlation_id, order.device_id, order.action, True, "", state, res.angle,
                           res.elapsed_ms, order.order_id, order.source, str(device.mac))
        return Outcome(order.correlation_id, order.device_id, order.action, False, res.detail or "fallo en el embebido",
                       "", res.angle, res.elapsed_ms, order.order_id, order.source, str(device.mac))

    @staticmethod
    def _fail(order: CommandOrder, detail: str) -> Outcome:
        return Outcome(order.correlation_id, order.device_id, order.action, False, detail,
                       order_id=order.order_id, source=order.source, mac=order.mac)

    async def _finish(self, outcome: Outcome, save: bool) -> None:
        if save:
            self._remember(outcome)
        log.info("resultado %s dispositivo=%s accion=%s ok=%s %s", outcome.correlation_id, outcome.device_id,
                 outcome.action, outcome.ok, outcome.detail)
        try:
            await self._publisher.publish_result(outcome)
            if outcome.ok:
                await self._publisher.publish_state(outcome)
        except Exception:
            log.exception("no se pudo publicar el resultado %s en el broker", outcome.correlation_id)
        try:
            await self._reporter.report(outcome)
        except Exception:
            log.exception("el reporte del resultado %s fallo", outcome.correlation_id)

    # ---- duplicados ----
    def _remember(self, outcome: Outcome) -> None:
        now = self._now()
        self._done[outcome.correlation_id] = (outcome, now + int(self._cfg.dedup_ttl_s * 1000))
        self._done.move_to_end(outcome.correlation_id)
        while len(self._done) > self._cfg.dedup_max:
            self._done.popitem(last=False)

    def _lookup(self, corr: str) -> Outcome | None:
        item = self._done.get(corr)
        if item is None:
            return None
        if item[1] < self._now():
            del self._done[corr]
            return None
        return item[0]
