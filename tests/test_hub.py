import asyncio
import json

import pytest
import websockets

from alivia_actuator_edge.devices.domain.model import Device, DeviceKind, MacAddress, StaticDeviceTable, Status
from alivia_actuator_edge.dispatch.application.ports import EmbeddedTimeout, EmbeddedUnavailable
from alivia_actuator_edge.dispatch.domain.models import CommandOrder
from alivia_actuator_edge.embedded.application.hub import EmbeddedHub
from alivia_actuator_edge.embedded.interfaces.transports.bluetooth import BtLink
from alivia_actuator_edge.embedded.interfaces.transports.websocket import serve_ws
from alivia_actuator_edge.shared.config import ServerCfg
from tests.conftest import build_table, free_port, load_fixture
from tests.fakes import FakeSerial
from tools.fake_embebido import FakeEsp32

TOKEN = "secreto-de-prueba"


def order(corr="k1", action="open", device_id=10):
    return CommandOrder(corr, "o", device_id, "", "", "", action, {}, "t", 0, 5000)


async def wait_until(pred, timeout=3.0):
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    while not pred():
        if loop.time() - t0 > timeout:
            raise AssertionError("no se cumplio la condicion a tiempo")
        await asyncio.sleep(0.01)


@pytest.fixture
async def stack():
    port = free_port()
    cfg = ServerCfg(host="127.0.0.1", port=port, token=TOKEN, hello_timeout_s=0.5, ping_interval_s=5, ping_timeout_s=5)
    table = build_table()
    hub = EmbeddedHub(table, cfg)
    server = await serve_ws(cfg, hub.serve_link)
    yield hub, table, f"ws://127.0.0.1:{port}/ws"
    server.close()
    await hub.close_all()
    await server.wait_closed()


async def connect(url, **kw):
    esp = FakeEsp32(url, TOKEN, time_scale=0.05, **kw)
    task = asyncio.create_task(esp.run())
    return esp, task


async def test_hello_valido_deja_los_4_canales_online(stack):
    hub, table, url = stack
    esp, task = await connect(url)
    await asyncio.wait_for(esp.ready.wait(), 3)
    assert esp.last_reply == {"type": "ready", "channels": [0, 1, 2, 3]}
    assert hub.online_device_ids() == {10, 11, 12, 13}
    assert hub.online_summary()[0]["link"] == "ws"
    task.cancel()


async def test_ejecucion_exitosa_y_reglas_del_firmware(stack):
    hub, table, url = stack
    esp, task = await connect(url)
    await asyncio.wait_for(esp.ready.wait(), 3)
    res = await hub.execute(table.get(10), order(), 3)
    assert res.ok and res.state == "open" and res.angle == 90
    res = await hub.execute(table.get(13), order("l1", "on", 13), 3)
    assert res.ok and res.state == "on"
    assert esp.servo_writes == [(0, 90), (3, 60)]
    task.cancel()


async def test_token_incorrecto_se_rechaza(stack):
    hub, _, url = stack
    esp = FakeEsp32(url, "otro-token")
    await asyncio.wait_for(esp.run(), 3)  # el edge responde error y cierra: run() termina
    assert esp.last_reply == {"type": "error", "reason": "token invalido"}
    assert hub.online_device_ids() == set()


async def test_canales_fuera_de_tabla_quedan_denegados(stack):
    hub, table, url = stack
    esp, task = await connect(url, channel_macs={1: "02:00:00:00:00:99", 2: "02:00:00:00:00:98"})
    await asyncio.wait_for(esp.ready.wait(), 3)
    assert esp.last_reply["channels"] == [0, 3]
    assert hub.online_device_ids() == {10, 13}
    with pytest.raises(EmbeddedUnavailable):
        await hub.execute(table.get(11), order("v1", "open", 11), 1)
    task.cancel()


async def test_ningun_canal_autorizado(stack):
    hub, _, url = stack
    esp = FakeEsp32(url, TOKEN, channel_macs={0: "02:00:00:00:00:01", 1: "02:00:00:00:00:02", 2: "02:00:00:00:00:03", 3: "02:00:00:00:00:04"})
    await asyncio.wait_for(esp.run(), 3)
    assert esp.last_reply["reason"] == "ningun canal autorizado"


async def test_sin_hello_se_cierra(stack):
    _, _, url = stack
    async with websockets.connect(url, compression=None) as ws:
        with pytest.raises(websockets.ConnectionClosed):
            await asyncio.wait_for(ws.recv(), 3)


async def test_ruta_invalida_da_404(stack):
    _, _, url = stack
    with pytest.raises(websockets.InvalidStatus):
        await websockets.connect(url.replace("/ws", "/otra"))


