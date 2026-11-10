import itertools
import json
import time
from collections import defaultdict, deque
from collections.abc import Iterator
from contextlib import ExitStack
from typing import Any

from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException
from websockets.sync.client import ClientConnection, connect

from t3cc.errors import T3ccError
from t3cc.remote.catalog import Environment

PROTOCOL_QUERY: str = "orchestrationProtocol=2"


class RpcError(T3ccError):
    """A request the environment answered with a failure."""


def _cause_message(cause: Any) -> str:
    first = cause[0] if isinstance(cause, list) and cause else cause
    if isinstance(first, dict):
        error = first.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("_tag"))
        if "defect" in first:
            return str(first["defect"])
    return json.dumps(cause)


def exit_value(message: dict) -> Any:
    if message.get("_tag") == "Defect":
        raise RpcError(f"the environment failed: {message.get('defect')}")
    if message.get("_tag") != "Exit":
        raise RpcError(f"unexpected {message.get('_tag')} from the environment")
    outcome = message["exit"]
    if outcome.get("_tag") == "Success":
        return outcome.get("value")
    raise RpcError(_cause_message(outcome.get("cause")))


class Connection:
    def __init__(self, ws: ClientConnection, label: str, timeout: float, stack: ExitStack | None = None):
        self._ws = ws
        self._stack = stack or ExitStack()
        self.label = label
        self.timeout = timeout
        self._ids = itertools.count(1)
        self._inbox: defaultdict[str, deque[dict]] = defaultdict(deque)
        self._streams: set[str] = set()

    @classmethod
    def open(cls, env: Environment, timeout: float = 30.0) -> "Connection":
        if not env.token:
            raise T3ccError(f"{env.label}: only bearer-token connections are supported")
        url = f"{env.ws_url.rstrip('/')}/ws?{PROTOCOL_QUERY}"
        stack = ExitStack()
        try:
            ws = stack.enter_context(
                connect(
                    url,
                    additional_headers={"Authorization": f"Bearer {env.token}"},
                    max_size=None,
                    open_timeout=timeout,
                    ping_timeout=120,
                )
            )
        except InvalidStatus as error:
            raise T3ccError(f"{env.label} refused the connection (HTTP {error.response.status_code})") from None
        except (OSError, TimeoutError, WebSocketException) as error:
            raise T3ccError(f"{env.label} is unreachable: {error}") from None
        return cls(ws, env.label, timeout, stack)

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._stack.close()

    def _send(self, message: dict) -> None:
        self._ws.send(json.dumps(message))

    def send(self, tag: str, payload: dict, *, stream: bool = False) -> str:
        rid = str(next(self._ids))
        if stream:
            self._streams.add(rid)
        self._send({"_tag": "Request", "id": rid, "tag": tag, "payload": payload, "headers": []})
        return rid

    def _pump(self, timeout: float) -> None:
        try:
            message = json.loads(self._ws.recv(timeout=timeout))
        except TimeoutError:
            raise T3ccError(f"{self.label} did not answer in time") from None
        except ConnectionClosed as error:
            raise T3ccError(f"{self.label} closed the connection: {error}") from None
        tag = message.get("_tag")
        if tag == "Ping":
            self._send({"_tag": "Pong"})
            return
        rid = message.get("requestId")
        if rid is None:
            return
        if tag == "Chunk" and rid in self._streams:
            self._send({"_tag": "Ack", "requestId": rid})
        self._inbox[rid].append(message)

    def next_message(self, rid: str, timeout: float | None = None) -> dict:
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while not self._inbox[rid]:
            self._pump(max(deadline - time.monotonic(), 0.001))
        message = self._inbox[rid].popleft()
        if message.get("_tag") != "Chunk":
            self._streams.discard(rid)
        return message

    def result(self, rid: str, timeout: float | None = None) -> Any:
        return exit_value(self.next_message(rid, timeout))

    def call(self, tag: str, payload: dict, timeout: float | None = None) -> Any:
        return self.result(self.send(tag, payload), timeout)

    def chunks(self, rid: str, timeout: float | None = None) -> Iterator[Any]:
        while True:
            message = self.next_message(rid, timeout)
            if message.get("_tag") != "Chunk":
                exit_value(message)
                return
            yield from message.get("values", [])
