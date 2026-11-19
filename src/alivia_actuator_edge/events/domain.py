"""Qué se informa al backend y cuándo vale la pena hacerlo."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

HELP_REQUESTED = "HELP_REQUESTED"
LOW_BATTERY = "LOW_BATTERY"
DEVICE_FAILURE = "DEVICE_FAILURE"

# Causas que no son del dispositivo: la orden se rechazó antes de llegar al servo. No se avisa al familiar.
NOT_DEVICE_FAULTS = ("caducado", "dispositivo no autorizado", "accion invalida")


@dataclass(frozen=True)
class EdgeEvent:
    """Un evento con su id: si se reenvía tras un corte de red, el backend no vuelve a notificar."""

    type: str
    device_id: int | None = None
    battery_pct: int | None = None
    detail: str = ""
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_json(self) -> dict:
        body: dict = {"eventId": self.event_id, "type": self.type}
        if self.device_id is not None:
            body["deviceId"] = self.device_id
        if self.battery_pct is not None:
            body["batteryPct"] = self.battery_pct
        if self.detail:
            body["detail"] = self.detail[:200]
        return body


class BatteryWatch:
    """Avisa una vez cuando la batería de un embebido baja del umbral, y otra vez solo después de recargarse."""

    def __init__(self, low_pct: int, rearm_pct: int) -> None:
        if not 0 < low_pct < rearm_pct <= 100:
            raise ValueError("se espera 0 < low_battery_pct < battery_rearm_pct <= 100")
        self._low, self._rearm = low_pct, rearm_pct
        self._warned: set[str] = set()

    def check(self, embedded_id: str, battery_pct) -> bool:
        """True cuando hay que avisar ahora."""
        try:
            pct = float(battery_pct)
        except (TypeError, ValueError):
            return False
        if pct >= self._rearm:
            self._warned.discard(embedded_id)
            return False
        if pct <= self._low and embedded_id not in self._warned:
            self._warned.add(embedded_id)
            return True
        return False


class FailureFilter:
    """Una falla del mismo dispositivo se avisa a lo más una vez por ventana, aunque la persona reintente."""

    def __init__(self, cooldown_s: float) -> None:
        self._cooldown = cooldown_s
        self._last: dict[int, float] = {}

    def should_report(self, device_id: int, detail: str, now: float) -> bool:
        if any(detail.startswith(cause) for cause in NOT_DEVICE_FAULTS):
            return False
        last = self._last.get(device_id)
        if last is not None and now - last < self._cooldown:
            return False
        self._last[device_id] = now
        return True
