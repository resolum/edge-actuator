"""Los fixtures de docs/fixtures son los mismos que usa el firmware: aqui se comprueba que el edge los entiende."""
import asyncio

from alivia_actuator_edge.dispatch.domain.models import CommandOrder
from alivia_actuator_edge.embedded.application.hub import EmbeddedHub
from alivia_actuator_edge.shared.config import ServerCfg
from tests.conftest import build_table, load_fixture
from tests.fakes import FakeSerial
from tests.test_hub import wait_until
from alivia_actuator_edge.embedded.interfaces.transports.bluetooth import BtLink


def test_hello_de_ejemplo_es_aceptado():
    hub = EmbeddedHub(build_table(), ServerCfg(token="secreto-de-prueba"))
    ok = hub._check_hello(load_fixture("hello.json"))
    assert not isinstance(ok, str), ok
    assert sorted(ok[3]) == [0, 1, 2, 3]


def test_hello_con_token_distinto_es_rechazado():
    hub = EmbeddedHub(build_table(), ServerCfg(token="otro"))
    assert hub._check_hello(load_fixture("hello.json")) == "token invalido"


async def test_el_comando_que_envia_el_edge_tiene_la_forma_del_fixture():
    cfg = ServerCfg(token="secreto-de-prueba", hello_timeout_s=2)
    table = build_table()
    hub = EmbeddedHub(table, cfg)
    ser = FakeSerial()
    link = BtLink(ser, asyncio.get_running_loop(), "t")
    task = asyncio.create_task(hub.serve_link(link))
    ser.peer_json(load_fixture("hello.json"))
    await wait_until(lambda: hub.online_device_ids())
    expected = load_fixture("command.json")
    fut = asyncio.create_task(hub.execute(table.get(10), CommandOrder(expected["correlationId"], "", 10, "", "", "", "open", {}, "", 0, 5000), 5.0))
    msgs = []

    def got_command():
        msgs.extend(m for m in ser.peer_messages() if m["type"] == "command")
        return msgs

    await wait_until(got_command)
    assert msgs[0] == expected  # el edge manda ttlMs = timeout de ejecucion (5.0 s -> 5000)
    ser.peer_json(load_fixture("result_ok.json"))
    res = await asyncio.wait_for(fut, 3)
    assert res.ok and res.state == "open" and res.angle == 90 and res.elapsed_ms == 700
    ser.close()
    await asyncio.wait_for(task, 3)
    await link.close()


def test_result_fail_fixture_es_ok_false_con_detalle():
    r = load_fixture("result_fail.json")
    assert r["ok"] is False and r["detail"] == "caducado" and r["angle"] == -1


def test_cases_json_trae_15_casos_con_configuracion_de_4_canales():
    d = load_fixture("cases.json")
    assert len(d["cases"]) == 15 and len(d["config"]) == 4
    assert {c["kind"] for c in d["config"]} == {"DOOR", "WINDOW", "CURTAIN", "LIGHT"}
