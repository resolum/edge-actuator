"""Configuración del edge de actuadores (config.toml)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass
class ServerCfg:
    host: str = "0.0.0.0"
    port: int = 8766  # el edge de voz usa 8765: pueden correr los dos en la misma PC
    path: str = "/ws"
    token: str = ""  # si está vacío se usa la variable de entorno ALIVIA_ACTUATOR_WS_TOKEN
    hello_timeout_s: float = 5.0
    ping_interval_s: float = 10.0  # el servidor hace ping; sin pong en ping_timeout_s se cierra
    ping_timeout_s: float = 10.0


@dataclass
class BtLinkCfg:
    mac: str = ""  # MAC base del ESP32 (la del hello); evita abrir Bluetooth si ya hay WiFi
    port: str = ""  # COM5 (Windows, puerto saliente), /dev/rfcomm0 (Linux) o /dev/cu.<nombre> (macOS)
    baudrate: int = 115200


@dataclass
class BluetoothCfg:
    enabled: bool = False
    links: list[BtLinkCfg] = field(default_factory=list)
    retry_min_s: float = 2.0
    retry_max_s: float = 15.0


@dataclass
class MqttCfg:
    host: str = "127.0.0.1"
    port: int = 1883
    username: str = ""
    password: str = ""
    tls: bool = False
    client_id: str = "alivia-actuator-edge"
    command_filter: str = "alivia/actuators/+/command"
    result_topic: str = "alivia/actuators/{device}/result"
    state_topic: str = "alivia/actuators/{device}/state"  # retenido: última acción lograda de cada dispositivo
    status_topic: str = "alivia/actuators/status"  # retenido, con LWT: {"online":true,"embedded":[...]}
    qos: int = 1


@dataclass
class DispatchCfg:
    exec_timeout_s: float = 4.0  # debe ser MENOR que [dialog].exec_timeout_s del edge de voz (por defecto 5 s + margen)
    dedup_ttl_s: float = 300.0  # cuánto se recuerda un correlationId ya resuelto (QoS 1 puede duplicar)
    dedup_max: int = 512


@dataclass
class HttpCfg:
    """API de salud y diagnostico (Flask). Solo lectura. Por defecto escucha solo en esta PC."""

    enabled: bool = True
    host: str = "127.0.0.1"  # 0.0.0.0 para verla desde otra maquina de la red (entonces define token)
    port: int = 8780
    token: str = ""  # si no esta vacio, se exige "Authorization: Bearer <token>" (excepto en /health)
    errors_kept: int = 200  # cuantos errores recientes se recuerdan


@dataclass
class DeviceCfg:
    device_id: int = 0
    mac: str = ""  # MAC lógica del canal (la que se registró en el backend para este dispositivo)
    type: str = "DOOR"  # DOOR | WINDOW | CURTAIN | LIGHT
    status: str = "IN_USE"
    room_key: str = ""
    function: str = ""


@dataclass
class Config:
    server: ServerCfg = field(default_factory=ServerCfg)
    bluetooth: BluetoothCfg = field(default_factory=BluetoothCfg)
    mqtt: MqttCfg = field(default_factory=MqttCfg)
    dispatch: DispatchCfg = field(default_factory=DispatchCfg)
    http: HttpCfg = field(default_factory=HttpCfg)
    devices: list[DeviceCfg] = field(default_factory=list)  # tabla local (más adelante vendrá del backend)
    base_dir: Path = field(default_factory=Path.cwd)


def _build(cls, data: dict):
    kwargs = {}
    known = {f.name: f for f in fields(cls)}
    for key, value in data.items():
        if key not in known:
            raise ValueError(f"opción desconocida en config.toml: {cls.__name__}.{key}")
        kwargs[key] = value
    return cls(**kwargs)


def load_config(path: str | os.PathLike[str]) -> Config:
    p = Path(path).resolve()
    with p.open("rb") as fh:
        raw = tomllib.load(fh)
    cfg = Config(base_dir=p.parent)
    for name, cls in (("server", ServerCfg), ("mqtt", MqttCfg), ("dispatch", DispatchCfg), ("http", HttpCfg)):
        if name in raw:
            setattr(cfg, name, _build(cls, raw[name]))
    if "bluetooth" in raw:
        bt = dict(raw["bluetooth"])
        links = [_build(BtLinkCfg, item) for item in bt.pop("links", [])]
        cfg.bluetooth = _build(BluetoothCfg, bt)
        cfg.bluetooth.links = links
    cfg.devices = [_build(DeviceCfg, item) for item in raw.get("devices", [])]
    unknown = set(raw) - {"server", "mqtt", "dispatch", "http", "bluetooth", "devices"}
    if unknown:
        raise ValueError(f"secciones desconocidas en config.toml: {sorted(unknown)}")
    if not cfg.server.token:
        cfg.server.token = os.environ.get("ALIVIA_ACTUATOR_WS_TOKEN", "")
    if not cfg.server.token:
        raise ValueError("Falta el token: define [server].token o la variable ALIVIA_ACTUATOR_WS_TOKEN")
    return cfg
