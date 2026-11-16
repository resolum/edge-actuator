from __future__ import annotations

from typing import Protocol

from alivia_actuator_edge.devices.domain.model import Device
from alivia_actuator_edge.dispatch.domain.models import CommandOrder, EmbeddedResult, Outcome


class EmbeddedUnavailable(Exception):
    """No hay un embebido conectado que maneje ese dispositivo (o el enlace cayó durante la orden)."""


class EmbeddedTimeout(Exception):
    """El embebido no contestó a tiempo."""


class EmbeddedGateway(Protocol):
    async def execute(self, device: Device, order: CommandOrder, timeout_s: float) -> EmbeddedResult: ...


class ResultPublisher(Protocol):
    """Hacia el edge de voz (por el broker MQTT)."""

    async def publish_result(self, outcome: Outcome) -> None: ...

    async def publish_state(self, outcome: Outcome) -> None: ...


class ResultReporter(Protocol):
    """Hacia el backend. Hoy solo se registra en el log; el cliente HTTP se agrega cuando el backend tenga el endpoint.
    Una implementación nunca debe lanzar: un fallo al reportar no cambia el resultado de la orden."""

    async def report(self, outcome: Outcome) -> None: ...
