import json
import time

import pytest

from alivia_actuator_edge.devices.domain.model import Status
from alivia_actuator_edge.dispatch.application.ports import EmbeddedTimeout, EmbeddedUnavailable
from alivia_actuator_edge.dispatch.application.service import ActuatorService
from alivia_actuator_edge.dispatch.domain.models import EmbeddedResult
from tests.conftest import build_table, make_order


class FakeGateway:
    def __init__(self, result=None, exc=None):
        self.result = result or EmbeddedResult(True, "", "", 90, 700)
        self.exc = exc
        self.calls = []

    async def execute(self, device, order, timeout_s):
        self.calls.append((device.device_id, order.action, timeout_s))
        if self.exc:
            raise self.exc
        return self.result


class FakePublisher:
    def __init__(self):
        self.results, self.states = [], []

    async def publish_result(self, o):
        self.results.append(o)

    async def publish_state(self, o):
        self.states.append(o)


class FakeReporter:
    def __init__(self, boom=False):
        self.items, self.boom = [], boom

    async def report(self, o):
        self.items.append(o)
        if self.boom:
            raise RuntimeError("backend caido")


def make(table=None, gateway=None, reporter=None, dcfg=None, now=None):
    from alivia_actuator_edge.shared.config import DispatchCfg
    pub = FakePublisher()
    rep = reporter or FakeReporter()
    gw = gateway or FakeGateway()
    kwargs = {"now_ms": now} if now else {}
    svc = ActuatorService(table or build_table(), gw, pub, rep, dcfg or DispatchCfg(exec_timeout_s=2.0, dedup_max=3), **kwargs)
    return svc, gw, pub, rep


async def test_exito_publica_resultado_estado_y_reporta():
    svc, gw, pub, rep = make()
    await svc.handle("10", make_order())
    o = pub.results[0]
    assert (o.ok, o.state, o.device_id, o.action) == (True, "open", 10, "open")
    assert len(pub.states) == 1 and len(rep.items) == 1
    assert gw.calls[0][2] <= 2.0


async def test_fallo_del_embebido_se_propaga_y_no_publica_estado():
    svc, _, pub, rep = make(gateway=FakeGateway(EmbeddedResult(False, "trabado")))
    await svc.handle("10", make_order())
    assert (pub.results[0].ok, pub.results[0].detail) == (False, "trabado")
    assert pub.states == [] and len(rep.items) == 1


@pytest.mark.parametrize("exc,detail", [(EmbeddedUnavailable(), "embebido sin conexion"), (EmbeddedTimeout(), "sin respuesta del embebido")])
async def test_embebido_caido_o_mudo(exc, detail):
    svc, _, pub, _ = make(gateway=FakeGateway(exc=exc))
    await svc.handle("10", make_order())
    assert (pub.results[0].ok, pub.results[0].detail) == (False, detail)


@pytest.mark.parametrize("kw", [
    dict(mac="AA:BB:CC:00:00:99"),        # MAC que no es la de la tabla
    dict(kind="WINDOW"),                  # tipo que no coincide
    dict(device_id=99),                   # dispositivo desconocido
])
async def test_no_autorizado_no_llega_al_embebido(kw):
    svc, gw, pub, _ = make()
    await svc.handle(str(kw.get("device_id", 10)), make_order(**kw))
    assert pub.results[0].detail == "dispositivo no autorizado" and gw.calls == []


async def test_estado_distinto_de_in_use_se_niega():
    svc, gw, pub, _ = make(table=build_table(Status.MAINTENANCE))
    await svc.handle("10", make_order())
    assert pub.results[0].detail == "dispositivo no autorizado" and gw.calls == []


async def test_accion_invalida_para_el_tipo():
    svc, gw, pub, _ = make()
    await svc.handle("13", make_order(device_id=13, action="open", mac="AA:BB:CC:00:00:13", kind="LIGHT"))
    assert "accion invalida" in pub.results[0].detail and gw.calls == []


async def test_orden_caducada():
    svc, gw, pub, _ = make()
    await svc.handle("10", make_order(ts=int(time.time() * 1000) - 10_000, ttl=1000))
    assert pub.results[0].detail == "caducado" and gw.calls == []


async def test_duplicado_repite_resultado_sin_mover():
    svc, gw, pub, rep = make()
    await svc.handle("10", make_order())
    await svc.handle("10", make_order())
    assert len(gw.calls) == 1 and len(pub.results) == 2 and pub.results[1] is pub.results[0]
    assert len(rep.items) == 1  # el backend no recibe el mismo resultado dos veces


async def test_dedup_expira_y_se_acota():
    t = [1_000_000]
    svc, gw, pub, _ = make(now=lambda: t[0])
    await svc.handle("10", make_order(ts=t[0]))
    t[0] += 301_000  # supera dedup_ttl_s (300 s por defecto): el correlationId se olvida
    await svc.handle("10", make_order(ts=t[0]))
    assert len(gw.calls) == 2
    for i in range(5):
        await svc.handle("10", make_order(corr=f"x{i}", ts=t[0]))
    assert len(svc._done) <= 3


async def test_orden_invalida_con_correlacion_se_contesta_y_sin_ella_se_ignora():
    svc, _, pub, _ = make()
    await svc.handle("10", make_order(v=1))
    assert pub.results[0].ok is False and "orden invalida" in pub.results[0].detail
    await svc.handle("10", b"basura")
    assert len(pub.results) == 1


async def test_fallo_al_reportar_no_cambia_el_resultado():
    svc, _, pub, rep = make(reporter=FakeReporter(boom=True))
    await svc.handle("10", make_order())
    assert pub.results[0].ok is True
