"""Punta a punta: broker MQTT real (amqtt) + edge de actuadores completo + ESP32 falso por WebSocket."""
import asyncio
import json
import time

import pytest

pytest.importorskip("amqtt")
import paho.mqtt.client as mqtt  # noqa: E402
from amqtt.broker import Broker  # noqa: E402

from alivia_actuator_edge.app import run  # noqa: E402
from alivia_actuator_edge.shared.config import Config, DispatchCfg, HttpCfg, MqttCfg, ServerCfg  # noqa: E402
from tests.conftest import device_cfgs, free_port, make_order  # noqa: E402
from tools.fake_embebido import FakeEsp32  # noqa: E402

TOKEN = "e2e-token"


class Probe:
    """Cliente MQTT de prueba (hace de edge de voz): publica ordenes y recoge lo que el edge de actuadores emite."""

    def __init__(self, port):
        self.port = port
        self.msgs: list[tuple[str, dict]] = []
        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="probe")
        self.c.on_message = lambda c, u, m: self.msgs.append((m.topic, json.loads(m.payload) if m.payload else {}))

    async def start(self):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: self.c.connect("127.0.0.1", self.port))
        self.c.loop_start()
        self.c.subscribe([("alivia/actuators/+/result", 1), ("alivia/actuators/+/state", 1), ("alivia/actuators/status", 1)])

    def stop(self):
        self.c.loop_stop()
        self.c.disconnect()

    def send(self, device, payload: bytes):
        self.c.publish(f"alivia/actuators/{device}/command", payload, qos=1)

    def find(self, topic_part, pred=lambda b: True):
        return [b for t, b in self.msgs if topic_part in t and pred(b)]

    async def until(self, fn, timeout=5.0):
        t0 = time.monotonic()
        while True:
            r = fn()
            if r:
                return r
            if time.monotonic() - t0 > timeout:
                raise AssertionError(f"no llego lo esperado; mensajes: {self.msgs}")
            await asyncio.sleep(0.02)


@pytest.fixture
async def system():
    bport = free_port()
    broker = Broker({"listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{bport}"}}, "sys_interval": 0,
                     "auth": {"allow-anonymous": True, "plugins": ["auth_anonymous"]}, "topic-check": {"enabled": False}})
    await broker.start()
    wport = free_port()
    cfg = Config(server=ServerCfg(host="127.0.0.1", port=wport, token=TOKEN, ping_interval_s=5, ping_timeout_s=5),
                 mqtt=MqttCfg(port=bport), dispatch=DispatchCfg(exec_timeout_s=1.5), http=HttpCfg(port=0), devices=device_cfgs())
    stop, ready = asyncio.Event(), asyncio.Event()
    app = asyncio.create_task(run(cfg, stop, ready))
    await asyncio.wait_for(ready.wait(), 5)
    probe = Probe(bport)
    probe.http = ready.http  # type: ignore[attr-defined]
    await probe.start()
    # el edge publica su estado justo despues de suscribirse: si llego, ya esta escuchando las ordenes
    await probe.until(lambda: probe.find("status", lambda b: b.get("online")))
    esp_holder = {}

    async def add_esp(**kw):
        esp = FakeEsp32(f"ws://127.0.0.1:{wport}/ws", TOKEN, time_scale=0.05, **kw)
        esp_holder["task"] = asyncio.create_task(esp.run())
        await asyncio.wait_for(esp.ready.wait(), 5)
        return esp

    yield probe, add_esp
    probe.stop()
    if "task" in esp_holder:
        esp_holder["task"].cancel()
    stop.set()
    await asyncio.wait_for(app, 10)
    try:
        await asyncio.wait_for(broker.shutdown(), timeout=5.0)
    except TimeoutError:
        pass


async def test_orden_de_voz_mueve_el_servo_y_se_confirma_por_el_broker(system):
    probe, add_esp = system
    esp = await add_esp()
    await probe.until(lambda: probe.find("status", lambda b: b.get("online") and b.get("embedded")))
    probe.send(10, make_order(corr="e2e-1"))
    res = (await probe.until(lambda: probe.find("10/result")))[0]
    assert res["v"] == 2 and res["correlationId"] == "e2e-1" and res["ok"] is True and res["state"] == "open"
    assert esp.servo_writes == [(0, 90)]
    st = (await probe.until(lambda: probe.find("10/state")))[0]
    assert st["state"] == "open"


