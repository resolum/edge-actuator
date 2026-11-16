from __future__ import annotations

import logging

from alivia_actuator_edge.dispatch.domain.models import Outcome

log = logging.getLogger("alivia.actuator.report")


class LogReporter:
    """Hasta que el backend tenga el endpoint de resultados, el reporte queda solo en el log."""

    async def report(self, outcome: Outcome) -> None:
        log.info("[backend pendiente] dispositivo=%s accion=%s ok=%s detalle=%s", outcome.device_id, outcome.action,
                 outcome.ok, outcome.detail)


class NullReporter:
    async def report(self, outcome: Outcome) -> None:
        return None
