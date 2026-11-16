import asyncio
import json
import time
import urllib.error
import urllib.request

import pytest

from alivia_actuator_edge.dispatch.domain.models import Outcome
from alivia_actuator_edge.embedded.application.hub import EmbeddedHub
from alivia_actuator_edge.embedded.interfaces.transports.websocket import serve_ws
from alivia_actuator_edge.http.app import HealthContext, create_app
from alivia_actuator_edge.shared.config import HttpCfg, ServerCfg
from alivia_actuator_edge.shared.diagnostics import CompositeReporter, Diagnostics
from tests.conftest import build_table, free_port
from tests.test_hub import TOKEN, wait_until
from tools.fake_embebido import FakeEsp32


def outcome(dev=10, ok=True, detail="", corr="c"):
    return Outcome(corr, dev, "open", ok, detail, "open" if ok else "", 90 if ok else -1, 700)


def client(snapshot=(), mqtt=True, token="", diag=None):
    diag = diag or Diagnostics(5)
    ctx = HealthContext(build_table(), diag, lambda: snapshot, lambda: mqtt, HttpCfg(token=token))
    return create_app(ctx).test_client(), diag


def esp(telemetry=None):
    return ({"deviceId": "alivia-act-01", "mac": "02:11:22:33:44:20", "link": "ws", "fw": "1", "connectedAt": time.time() - 5,
             "channels": {0: {"deviceId": 10, "mac": "AA:BB:CC:00:00:10", "kind": "DOOR"}},
             "telemetry": telemetry or {}, "telemetryAt": time.time() if telemetry else None, "pending": 0},)


async def test_diagnostics_cuenta_y_recuerda_errores():
    d = Diagnostics(3)
    for i in range(5):
        await d.report(outcome(ok=False, detail="trabado", corr=f"e{i}"))
    await d.report(outcome(11, True))
    s = d.summary()
    assert (s["orders"], s["ok"], s["failed"], s["failuresByDetail"]) == (6, 1, 5, {"trabado": 5})
    assert len(d.errors()) == 3 and d.errors()[0]["correlationId"] == "e4"  # los mas recientes primero, acotado
    assert d.errors(device_id=11) == []
    assert d.device_counts(10) == {"total": 5, "ok": 0, "fail": 5}


async def test_composite_sigue_aunque_un_reportero_falle():
    d = Diagnostics()

    class Roto:
        async def report(self, o):
            raise RuntimeError("x")

    await CompositeReporter(Roto(), d).report(outcome())
    assert d.summary()["orders"] == 1


def test_health_degradado_y_ok():
    c, _ = client(snapshot=())
    r = c.get("/health")
    assert r.status_code == 200 and r.json["status"] == "degraded" and "ningun ESP32" in r.json["problems"][0]
    c, _ = client(snapshot=esp())
    assert c.get("/health").json["status"] == "ok"
    c, _ = client(snapshot=esp(), mqtt=False)
    r = c.get("/health")
    assert r.status_code == 503 and r.json["mqtt"] is False


async def test_devices_incluye_en_linea_ultimo_resultado_y_contadores():
    c, d = client(snapshot=esp())
    await d.report(outcome(10, False, "trabado"))
    devs = {x["deviceId"]: x for x in c.get("/devices").json["devices"]}
    assert devs[10]["online"] is True and devs[10]["via"]["channel"] == 0
    assert devs[10]["lastResult"]["detail"] == "trabado" and devs[10]["counts"]["fail"] == 1
    assert devs[11]["online"] is False and devs[11]["lastResult"] is None


def test_embedded_muestra_bateria_solo_si_el_esp32_la_reporta():
    c, _ = client(snapshot=esp({"batteryMv": 3900, "freeHeap": 1, "rssi": -50}))
    e = c.get("/embedded").json["embedded"][0]
    assert e["battery"] == {"mv": 3900, "pct": None} and e["telemetry"]["rssi"] == -50 and e["telemetryAgeS"] == 0
    c, _ = client(snapshot=esp())
    assert c.get("/embedded").json["embedded"][0]["battery"] is None


async def test_errors_filtra_y_valida():
    c, d = client()
    await d.report(outcome(10, False, "a"))
    await d.report(outcome(11, False, "b"))
    assert [e["detail"] for e in c.get("/errors").json["errors"]] == ["b", "a"]
    assert [e["detail"] for e in c.get("/errors?deviceId=10").json["errors"]] == ["a"]
    assert c.get("/errors?limit=abc").status_code == 400


def test_token_protege_todo_menos_health():
    c, _ = client(token="s3")
    assert c.get("/health").status_code == 200
    assert c.get("/devices").status_code == 401
    assert c.get("/devices", headers={"Authorization": "Bearer mal"}).status_code == 401
    assert c.get("/devices", headers={"Authorization": "Bearer s3"}).status_code == 200


def test_la_api_es_de_solo_lectura():
    c, _ = client()
    assert c.post("/devices").status_code == 405 and c.delete("/errors").status_code == 405


async def test_telemetria_del_esp32_llega_al_snapshot_del_hub():
    port = free_port()
    cfg = ServerCfg(host="127.0.0.1", port=port, token=TOKEN, hello_timeout_s=1, ping_interval_s=5, ping_timeout_s=5)
    hub = EmbeddedHub(build_table(), cfg)
    server = await serve_ws(cfg, hub.serve_link)
    e = FakeEsp32(f"ws://127.0.0.1:{port}/ws", TOKEN, telemetry={"uptimeMs": 5, "freeHeap": 100, "batteryMv": 3700, "malo": "x" * 1, "otro": {"a": 1}})
    t = asyncio.create_task(e.run())
    await wait_until(lambda: hub.snapshot() and hub.snapshot()[0]["telemetry"])
    tel = hub.snapshot()[0]["telemetry"]
    assert tel["batteryMv"] == 3700 and "otro" not in tel and "malo" not in tel  # solo claves conocidas
    t.cancel()
    server.close()
    await hub.close_all()
    await server.wait_closed()
