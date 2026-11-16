"""Adaptador MQTT (paho) puenteado a asyncio. Habla el contrato v2 del edge de voz (docs/mqtt-contract.md)."""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from collections.abc import Awaitable, Callable

import paho.mqtt.client as mqtt

from alivia_actuator_edge.dispatch.domain.models import Outcome
from alivia_actuator_edge.shared.config import MqttCfg

log = logging.getLogger("alivia.actuator.mqtt")

CommandHandler = Callable[[str, bytes], Awaitable[None]]


class MqttGateway:
    """Implementa ResultPublisher y ademas recibe las ordenes y publica el estado del edge (retenido, con LWT)."""

    def __init__(self, cfg: MqttCfg, status_provider: Callable[[], list[dict]]) -> None:
        self._cfg = cfg
        self._status_provider = status_provider
        self._loop: asyncio.AbstractEventLoop | None = None
        self._handler: CommandHandler | None = None
        self._tasks: set[asyncio.Task] = set()
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cfg.client_id, clean_session=True)
        if cfg.username:
            self._client.username_pw_set(cfg.username, cfg.password)
        if cfg.tls:
            self._client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        self._client.will_set(cfg.status_topic, json.dumps({"v": 2, "online": False}), qos=cfg.qos, retain=True)
        self._client.reconnect_delay_set(min_delay=1, max_delay=15)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

    @property
    def connected(self) -> bool:
        return self._client.is_connected()

    async def start(self, handler: CommandHandler) -> None:
        self._loop = asyncio.get_running_loop()
        self._handler = handler
        self._client.connect_async(self._cfg.host, self._cfg.port, keepalive=30)
        self._client.loop_start()
        log.info("MQTT: conectando a %s:%d", self._cfg.host, self._cfg.port)

    async def stop(self) -> None:
        try:
            self.publish_status(online=False)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.1)
        self._client.disconnect()
        self._client.loop_stop()
        for t in list(self._tasks):
            t.cancel()

    # ---- callbacks de paho (hilo de red) ----
    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code != 0:
            log.error("MQTT: conexion rechazada (%s)", reason_code)
            return
        log.info("MQTT: conectado, suscrito a %s (QoS %d)", self._cfg.command_filter, self._cfg.qos)
        client.subscribe(self._cfg.command_filter, qos=self._cfg.qos)
        self.publish_status()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        log.warning("MQTT: desconectado (%s); paho reintenta solo", reason_code)

    def _on_message(self, client, userdata, msg) -> None:
        parts = msg.topic.split("/")
        if len(parts) != 4 or parts[0] != "alivia" or parts[1] != "actuators" or parts[3] != "command":
            return
        if self._loop is None or self._handler is None:
            return
        self._loop.call_soon_threadsafe(self._spawn, parts[2], bytes(msg.payload))

    def _spawn(self, device: str, payload: bytes) -> None:
        task = asyncio.create_task(self._handler(device, payload))  # type: ignore[misc]
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---- publicacion ----
    def _publish(self, topic: str, body: dict, retain: bool = False) -> None:
        info = self._client.publish(topic, json.dumps(body, separators=(",", ":"), ensure_ascii=False),
                                    qos=self._cfg.qos, retain=retain)
        if info.rc != mqtt.MQTT_ERR_SUCCESS and info.rc != mqtt.MQTT_ERR_NO_CONN:
            log.warning("MQTT: publish en %s devolvio %s", topic, info.rc)

    def publish_status(self, online: bool = True) -> None:
        body: dict = {"v": 2, "online": online}
        if online:
            body["embedded"] = self._status_provider()
        self._publish(self._cfg.status_topic, body, retain=True)

    async def publish_result(self, outcome: Outcome) -> None:
        body = {
            "v": 2, "correlationId": outcome.correlation_id, "ok": outcome.ok, "detail": outcome.detail,
            "ts": int(time.time() * 1000), "deviceId": outcome.device_id, "action": outcome.action,
            "state": outcome.state, "durationMs": outcome.elapsed_ms,
        }
        if outcome.device_id <= 0:
            log.warning("resultado %s sin deviceId valido: no se puede publicar", outcome.correlation_id)
            return
        self._publish(self._cfg.result_topic.format(device=outcome.device_id), body)

    async def publish_state(self, outcome: Outcome) -> None:
        body = {"v": 2, "deviceId": outcome.device_id, "state": outcome.state, "action": outcome.action,
                "correlationId": outcome.correlation_id, "ts": int(time.time() * 1000)}
        self._publish(self._cfg.state_topic.format(device=outcome.device_id), body, retain=True)