async def test_esp32_mudo_da_timeout_y_el_resultado_tardio_se_descarta(stack):
    hub, table, url = stack
    esp, task = await connect(url, silent={0})
    await asyncio.wait_for(esp.ready.wait(), 3)
    with pytest.raises(EmbeddedTimeout):
        await hub.execute(table.get(10), order(), 0.3)
    task.cancel()


async def test_falla_del_servo_llega_como_ok_false(stack):
    hub, table, url = stack
    esp, task = await connect(url, fail={0: "trabado"})
    await asyncio.wait_for(esp.ready.wait(), 3)
    res = await hub.execute(table.get(10), order(), 3)
    assert (res.ok, res.detail) == (False, "trabado")
    task.cancel()


async def test_caida_del_enlace_falla_lo_pendiente_y_actualiza_el_estado(stack):
    hub, table, url = stack
    changes = []
    hub.add_listener(lambda: changes.append(sorted(hub.online_device_ids())))
    esp, task = await connect(url, silent={0})
    await asyncio.wait_for(esp.ready.wait(), 3)
    pending = asyncio.create_task(hub.execute(table.get(10), order(), 5))
    await asyncio.sleep(0.1)
    task.cancel()
    await esp.ws.close()
    with pytest.raises(EmbeddedUnavailable):
        await asyncio.wait_for(pending, 3)
    await wait_until(lambda: hub.online_device_ids() == set())
    assert changes[0] == [10, 11, 12, 13] and changes[-1] == []


async def test_reconexion_reemplaza_la_sesion_anterior(stack):
    hub, table, url = stack
    esp1, t1 = await connect(url)
    await asyncio.wait_for(esp1.ready.wait(), 3)
    esp2, t2 = await connect(url)
    await asyncio.wait_for(esp2.ready.wait(), 3)
    await wait_until(lambda: len(hub.online_summary()) == 1)
    res = await hub.execute(table.get(10), order(), 3)
    assert res.ok and esp2.servo_writes == [(0, 90)] and esp1.servo_writes == []
    t1.cancel(), t2.cancel()


# ---- Bluetooth ----
async def bt_session(hub, serial):
    link = BtLink(serial, asyncio.get_running_loop(), "COM-test")
    task = asyncio.create_task(hub.serve_link(link))
    return link, task


async def test_bluetooth_flujo_completo():
    cfg = ServerCfg(token=TOKEN, hello_timeout_s=2)
    table = build_table()
    hub = EmbeddedHub(table, cfg)
    ser = FakeSerial()
    link, task = await bt_session(hub, ser)
    ser.peer_json(load_fixture("hello.json"))
    await wait_until(lambda: hub.online_device_ids() == {10, 11, 12, 13})
    assert ser.peer_messages()[0] == {"type": "ready", "channels": [0, 1, 2, 3]}
    fut = asyncio.create_task(hub.execute(table.get(12), order("b1", "open", 12), 3))
    await wait_until(lambda: ser.tx)
    cmd = ser.peer_messages()[0]
    assert cmd["channel"] == 2 and cmd["kind"] == "CURTAIN" and cmd["action"] == "open" and cmd["correlationId"] == "b1"
    ser.peer_json({"type": "result", "correlationId": "b1", "channel": 2, "ok": True, "detail": "", "state": "open", "angle": 180, "elapsedMs": 1200})
    res = await asyncio.wait_for(fut, 3)
    assert res.ok and res.angle == 180
    ser.close()
    await asyncio.wait_for(task, 3)
    assert hub.online_device_ids() == set()
    await link.close()


async def test_wifi_tiene_prioridad_sobre_bluetooth(stack):
    hub, table, url = stack
    esp, t = await connect(url)
    await asyncio.wait_for(esp.ready.wait(), 3)
    assert hub.wifi_alive("02:11:22:33:44:20")
    ser = FakeSerial()
    hello = load_fixture("hello.json")
    hello["mac"] = "02:11:22:33:44:20"
    link, task = await bt_session(hub, ser)
    ser.peer_json(hello)
    await asyncio.wait_for(task, 3)  # el edge lo rechaza porque ya esta por WiFi
    assert any(m.get("type") == "error" for m in ser.peer_messages())
    assert hub.online_summary()[0]["link"] == "ws"
    t.cancel()
    await link.close()


async def test_la_telemetria_llega_a_los_oyentes_con_los_dispositivos_del_embebido(stack):
    hub, table, url = stack
    seen = []
    hub.add_telemetry_listener(lambda esp_id, devices, telemetry: seen.append((devices, telemetry.get("batteryPct"))))
    hub.add_telemetry_listener(lambda *a: 1 / 0)  # un oyente que falla no afecta a los demas
    esp, task = await connect(url, telemetry={"batteryPct": 12, "rssi": -60})
    await asyncio.wait_for(esp.ready.wait(), 3)
    await wait_until(lambda: seen)
    assert seen == [([10, 11, 12, 13], 12)]
    task.cancel()
