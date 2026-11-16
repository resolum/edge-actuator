from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest

from alivia_actuator_edge.devices.domain.model import Device, DeviceKind, MacAddress, StaticDeviceTable, Status
from alivia_actuator_edge.shared.config import DeviceCfg, DispatchCfg

FIXTURES = Path(__file__).resolve().parent.parent / "docs" / "fixtures"


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


def make_order(device_id=10, action="open", corr="c1", mac="AA:BB:CC:00:00:10", kind="DOOR", ts=None, ttl=5000, **extra) -> bytes:
    body = {"v": 2, "correlationId": corr, "orderId": "o1", "deviceId": device_id, "mac": mac, "deviceType": kind,
            "roomKey": "sala", "action": action, "params": {}, "source": "voz", "ts": ts if ts is not None else int(time.time() * 1000),
            "ttlMs": ttl}
    body.update(extra)
    return json.dumps(body).encode()


def build_table(status=Status.IN_USE) -> StaticDeviceTable:
    return StaticDeviceTable([
        Device(10, MacAddress("AA:BB:CC:00:00:10"), DeviceKind.DOOR, status, "entrada", "Puerta"),
        Device(11, MacAddress("AA:BB:CC:00:00:11"), DeviceKind.WINDOW, Status.IN_USE, "sala", "Ventana"),
        Device(12, MacAddress("AA:BB:CC:00:00:12"), DeviceKind.CURTAIN, Status.IN_USE, "sala", "Cortina"),
        Device(13, MacAddress("AA:BB:CC:00:00:13"), DeviceKind.LIGHT, Status.IN_USE, "sala", "Luz"),
    ])


def device_cfgs() -> list[DeviceCfg]:
    return [DeviceCfg(10, "AA:BB:CC:00:00:10", "DOOR", "IN_USE", "entrada", "Puerta"),
            DeviceCfg(11, "AA:BB:CC:00:00:11", "WINDOW", "IN_USE", "sala", "Ventana"),
            DeviceCfg(12, "AA:BB:CC:00:00:12", "CURTAIN", "IN_USE", "sala", "Cortina"),
            DeviceCfg(13, "AA:BB:CC:00:00:13", "LIGHT", "IN_USE", "sala", "Luz")]


@pytest.fixture
def table():
    return build_table()


@pytest.fixture
def dcfg():
    return DispatchCfg(exec_timeout_s=2.0, dedup_ttl_s=60.0, dedup_max=8)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
