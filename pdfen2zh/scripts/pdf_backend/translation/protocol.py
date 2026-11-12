"""Framed worker events, isolated from third-party stdout without retaining its text."""

import json
import math
import threading

VERSION = 1
MAX_LINE_BYTES = 256 * 1024
MAX_NOISE_BYTES = 1024 * 1024


class WorkerProtocolError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__("翻译工作进程通信异常，任务已停止；没有自动重试。请查看本次任务诊断。")


def event_prefix(token):
    if not isinstance(token, str) or len(token) != 32 or any(c not in "0123456789abcdef" for c in token):
        raise WorkerProtocolError("INVALID_CHANNEL")
    return f"READER_EVENT_V{VERSION}:{token} ".encode("ascii")


class EventWriter:
    def __init__(self, output, token):
        self.output = output
        self.prefix = event_prefix(token).decode("ascii")
        self.lock = threading.Lock()

    def emit(self, event):
        # A single write, including the newline, avoids split print/newline interleaving.
        message = self.prefix + json.dumps(event, ensure_ascii=True, allow_nan=False) + "\n"
        if len(message) > MAX_LINE_BYTES:
            raise WorkerProtocolError("EVENT_TOO_LARGE")
        with self.lock:
            self.output.write(message)
            self.output.flush()


class EventReader:
    def __init__(self, token):
        self.prefix = event_prefix(token)
        self.hello = self.finished = False
        self.noise_lines = self.noise_bytes = self.events = 0
        self.last_type = None

    def _noise(self, data):
        if not data:
            return
        self.noise_lines += 1
        self.noise_bytes += len(data)
        if self.noise_bytes > MAX_NOISE_BYTES:
            raise WorkerProtocolError("TOO_MUCH_UNFRAMED_OUTPUT")

    def read(self, line):
        if len(line) > MAX_LINE_BYTES:
            raise WorkerProtocolError("LINE_TOO_LARGE")
        start = line.find(self.prefix)
        if start < 0:
            self._noise(line)
            return None
        self._noise(line[:start])
        try:
            event = json.loads(line[start + len(self.prefix) :])
        except (ValueError, UnicodeError):
            raise WorkerProtocolError("INVALID_EVENT_JSON") from None
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise WorkerProtocolError("INVALID_EVENT_SHAPE")
        kind = event["type"]
        self.events += 1
        if kind == "hello":
            if self.hello or event.get("version") != VERSION or type(event.get("version")) is not int:
                raise WorkerProtocolError("INVALID_HANDSHAKE")
            self.hello = True
            self.last_type = kind
            return None
        if not self.hello or self.finished:
            raise WorkerProtocolError("INVALID_EVENT_ORDER")
        if kind == "progress":
            progress = event.get("progress")
            if (
                not isinstance(event.get("stage"), str)
                or type(progress) not in (int, float)
                or not math.isfinite(progress)
            ):
                raise WorkerProtocolError("INVALID_PROGRESS")
        elif kind == "heartbeat":
            if not isinstance(event.get("requests", {}), dict):
                raise WorkerProtocolError("INVALID_HEARTBEAT")
        elif kind == "usage":
            if not isinstance(event.get("entry"), dict):
                raise WorkerProtocolError("INVALID_USAGE_EVENT")
        elif kind == "finish":
            if not all(isinstance(event.get(k), str) and event[k] for k in ("mono", "dual")):
                raise WorkerProtocolError("INVALID_FINISH")
            self.finished = True
        elif kind == "error":
            if not isinstance(event.get("message"), str) or not isinstance(event.get("diagnostic", {}), dict):
                raise WorkerProtocolError("INVALID_ERROR_EVENT")
        else:
            raise WorkerProtocolError("UNKNOWN_EVENT_TYPE")
        self.last_type = kind
        return event

    def end(self):
        if not self.hello:
            raise WorkerProtocolError("MISSING_HANDSHAKE")
        if not self.finished:
            raise WorkerProtocolError("MISSING_FINISH")

    def diagnostic(self):
        return {
            "version": VERSION,
            "events": self.events,
            "lastEventType": self.last_type,
            "noiseLines": self.noise_lines,
            "noiseBytes": self.noise_bytes,
            "handshake": self.hello,
            "finished": self.finished,
        }
