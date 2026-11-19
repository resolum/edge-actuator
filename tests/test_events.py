import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from alivia_actuator_edge.dispatch.domain.models import Outcome
from alivia_actuator_edge.events.domain import (
    DEVICE_FAILURE, LOW_BATTERY, BatteryWatch, EdgeEvent, FailureFilter,
)
from alivia_actuator_edge.events.reporter import BatteryEventReporter, EventReporter, FailureEventReporter
from alivia_actuator_edge.events.sender import HttpEventSender, RejectedError, RetryableError
from alivia_actuator_edge.shared.config import load_config


class FakeSender:
    def __init__(self, failures=()):
        self.failures = list(failures)
        self.sent = []

    async def send(self, event):
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append(event)
        return 1


async def wait_until(pred, timeout=3.0):
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    while not pred():
        if loop.time() - t0 > timeout:
            raise AssertionError("no se cumplio la condicion a tiempo")
        await asyncio.sleep(0.01)


def test_event_json_only_carries_what_it_has():
    e = EdgeEvent(LOW_BATTERY, device_id=10, battery_pct=15, event_id="e1")
    assert e.to_json() == {"eventId": "e1", "type": "LOW_BATTERY", "deviceId": 10, "batteryPct": 15}
    assert EdgeEvent("HELP_REQUESTED", detail="x" * 300, event_id="e2").to_json()["detail"] == "x" * 200
    assert EdgeEvent(LOW_BATTERY).event_id != EdgeEvent(LOW_BATTERY).event_id


def test_battery_warns_once_until_it_is_recharged():
    watch = BatteryWatch(20, 30)
    assert not watch.check("esp", 50)
    assert watch.check("esp", 19)
    assert not watch.check("esp", 15)
    assert not watch.check("esp", 25)  # entre los umbrales: sigue avisado
    assert not watch.check("esp", 35)  # recargado
    assert watch.check("esp", 18)
    assert watch.check("otro", 5)
    assert not watch.check("esp", "no es numero")
    with pytest.raises(ValueError):
        BatteryWatch(30, 20)


def test_failures_skip_rejected_orders_and_repeat_only_after_the_cooldown():
    f = FailureFilter(600)
    assert not f.should_report(10, "caducado", 0)
    assert not f.should_report(10, "dispositivo no autorizado", 0)
    assert not f.should_report(10, "accion invalida para DOOR", 0)
    assert f.should_report(10, "sin respuesta del embebido", 0)
    assert not f.should_report(10, "trabado", 100)
    assert f.should_report(11, "trabado", 100)
    assert f.should_report(10, "trabado", 700)


async def test_reporter_retries_until_the_backend_answers():
    sender = FakeSender([RetryableError("red"), RetryableError("HTTP 503", retry_after=0.01)])
    events = EventReporter(sender, first_retry_s=0.01)
    events.start()
    events.submit(EdgeEvent(LOW_BATTERY, event_id="a"))
    events.submit(EdgeEvent(DEVICE_FAILURE, event_id="b"))
    await wait_until(lambda: len(sender.sent) == 2)
    assert [e.event_id for e in sender.sent] == ["a", "b"]
    assert events.pending == 0 and events.delivered == 2
    await events.stop()


async def test_reporter_drops_rejected_events_and_keeps_going():
    sender = FakeSender([RejectedError("HTTP 404"), ValueError("raro")])
    events = EventReporter(sender, first_retry_s=0.01)
    events.start()
    for i in "abc":
        events.submit(EdgeEvent(LOW_BATTERY, event_id=i))
    await wait_until(lambda: events.pending == 0)
    assert [e.event_id for e in sender.sent] == ["c"]
    await events.stop()
    await events.stop()


def test_queue_keeps_the_newest_events():
    events = EventReporter(FakeSender(), queue_max=2)
    for i in "abc":
        events.submit(EdgeEvent(LOW_BATTERY, event_id=i))
    assert events.pending == 2


async def test_failed_orders_become_failure_events():
    events = EventReporter(FakeSender(), queue_max=10)
    reporter = FailureEventReporter(events, FailureFilter(600), now=lambda: 0)
    await reporter.report(Outcome("c1", 10, "open", True))
    await reporter.report(Outcome("c2", 10, "close", False, "sin respuesta del embebido"))
    await reporter.report(Outcome("c3", 10, "close", False, "sin respuesta del embebido"))
    await reporter.report(Outcome("c4", 11, "open", False, "caducado"))
    pending = list(events._pending)
    assert len(pending) == 1
    assert pending[0].type == DEVICE_FAILURE and pending[0].device_id == 10
    assert pending[0].detail == "no se pudo cerrar (sin respuesta del embebido)"


def test_low_battery_telemetry_becomes_one_event():
    events = EventReporter(FakeSender(), queue_max=10)
    battery = BatteryEventReporter(events, BatteryWatch(20, 30))
    battery.on_telemetry("esp", [13, 10], {"rssi": -50})
    battery.on_telemetry("esp", [13, 10], {"batteryPct": 12.7})
    battery.on_telemetry("esp", [13, 10], {"batteryPct": 11})
    battery.on_telemetry("solo", [], {"batteryPct": 3})
    pending = list(events._pending)
    assert [(e.type, e.device_id, e.battery_pct) for e in pending] == [(LOW_BATTERY, 10, 12), (LOW_BATTERY, None, 3)]


class _Backend(BaseHTTPRequestHandler):
    replies: list = []
    received: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).received.append((self.path, self.headers["Authorization"], body))
        code, payload, headers = type(self).replies.pop(0)
        self.send_response(code)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def backend():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Backend)
    _Backend.received = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


async def test_sender_posts_with_the_edge_credential(backend):
    _Backend.replies = [(202, b'{"notified": 2}', {}), (503, b"", {"Retry-After": "7"}), (404, b"", {}),
                        (500, b"", {}), (202, b"no json", {})]
    sender = HttpEventSender(f"http://127.0.0.1:{backend.server_port}/", "3.secreto")
    event = EdgeEvent(DEVICE_FAILURE, device_id=10, detail="trabado", event_id="f1")

    assert await sender.send(event) == 2
    assert _Backend.received[0] == ("/api/v1/edge/events", "Bearer 3.secreto",
                                    {"eventId": "f1", "type": "DEVICE_FAILURE", "deviceId": 10, "detail": "trabado"})
    with pytest.raises(RetryableError) as busy:
        await sender.send(event)
    assert busy.value.retry_after == 7
    with pytest.raises(RejectedError):
        await sender.send(event)
    with pytest.raises(RetryableError):
        await sender.send(event)
    assert await sender.send(event) == 0


async def test_sender_without_network_is_retryable():
    sender = HttpEventSender("http://127.0.0.1:1", "3.s", timeout_s=1)
    with pytest.raises(RetryableError):
        await sender.send(EdgeEvent(LOW_BATTERY))
    with pytest.raises(ValueError):
        HttpEventSender("http://x", "")


def test_config_reads_the_backend_section(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[server]\ntoken = "t"\n[backend]\nenabled = true\nlow_battery_pct = 15\n')
    monkeypatch.setenv("ALIVIA_EDGE_CREDENTIAL", "3.s")
    cfg = load_config(path)
    assert cfg.backend.enabled and cfg.backend.credential == "3.s" and cfg.backend.low_battery_pct == 15
    monkeypatch.delenv("ALIVIA_EDGE_CREDENTIAL")
    with pytest.raises(ValueError):
        load_config(path)