async def test_cada_servo_cumple_su_funcion(system):
    probe, add_esp = system
    esp = await add_esp()
    probe.send(10, make_order(10, "open", "a"))
    probe.send(11, make_order(11, "open", "b", "AA:BB:CC:00:00:11", "WINDOW"))
    probe.send(12, make_order(12, "close", "c", "AA:BB:CC:00:00:12", "CURTAIN"))
    probe.send(13, make_order(13, "on", "d", "AA:BB:CC:00:00:13", "LIGHT"))
    await probe.until(lambda: len(probe.find("/result")) == 4)
    got = {b["deviceId"]: (b["ok"], b["state"]) for b in probe.find("/result")}
    assert got == {10: (True, "open"), 11: (True, "open"), 12: (True, "closed"), 13: (True, "on")}
    assert sorted(esp.servo_writes) == [(0, 90), (1, 90), (2, 0), (3, 60)]


async def test_falla_del_servo_se_informa_con_ok_false(system):
    probe, add_esp = system
    await add_esp(fail={0: "trabado"})
    probe.send(10, make_order(corr="f1"))
    res = (await probe.until(lambda: probe.find("10/result")))[0]
    assert res["ok"] is False and res["detail"] == "trabado"
    assert probe.find("10/state") == []  # un fallo no cambia el ultimo estado conocido


async def test_sin_embebido_responde_error_explicito(system):
    probe, _ = system
    probe.send(10, make_order(corr="n1"))
    res = (await probe.until(lambda: probe.find("10/result")))[0]
    assert res["ok"] is False and res["detail"] == "embebido sin conexion"


async def test_embebido_mudo_responde_timeout_antes_que_el_edge_de_voz(system):
    probe, add_esp = system
    await add_esp(silent={3})
    t0 = time.monotonic()
    probe.send(13, make_order(13, "on", "m1", "AA:BB:CC:00:00:13", "LIGHT"))
    res = (await probe.until(lambda: probe.find("13/result")))[0]
    assert res["ok"] is False and res["detail"] == "sin respuesta del embebido"
    assert time.monotonic() - t0 < 4.0  # menor que dialog.exec_timeout_s del edge de voz (5 s)


async def test_duplicado_qos1_no_mueve_dos_veces(system):
    probe, add_esp = system
    esp = await add_esp()
    payload = make_order(corr="dup")
    probe.send(10, payload)
    await probe.until(lambda: probe.find("10/result"))
    probe.send(10, payload)
    await probe.until(lambda: len(probe.find("10/result")) == 2)
    assert esp.servo_writes == [(0, 90)]


async def test_orden_no_autorizada_no_toca_el_servo(system):
    probe, add_esp = system
    esp = await add_esp()
    probe.send(10, make_order(corr="x", mac="AA:BB:CC:00:00:99"))
    res = (await probe.until(lambda: probe.find("10/result")))[0]
    assert res["detail"] == "dispositivo no autorizado" and esp.servo_writes == []


async def test_orden_caducada(system):
    probe, add_esp = system
    esp = await add_esp()
    probe.send(10, make_order(corr="old", ts=int(time.time() * 1000) - 60_000, ttl=1000))
    res = (await probe.until(lambda: probe.find("10/result")))[0]
    assert res["detail"] == "caducado" and esp.servo_writes == []


async def test_estado_del_edge_se_publica_al_conectar_y_al_caer_el_esp32(system):
    probe, add_esp = system
    esp = await add_esp()
    await probe.until(lambda: probe.find("status", lambda b: b.get("embedded")))
    await esp.ws.close()
    await probe.until(lambda: probe.msgs and probe.find("status", lambda b: b.get("online") and b.get("embedded") == []))


async def test_api_de_salud_refleja_la_operacion_real(system):
    import urllib.request
    probe, add_esp = system
    loop = asyncio.get_running_loop()

    def get(path):
        with urllib.request.urlopen(f"http://127.0.0.1:{probe.http.port}{path}", timeout=3) as r:
            return json.loads(r.read())

    await add_esp(fail={0: "trabado"}, telemetry={"uptimeMs": 10, "freeHeap": 150000, "rssi": -60, "batteryMv": 3850})
    probe.send(10, make_order(corr="h1"))
    probe.send(13, make_order(13, "on", "h2", "AA:BB:CC:00:00:13", "LIGHT"))
    await probe.until(lambda: len(probe.find("/result")) == 2)
    await asyncio.sleep(0.2)
    health = await loop.run_in_executor(None, get, "/health")
    devs = {d["deviceId"]: d for d in (await loop.run_in_executor(None, get, "/devices"))["devices"]}
    errs = (await loop.run_in_executor(None, get, "/errors"))["errors"]
    emb = (await loop.run_in_executor(None, get, "/embedded"))["embedded"][0]
    assert health["status"] == "ok" and health["embedded"] == 1
    assert devs[10]["lastResult"]["detail"] == "trabado" and devs[13]["lastResult"]["ok"] is True and devs[13]["online"]
    assert errs[0]["deviceId"] == 10 and emb["battery"]["mv"] == 3850
