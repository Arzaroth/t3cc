"""A T3 Code server stand-in: the RPC framing, auth and stream ack window of the real one."""

import codecs
import contextlib
import json
import os
import pty
import subprocess
import threading
from collections.abc import Callable
from http import HTTPStatus

from websockets.exceptions import ConnectionClosed
from websockets.sync.server import ServerConnection, serve

ACK_WINDOW = 8


class Call:
    def __init__(self, server: "FakeT3", ws: ServerConnection, rid: str, payload: dict):
        self.server = server
        self.ws = ws
        self.rid = rid
        self.payload = payload
        self.acks = threading.Semaphore(ACK_WINDOW)

    def send(self, message: dict) -> None:
        self.ws.send(json.dumps({**message, "requestId": self.rid}))

    def succeed(self, value=None) -> None:
        self.send({"_tag": "Exit", "exit": {"_tag": "Success", "value": value}})

    def fail(self, error: dict) -> None:
        self.send({"_tag": "Exit", "exit": {"_tag": "Failure", "cause": [{"_tag": "Fail", "error": error}]}})

    def chunk(self, *values) -> bool:
        if not self.acks.acquire(timeout=5):
            return False
        self.send({"_tag": "Chunk", "values": list(values)})
        return True


Handler = Callable[[Call], None]


def _run(handler: Handler, call: Call) -> None:
    with contextlib.suppress(ConnectionClosed, OSError):
        handler(call)


def _echo(call: Call) -> None:
    call.succeed(call.payload)


def _count(call: Call) -> None:
    for n in range(call.payload["n"]):
        if not call.chunk(n):
            call.fail({"_tag": "Stalled", "message": "client stopped acknowledging"})
            return
    call.succeed()


def _fail(call: Call) -> None:
    call.fail({"_tag": "NopeError", "message": "nope"})


def _chunk_then_exit(call: Call) -> None:
    call.send({"_tag": "Chunk", "values": [1]})
    call.succeed()


def _ping_first(call: Call) -> None:
    call.ws.send(json.dumps({"_tag": "Ping"}))
    call.ws.send(json.dumps({"_tag": "Noise"}))
    call.succeed("pong seen")


class Terminal:
    def __init__(self, cwd: str):
        self.master, slave = pty.openpty()
        self.proc = subprocess.Popen(
            ["sh"],
            cwd=cwd,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env={**os.environ, "PS1": "fake$ "},
            start_new_session=True,
        )
        os.close(slave)
        self.attached = threading.Event()

    def stream(self, call: Call) -> None:
        call.chunk({"type": "snapshot", "snapshot": {"history": ""}})
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        while True:
            try:
                data = os.read(self.master, 65536)
            except OSError:
                data = b""
            if not data:
                break
            call.chunk({"type": "output", "data": decoder.decode(data)})
        call.chunk({"type": "exited", "exitCode": self.proc.wait()})
        call.succeed()

    def close(self) -> None:
        self.proc.kill()
        self.proc.wait()


class FakeT3:
    def __init__(self, token: str = "tok"):
        self.token = token
        self.pongs = 0
        self.handlers: dict[str, Handler] = {
            "echo": _echo,
            "count": _count,
            "fail": _fail,
            "ping-first": _ping_first,
            "defect": lambda call: call.send({"_tag": "Defect", "defect": "boom"}),
            "chunk-then-exit": _chunk_then_exit,
            "silent": lambda call: None,
            "hang-up": lambda call: call.ws.close(),
            "terminal.open": self._terminal_open,
            "terminal.attach": self._terminal_attach,
            "terminal.write": self._terminal_write,
            "terminal.close": self._terminal_close,
        }
        self.terminals: dict[str, Terminal] = {}
        self._server = serve(self._handle, "127.0.0.1", 0, process_request=self._authorize)
        self.port = self._server.socket.getsockname()[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/"

    def __enter__(self) -> "FakeT3":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        for terminal in self.terminals.values():
            terminal.close()
        self._server.shutdown()
        self._thread.join(5)

    def _terminal(self, call: Call) -> Terminal | None:
        terminal = self.terminals.get(call.payload["terminalId"])
        if terminal is None:
            call.fail({"_tag": "TerminalNotRunningError", "message": "no such terminal"})
        return terminal

    def _terminal_open(self, call: Call) -> None:
        cwd = call.payload["cwd"]
        if not os.path.isdir(cwd):
            call.fail({"_tag": "TerminalCwdNotFoundError", "message": f"Terminal cwd does not exist: {cwd}"})
            return
        self.terminals[call.payload["terminalId"]] = Terminal(cwd)
        call.succeed({"terminalId": call.payload["terminalId"], "status": "running"})

    def _terminal_attach(self, call: Call) -> None:
        terminal = self._terminal(call)
        if terminal:
            terminal.stream(call)

    def _terminal_write(self, call: Call) -> None:
        terminal = self._terminal(call)
        if terminal:
            os.write(terminal.master, call.payload["data"].encode())
            call.succeed()

    def _terminal_close(self, call: Call) -> None:
        terminal = self._terminal(call)
        if terminal:
            del self.terminals[call.payload["terminalId"]]
            terminal.close()
            call.succeed()

    def _authorize(self, connection: ServerConnection, request):
        if "orchestrationProtocol=2" not in request.path:
            return connection.respond(HTTPStatus.UPGRADE_REQUIRED, "orchestration_protocol_incompatible\n")
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            return connection.respond(HTTPStatus.UNAUTHORIZED, "unauthorized\n")
        return None

    def _handle(self, ws: ServerConnection) -> None:
        calls: dict[str, Call] = {}
        with contextlib.suppress(ConnectionClosed):
            self._serve(ws, calls)

    def _serve(self, ws: ServerConnection, calls: dict[str, "Call"]) -> None:
        for raw in ws:
            message = json.loads(raw)
            tag = message.get("_tag")
            if tag == "Pong":
                self.pongs += 1
            elif tag == "Ack" and message["requestId"] in calls:
                calls[message["requestId"]].acks.release()
            elif tag == "Request":
                call = Call(self, ws, message["id"], message["payload"])
                calls[call.rid] = call
                handler = self.handlers.get(message["tag"])
                if handler is None:
                    call.fail({"_tag": "UnknownMethod", "message": f"no method {message['tag']}"})
                    continue
                threading.Thread(target=_run, args=(handler, call), daemon=True).start()
