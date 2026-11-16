"""Contadores y errores recientes de las ordenes. Es un ResultReporter: recibe cada resultado final."""
from __future__ import annotations

import threading
import time
from collections import Counter, deque

from alivia_actuator_edge.dispatch.domain.models import Outcome


class Diagnostics:
    def __init__(self, errors_kept: int = 200) -> None:
        self._lock = threading.Lock()  # lo escribe el loop asyncio y lo lee el hilo de Flask
        self.started_at = time.time()
        self._total = 0
        self._ok = 0
        self._by_detail: Counter[str] = Counter()
        self._errors: deque[dict] = deque(maxlen=errors_kept)
        self._last: dict[int, dict] = {}  # deviceId -> ultimo resultado
        self._per_device: dict[int, Counter[str]] = {}

    async def report(self, o: Outcome) -> None:
        now = time.time()
        entry = {"ts": now, "deviceId": o.device_id, "action": o.action, "ok": o.ok, "detail": o.detail,
                 "state": o.state, "elapsedMs": o.elapsed_ms, "correlationId": o.correlation_id, "orderId": o.order_id}
        with self._lock:
            self._total += 1
            counter = self._per_device.setdefault(o.device_id, Counter())
            counter["total"] += 1
            if o.ok:
                self._ok += 1
                counter["ok"] += 1
            else:
                self._by_detail[o.detail or "sin detalle"] += 1
                counter["fail"] += 1
                self._errors.appendleft(entry)
            self._last[o.device_id] = entry

    def summary(self) -> dict:
        with self._lock:
            return {"startedAt": self.started_at, "uptimeS": int(time.time() - self.started_at), "orders": self._total,
                    "ok": self._ok, "failed": self._total - self._ok, "failuresByDetail": dict(self._by_detail)}

    def last(self, device_id: int) -> dict | None:
        with self._lock:
            return self._last.get(device_id)

    def device_counts(self, device_id: int) -> dict:
        with self._lock:
            c = self._per_device.get(device_id, Counter())
            return {"total": c["total"], "ok": c["ok"], "fail": c["fail"]}

    def errors(self, limit: int = 50, device_id: int | None = None) -> list[dict]:
        with self._lock:
            items = [e for e in self._errors if device_id is None or e["deviceId"] == device_id]
        return items[: max(1, min(limit, 500))]


class CompositeReporter:
    """Reparte cada resultado a varios reporteros (log, diagnostico, mas adelante el backend)."""

    def __init__(self, *reporters) -> None:
        self._reporters = reporters

    async def report(self, o: Outcome) -> None:
        for r in self._reporters:
            try:
                await r.report(o)
            except Exception:  # noqa: BLE001 - un reportero caido no afecta a los demas
                import logging
                logging.getLogger("alivia.actuator.report").exception("fallo un reportero")
