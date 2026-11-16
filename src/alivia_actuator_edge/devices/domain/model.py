"""Dispositivos de la casa que este edge puede mover. Reflejan el contrato del backend (contexto assets)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

_MAC = re.compile(r"^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$|^(?:[0-9A-F]{2}-){5}[0-9A-F]{2}$")


class InvalidMacAddress(ValueError):
    pass


@dataclass(frozen=True)
class MacAddress:
    """Forma canónica XX:XX:XX:XX:XX:XX (mayúsculas, con dos puntos), igual que en el backend."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise InvalidMacAddress("la MAC debe ser texto")
        text = self.value.strip().upper()
        if not _MAC.match(text):
            raise InvalidMacAddress(f"MAC inválida: {self.value!r} (formato XX:XX:XX:XX:XX:XX)")
        object.__setattr__(self, "value", text.replace("-", ":"))

    def __str__(self) -> str:
        return self.value


class DeviceKind(str, Enum):
    DOOR = "DOOR"
    WINDOW = "WINDOW"
    CURTAIN = "CURTAIN"
    LIGHT = "LIGHT"

    @property
    def actions(self) -> frozenset[str]:
        return _ACTIONS[self]


_ACTIONS = {
    DeviceKind.DOOR: frozenset({"open", "close"}),
    DeviceKind.WINDOW: frozenset({"open", "close"}),
    DeviceKind.CURTAIN: frozenset({"open", "close"}),
    DeviceKind.LIGHT: frozenset({"on", "off"}),
}

# Estado en el que queda el dispositivo tras lograr cada acción (el firmware usa los mismos nombres).
STATE_AFTER = {"open": "open", "close": "closed", "on": "on", "off": "off"}


class Status(str, Enum):
    AVAILABLE = "AVAILABLE"
    IN_USE = "IN_USE"
    MAINTENANCE = "MAINTENANCE"
    BROKEN = "BROKEN"
    RETIRED = "RETIRED"


@dataclass(frozen=True)
class Device:
    device_id: int
    mac: MacAddress
    kind: DeviceKind
    status: Status
    room_key: str = ""
    function: str = ""

    @property
    def usable(self) -> bool:
        """Solo un dispositivo IN_USE recibe comandos (misma regla que el backend aplica a los micrófonos)."""
        return self.status is Status.IN_USE


class DeviceTable(Protocol):
    """Fuente de la tabla de dispositivos. Hoy es local (config.toml); luego puede venir del backend."""

    def get(self, device_id: int) -> Device | None: ...

    def find_by_mac(self, mac: MacAddress) -> Device | None: ...

    def all(self) -> tuple[Device, ...]: ...


class StaticDeviceTable:
    def __init__(self, devices: list[Device] | tuple[Device, ...]) -> None:
        self._devices = tuple(sorted(devices, key=lambda d: d.device_id))
        ids = [d.device_id for d in self._devices]
        macs = [d.mac for d in self._devices]
        if len(set(ids)) != len(ids):
            raise ValueError("deviceId repetido en la tabla de dispositivos")
        if len(set(macs)) != len(macs):
            raise ValueError("MAC repetida en la tabla de dispositivos")

    def get(self, device_id: int) -> Device | None:
        return next((d for d in self._devices if d.device_id == device_id), None)

    def find_by_mac(self, mac: MacAddress) -> Device | None:
        return next((d for d in self._devices if d.mac == mac), None)

    def all(self) -> tuple[Device, ...]:
        return self._devices

    @classmethod
    def from_config(cls, items) -> "StaticDeviceTable":
        devices = []
        for item in items:
            try:
                devices.append(Device(
                    device_id=int(item.device_id), mac=MacAddress(item.mac), kind=DeviceKind(str(item.type).upper()),
                    status=Status(str(item.status).upper()), room_key=item.room_key, function=item.function,
                ))
            except (ValueError, KeyError) as exc:
                raise ValueError(f"dispositivo inválido en config.toml ({item}): {exc}") from exc
        return cls(devices)
