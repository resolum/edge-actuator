from __future__ import annotations

import json
import threading

from alivia_actuator_edge.embedded.interfaces.transports.frames import TYPE_TEXT, FrameDecoder, encode


class FakeSerial:
    """pyserial en memoria: el 'ESP32' escribe con peer_json() y lee lo que el edge escribio con peer_messages()."""

    def __init__(self):
        self._rx = bytearray()
        self._cv = threading.Condition()
        self.tx = bytearray()
        self.closed = False
        self._dec = FrameDecoder()

    @property
    def in_waiting(self):
        with self._cv:
            return len(self._rx)

    def read(self, n=1):
        with self._cv:
            if not self._rx and not self.closed:
                self._cv.wait(0.05)
            if self.closed:
                raise OSError("puerto cerrado")
            data = bytes(self._rx[:n])
            del self._rx[:n]
            return data

    def write(self, data):
        with self._cv:
            self.tx += data
        return len(data)

    def close(self):
        with self._cv:
            self.closed = True
            self._cv.notify_all()

    def peer_json(self, obj):
        with self._cv:
            self._rx += encode(TYPE_TEXT, json.dumps(obj).encode())
            self._cv.notify_all()

    def peer_messages(self) -> list[dict]:
        with self._cv:
            data, self.tx = bytes(self.tx), bytearray()
        out = []
        for t, payload in self._dec.feed(data):
            if t == TYPE_TEXT:
                out.append(json.loads(payload))
        return out
