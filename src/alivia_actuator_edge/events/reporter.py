"""Cola de eventos hacia el backend: nunca bloquea al que avisa y reintenta mientras no haya red."""
from __future__ import annotations

import asyncio
import collections
import logging
import time
from typing import Callable, Protocol

from alivia_actuator_edge.dispatch.domain.models import Outcome
from alivia_actuator_edge.events.domain import (
    DEVICE_FAILURE, LOW_BATTERY, BatteryWatch, EdgeEvent, FailureFilter,
)
from alivia_actuator_edge.events.sender import RejectedError, RetryableError

log = logging.getLogger("alivia.actuator.events")

VERBS = {"open": "abrir", "close": "cerrar", "on": "encender", "off": "apagar"}


class EventSender(Protocol):
    async def send(self, event: EdgeEvent) -> int: ...


class EventReporter:
    """Guarda los eventos en memoria (hasta `queue_max`) y los entrega en orden, con reintentos crecientes."""

    def __init__(self, sender: EventSender, *, queue_max: int = 200, retry_max_s: float = 60.0,
                 first_retry_s: float = 1.0) -> None:
        self._sender = sender
        self._pending: collections.deque[EdgeEvent] = collections.deque(maxlen=queue_max)
        self._wake = asyncio.Event()
        self._retry_max, self._first_retry = retry_max_s, first_retry_s
        self._task: asyncio.Task | None = None
        self.delivered = 0

    def submit(self, event: EdgeEvent) -> None:
        if len(self._pending) == self._pending.maxlen:
            log.warning("cola de eventos llena: se descarta el más antiguo")
        self._pending.append(event)
        self._wake.set()

    @property
    def pending(self) -> int:
        return len(self._pending)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="edge-events")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        delay = self._first_retry
        while True:
            if not self._pending:
                self._wake.clear()
                await self._wake.wait()
                continue
            event = self._pending[0]
            try:
                notified = await self._sender.send(event)
            except RetryableError as exc:
                wait = exc.retry_after if exc.retry_after is not None else delay
                log.warning("evento %s %s no entregado (%s); reintento en %.0f s", event.type, event.event_id, exc, wait)
                await asyncio.sleep(wait)
                delay = min(delay * 2, self._retry_max)
                continue
            except RejectedError as exc:
                log.error("el backend rechazó el evento %s %s: %s; se descarta", event.type, event.event_id, exc)
            except Exception:
                log.exception("error inesperado al entregar el evento %s; se descarta", event.event_id)
            else:
                self.delivered += 1
                log.info("evento %s entregado: %d familiar(es) notificado(s)", event.type, notified)
            delay = self._first_retry
            if self._pending and self._pending[0] is event:
                self._pending.popleft()


class FailureEventReporter:
    """ResultReporter: una orden que falló en el dispositivo se avisa al familiar (con límite por dispositivo)."""

    def __init__(self, events: EventReporter, failures: FailureFilter, now: Callable[[], float] = time.monotonic) -> None:
        self._events, self._failures, self._now = events, failures, now

    async def report(self, outcome: Outcome) -> None:
        if outcome.ok or not self._failures.should_report(outcome.device_id, outcome.detail, self._now()):
            return
        verb = VERBS.get(outcome.action, outcome.action)
        self._events.submit(EdgeEvent(DEVICE_FAILURE, device_id=outcome.device_id,
                                      detail=f"no se pudo {verb} ({outcome.detail})"))


class BatteryEventReporter:
    """Escucha la telemetría de cada embebido y avisa cuando su batería baja del umbral."""

    def __init__(self, events: EventReporter, watch: BatteryWatch) -> None:
        self._events, self._watch = events, watch

    def on_telemetry(self, embedded_id: str, device_ids: list[int], telemetry: dict) -> None:
        pct = telemetry.get("batteryPct")
        if pct is None or not self._watch.check(embedded_id, pct):
            return
        device_id = min(device_ids) if device_ids else None
        self._events.submit(EdgeEvent(LOW_BATTERY, device_id=device_id, battery_pct=int(float(pct))))
