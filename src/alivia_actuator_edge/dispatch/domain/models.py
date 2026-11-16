"""Órdenes que llegan por MQTT (contrato v2) y su resultado."""
from __future__ import annotations

import json
from dataclasses import dataclass, field


class InvalidOrder(Exception):
    """El mensaje no es una orden válida. `correlation_id` es el que se pudo leer (vacío si ninguno)."""

    def __init__(self, reason: str, correlation_id: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.correlation_id = correlation_id


@dataclass(frozen=True)
class CommandOrder:
    correlation_id: str
    order_id: str
    device_id: int
    mac: str
    device_type: str
    room_key: str
    action: str
    params: dict
    source: str
    ts: int  # ms desde epoch, reloj del edge de voz
    ttl_ms: int


@dataclass(frozen=True)
class Outcome:
    """Resultado final de una orden, el mismo que se publica en MQTT y se reporta al backend."""

    correlation_id: str
    device_id: int
    action: str
    ok: bool
    detail: str = ""
    state: str = ""
    angle: int = -1
    elapsed_ms: int = 0
    order_id: str = ""
    source: str = ""
    mac: str = ""
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class EmbeddedResult:
    """Lo que contesta el ESP32."""

    ok: bool
    detail: str = ""
    state: str = ""
    angle: int = -1
    elapsed_ms: int = 0


def parse_order(topic_device: str, payload: bytes) -> CommandOrder:
    try:
        data = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        raise InvalidOrder("el mensaje no es JSON") from None
    if not isinstance(data, dict):
        raise InvalidOrder("el mensaje no es un objeto JSON")
    corr = str(data.get("correlationId", "") or "")
    if not corr:
        raise InvalidOrder("falta correlationId")
    if data.get("v") != 2:
        raise InvalidOrder("versión de contrato no soportada (se espera v=2)", corr)
    try:
        device_id = int(data["deviceId"])
        action = str(data["action"]).strip().lower()
        ts = int(data["ts"])
        ttl_ms = int(data.get("ttlMs", 15000))
    except (KeyError, ValueError, TypeError):
        raise InvalidOrder("faltan campos obligatorios (deviceId, action, ts)", corr) from None
    if not action:
        raise InvalidOrder("acción vacía", corr)
    if str(device_id) != str(topic_device):
        raise InvalidOrder("el deviceId del mensaje no coincide con el del topic", corr)
    params = data.get("params")
    return CommandOrder(
        correlation_id=corr,
        order_id=str(data.get("orderId", "") or ""),
        device_id=device_id,
        mac=str(data.get("mac", "") or ""),
        device_type=str(data.get("deviceType", "") or "").upper(),
        room_key=str(data.get("roomKey", "") or ""),
        action=action,
        params=params if isinstance(params, dict) else {},
        source=str(data.get("source", "") or ""),
        ts=ts,
        ttl_ms=ttl_ms,
    )
