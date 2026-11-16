"""ESP32 de actuadores falso: habla el protocolo real (docs/protocolo-embebido.md) por WebSocket y aplica las mismas
reglas que el firmware (dedup, mismo comando en curso, reemplazo, tiempos de asentamiento).

Uso:  python -m tools.fake_embebido --url ws://127.0.0.1:8766/ws --token secreto
      python -m tools.fake_embebido ... --fail 0=trabado --silent 3     (simula puerta trabada y luz sin respuesta)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from collections import OrderedDict
from pathlib import Path

import websockets

log = logging.getLogger("fake-esp32")
FIXTURES = Path(__file__).resolve().parent.parent / "docs" / "fixtures"
STATE = {"open": "open", "close": "closed", "on": "on", "off": "off"}
ACTIONS = {"DOOR": {"open", "close"}, "WINDOW": {"open", "close"}, "CURTAIN": {"open", "close"}, "LIGHT": {"on", "off"}}


def default_channels() -> list[dict]:
    cfg = json.loads((FIXTURES / "cases.json").read_text())
    return cfg["config"]


class FakeEsp32:
    def __init__(self, url: str, token: str, channels: list[dict] | None = None, base_mac: str = "02:11:22:33:44:20",
                 embedded_id: str = "alivia-act-01", time_scale: float = 1.0, fail: dict[int, str] | None = None,
                 silent: set[int] | None = None, channel_macs: dict[int, str] | None = None,
                 telemetry: dict | None = None) -> None:
        self.url = url
        self.token = token
        self.channels = {c["channel"]: dict(c) for c in (channels or default_channels())}
        if channel_macs:
            for n, mac in channel_macs.items():
                self.channels[n]["mac"] = mac
        self.base_mac = base_mac
        self.embedded_id = embedded_id
        self.scale = time_scale
        self.fail = fail or {}
        self.silent = silent or set()
        self.ws = None
        self.servo_writes: list[tuple[int, int]] = []
        self.commands_seen: list[dict] = []
        self._angle: dict[int, int] = {n: c["closedAngle"] for n, c in self.channels.items()}
        self._moving: dict[int, dict] = {}
        self._done: OrderedDict[str, dict] = OrderedDict()
        self.ready: asyncio.Event = asyncio.Event()
        self.last_reply: dict | None = None
        self.telemetry = telemetry  # si se da, se envia tras el ready (bateria, memoria, senal)

    def hello(self) -> dict:
        return {"type": "hello", "deviceId": self.embedded_id, "mac": self.base_mac, "capabilities": ["actuators"],
                "fw": "sim-1.0.0", "token": self.token,
                "channels": [{"channel": n, "mac": c["mac"], "kind": c["kind"]} for n, c in sorted(self.channels.items())]}

    async def run(self) -> None:
        async with websockets.connect(self.url, compression=None, subprotocols=None) as ws:
            self.ws = ws
            await ws.send(json.dumps(self.hello()))
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "ready":
                    self.last_reply = msg
                    self.ready.set()
                    if self.telemetry:
                        await self._send({"type": "telemetry", **self.telemetry})
                    log.info("listo, canales habilitados: %s", msg.get("channels"))
                elif msg.get("type") == "error":
                    self.last_reply = msg
                    log.error("el edge rechazo el hello: %s", msg.get("reason"))
                elif msg.get("type") == "command":
                    await self.on_command(msg)

    async def _send(self, obj: dict) -> None:
        if self.ws is not None:
            await self.ws.send(json.dumps(obj, separators=(",", ":")))

    @staticmethod
    def _result(corr, ch, ok, detail="", state="", angle=-1, elapsed=0) -> dict:
        return {"type": "result", "correlationId": corr, "channel": ch, "ok": ok, "detail": detail, "state": state,
                "angle": angle, "elapsedMs": elapsed}

    async def on_command(self, msg: dict) -> None:
        self.commands_seen.append(msg)
        corr = str(msg.get("correlationId", ""))
        ch = msg.get("channel")
        action = msg.get("action")
        if corr in self._done:
            await self._send(self._done[corr])
            return
        cfg = self.channels.get(ch) if isinstance(ch, int) else None
        if not action:
            return await self._finish(corr, self._result(corr, ch if isinstance(ch, int) else -1, False, "comando invalido"))
        if cfg is None:
            return await self._finish(corr, self._result(corr, ch if isinstance(ch, int) else -1, False, "canal inexistente"))
        if str(msg.get("mac", "")).upper() != cfg["mac"].upper():
            return await self._finish(corr, self._result(corr, ch, False, "mac no coincide"))
        if msg.get("kind") != cfg["kind"]:
            return await self._finish(corr, self._result(corr, ch, False, "tipo no coincide"))
        if action not in ACTIONS[cfg["kind"]]:
            return await self._finish(corr, self._result(corr, ch, False, "accion invalida"))
        t0 = time.monotonic()
        mv = self._moving.get(ch)
        if mv is not None and mv["action"] == action:
            mv["followers"].append((corr, t0))
            return
        if mv is not None:
            mv["task"].cancel()
            for c, _ in [(mv["leader"], 0)] + mv["followers"]:
                await self._finish(c, self._result(c, ch, False, "reemplazado"))
        target = cfg["openAngle"] if action in ("open", "on") else cfg["closedAngle"]
        settle = 150 if self._angle[ch] == target else cfg["settleMs"]
        self._angle[ch] = target
        self.servo_writes.append((ch, target))
        entry = {"action": action, "leader": corr, "followers": [], "t0": t0}
        entry["task"] = asyncio.create_task(self._settle(ch, entry, target, settle, cfg))
        self._moving[ch] = entry

    async def _settle(self, ch, entry, target, settle_ms, cfg) -> None:
        await asyncio.sleep(settle_ms / 1000.0 * self.scale)
        if self._moving.get(ch) is not entry:
            return
        del self._moving[ch]
        if ch in self.silent:
            return  # simula un ESP32 colgado: nunca contesta
        for corr, t0 in [(entry["leader"], entry["t0"])] + entry["followers"]:
            elapsed = int((time.monotonic() - t0) * 1000 / self.scale) if self.scale else 0
            if ch in self.fail:
                res = self._result(corr, ch, False, self.fail[ch])
            else:
                res = self._result(corr, ch, True, "", STATE[entry["action"]], target, min(elapsed, settle_ms + 5000))
            await self._finish(corr, res)

    async def _finish(self, corr: str, res: dict) -> None:
        self._done[corr] = res
        while len(self._done) > 8:
            self._done.popitem(last=False)
        await self._send(res)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="ws://127.0.0.1:8766/ws")
    p.add_argument("--token", required=True)
    p.add_argument("--base-mac", default="02:11:22:33:44:20")
    p.add_argument("--macs", default="02:11:22:33:44:21,02:11:22:33:44:22,02:11:22:33:44:23,02:11:22:33:44:24",
                   help="MAC logicas de los canales 0..3 (separadas por coma); deben estar en la tabla del edge")
    p.add_argument("--fail", action="append", default=[], metavar="CANAL=DETALLE", help="el canal responde ok:false")
    p.add_argument("--silent", action="append", type=int, default=[], metavar="CANAL", help="el canal nunca responde")
    p.add_argument("--battery-mv", type=int, default=None, help="envia telemetria con este voltaje de bateria")
    p.add_argument("--speed", type=float, default=1.0, help="1.0 tiempo real; 0.1 diez veces mas rapido")
    return p.parse_args()


async def _main() -> None:
    a = _parse_args()
    macs = dict(enumerate(a.macs.split(",")))
    fail = {int(k): v for k, v in (x.split("=", 1) for x in a.fail)}
    sim = FakeEsp32(a.url, a.token, base_mac=a.base_mac, time_scale=a.speed, fail=fail, silent=set(a.silent),
                    channel_macs=macs,
                    telemetry=({"uptimeMs": 1000, "freeHeap": 180000, "rssi": -55, "batteryMv": a.battery_mv} if a.battery_mv else None))
    delay = 1.0
    while True:  # reconecta como el firmware real (espera creciente 1 s a 10 s)
        try:
            await sim.run()
        except (OSError, websockets.WebSocketException) as exc:
            log.warning("sin enlace (%s); reintento en %.0fs", exc, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 10.0)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
