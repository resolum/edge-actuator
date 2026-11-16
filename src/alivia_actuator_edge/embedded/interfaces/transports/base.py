from __future__ import annotations

from abc import ABC, abstractmethod


class LinkClosed(Exception):
    """El enlace con el embebido se cerró o dejó de responder."""


class Link(ABC):
    kind: str = "?"  # "ws" | "bt"
    peer: str = ""
    priority: int = 0  # si el mismo ESP32 aparece por dos transportes gana el de mayor prioridad (WiFi sobre Bluetooth)

    @abstractmethod
    async def recv(self) -> dict:
        """Siguiente mensaje JSON (objeto). Lanza LinkClosed al cerrarse."""

    @abstractmethod
    async def send_json(self, obj: dict) -> None: ...

    @abstractmethod
    async def close(self, reason: str = "") -> None: ...

    @property
    @abstractmethod
    def closed(self) -> bool: ...
