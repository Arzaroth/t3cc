"""A T3 Code server stand-in: the RPC framing, auth and stream ack window of the real one."""

import json
import threading
from collections.abc import Callable
from http import HTTPStatus

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
        }
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
        self._server.shutdown()
        self._thread.join(5)

    def _authorize(self, connection: ServerConnection, request):
        if "orchestrationProtocol=2" not in request.path:
            return connection.respond(HTTPStatus.UPGRADE_REQUIRED, "orchestration_protocol_incompatible\n")
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            return connection.respond(HTTPStatus.UNAUTHORIZED, "unauthorized\n")
        return None

    def _handle(self, ws: ServerConnection) -> None:
        calls: dict[str, Call] = {}
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
                threading.Thread(target=handler, args=(call,), daemon=True).start()
