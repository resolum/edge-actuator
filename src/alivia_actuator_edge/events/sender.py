"""Cliente HTTP mínimo (biblioteca estándar) de POST /api/v1/edge/events."""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

from alivia_actuator_edge.events.domain import EdgeEvent

EVENTS_PATH = "/api/v1/edge/events"


class RetryableError(Exception):
    """El backend no contestó o pidió reintentar (red, 429, 5xx). `retry_after` en segundos si lo indicó."""

    def __init__(self, reason: str, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.retry_after = retry_after


class RejectedError(Exception):
    """El backend rechazó el evento (400, 401, 403, 404): reintentarlo no cambia nada."""


class HttpEventSender:
    def __init__(self, base_url: str, credential: str, timeout_s: float = 10.0) -> None:
        if not credential:
            raise ValueError("falta la credencial del edge (formato <edgeId>.<secreto>)")
        self._url = base_url.rstrip("/") + EVENTS_PATH
        self._credential = credential
        self._timeout = timeout_s

    async def send(self, event: EdgeEvent) -> int:
        """Entrega el evento; devuelve cuántos familiares se notificaron."""
        return await asyncio.to_thread(self._post, event)

    def _post(self, event: EdgeEvent) -> int:
        request = urllib.request.Request(
            self._url, data=json.dumps(event.to_json()).encode(), method="POST",
            headers={"Authorization": f"Bearer {self._credential}", "Content-Type": "application/json",
                     "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            code = exc.code
            if code in (429, 502, 503, 504) or code >= 500:
                raise RetryableError(f"HTTP {code}", _retry_after(exc.headers.get("Retry-After"))) from None
            raise RejectedError(f"HTTP {code}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise RetryableError(f"red: {exc.__class__.__name__}") from None
        try:
            return int(json.loads(body or b"{}").get("notified", 0))
        except (ValueError, AttributeError, TypeError):
            return 0


def _retry_after(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None
