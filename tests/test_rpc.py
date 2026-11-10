import re
import socket

import pytest

from fake_t3 import FakeT3
from t3cc.errors import T3ccError
from t3cc.remote import rpc
from t3cc.remote.catalog import Environment
from t3cc.remote.rpc import Connection, RpcError


def env_for(server: FakeT3, token: str | None = "tok", ws_url: str | None = None) -> Environment:
    return Environment("env-1", "fake", "http://unused/", ws_url or server.ws_url, token)


@pytest.fixture
def server():
    with FakeT3() as fake:
        yield fake


@pytest.fixture
def conn(server):
    with Connection.open(env_for(server), timeout=5) as connection:
        yield connection


def test_call_returns_the_success_value(conn):
    assert conn.call("echo", {"a": 1}) == {"a": 1}


def test_call_raises_the_failure_message(conn):
    with pytest.raises(RpcError, match="^nope$"):
        conn.call("fail", {})
    with pytest.raises(RpcError, match="no method missing"):
        conn.call("missing", {})
    with pytest.raises(RpcError, match="failed: boom"):
        conn.call("defect", {})


def test_stream_acknowledges_past_the_window(conn):
    rid = conn.send("count", {"n": 20}, stream=True)
    assert list(conn.chunks(rid)) == list(range(20))


def test_chunk_on_a_plain_call_is_unexpected(conn):
    with pytest.raises(RpcError, match="unexpected Chunk"):
        conn.call("chunk-then-exit", {})


def test_answers_pings_and_ignores_noise(server, conn):
    assert conn.call("ping-first", {}) == "pong seen"
    assert conn.call("echo", {}) == {}
    assert server.pongs == 1


def test_requests_interleave(conn):
    first = conn.send("echo", {"n": 1})
    second = conn.send("echo", {"n": 2})
    assert conn.result(second) == {"n": 2}
    assert conn.result(first) == {"n": 1}


def test_timeout(conn):
    with pytest.raises(T3ccError, match="did not answer in time"):
        conn.call("silent", {}, timeout=0.2)


def test_server_hangs_up(conn):
    with pytest.raises(T3ccError, match="closed the connection"):
        conn.call("hang-up", {})


def test_open_refused(server):
    with pytest.raises(T3ccError, match=r"fake refused the connection \(HTTP 401\)"):
        Connection.open(env_for(server, token="wrong"))


def test_open_with_an_old_protocol(server, monkeypatch):
    monkeypatch.setattr(rpc, "PROTOCOL_QUERY", "orchestrationProtocol=1")
    with pytest.raises(T3ccError, match="HTTP 426"):
        Connection.open(env_for(server))


def test_open_needs_a_bearer_token(server):
    with pytest.raises(T3ccError, match="only bearer-token"):
        Connection.open(env_for(server, token=None))


def test_open_unreachable():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(T3ccError, match="fake is unreachable"):
        Connection.open(Environment("e", "fake", "h", f"ws://127.0.0.1:{port}/", "tok"), timeout=2)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ({"_tag": "Exit", "exit": {"_tag": "Failure", "cause": [{"_tag": "Die", "defect": "dead"}]}}, "dead"),
        ({"_tag": "Exit", "exit": {"_tag": "Failure", "cause": [{"_tag": "Fail", "error": {"_tag": "Tag"}}]}}, "Tag"),
        ({"_tag": "Exit", "exit": {"_tag": "Failure", "cause": []}}, "[]"),
        ({"_tag": "Exit", "exit": {"_tag": "Failure", "cause": [{"_tag": "Interrupt"}]}}, "Interrupt"),
    ],
)
def test_exit_value_describes_causes(message, expected):
    with pytest.raises(RpcError, match=re.escape(expected)):
        rpc.exit_value(message)
