import hashlib
import os

import pytest

from fake_t3 import FakeT3
from t3cc.errors import T3ccError
from t3cc.remote.catalog import Environment
from t3cc.remote.rpc import Connection
from t3cc.remote.shell import RemoteShell
from t3cc.send import transport

SEND = """#!/bin/sh
[ -n "$FAIL_SEND_CODE" ] && { echo "no code for you"; exit 4; }
for last; do :; done
cp "$last" "$SPOOL/7-fake-code"
echo "Wormhole code is: 7-fake-code"
[ -n "$SEND_WAITS" ] && sleep 30
exit ${SEND_EXIT:-0}
"""
RECEIVE = """#!/bin/sh
[ -e "$SPOOL/FAIL_RECEIVE" ] && { echo "receive broke"; exit 2; }
while [ $# -gt 1 ]; do [ "$1" = -o ] && dest=$2; shift; done
if [ -e "$SPOOL/CORRUPT" ]; then echo corrupt > "$dest"; else cp "$SPOOL/$1" "$dest"; fi
"""


@pytest.fixture
def tools(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    spool = tmp_path / "spool"
    spool.mkdir()
    for name in ("wyrm", "wormhole"):
        script = bin_dir / name
        script.write_text(
            '#!/bin/sh\nmode=$1; shift\n[ "$mode" = send ] && exec "$0-send" "$@"\nexec "$0-receive" "$@"\n'
        )
        (bin_dir / f"{name}-send").write_text(SEND)
        (bin_dir / f"{name}-receive").write_text(RECEIVE)
    for path in bin_dir.iterdir():
        path.chmod(0o755)
    monkeypatch.setenv("SPOOL", str(spool))
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return bin_dir


@pytest.fixture
def shell(tmp_path, tools):
    with (
        FakeT3() as server,
        Connection.open(Environment("e", "fake", "h", server.ws_url, "tok"), timeout=10) as conn,
        RemoteShell(conn, cwd=str(tmp_path)) as remote,
    ):
        yield remote


@pytest.fixture
def archive_file(tmp_path):
    path = tmp_path / "session.tar.xz"
    path.write_bytes(os.urandom(20_000))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def send(shell, archive_file, tmp_path, **kwargs):
    path, digest = archive_file
    dest = tmp_path / "out" / "a.tar.xz"
    dest.parent.mkdir(exist_ok=True)
    result = transport.send(path, str(dest), shell, sha256=digest, **kwargs)
    return result, dest


def test_prefers_wyrm(shell, archive_file, tmp_path):
    result, dest = send(shell, archive_file, tmp_path)
    assert result.used == "wyrm" and result.warnings == []
    assert dest.read_bytes() == archive_file[0].read_bytes()


def test_without_tools_uses_the_channel(shell, archive_file, tmp_path):
    seen = []
    result, dest = send(shell, archive_file, tmp_path, which=lambda name: None, progress=lambda *a: seen.append(a))
    assert result.used == "channel" and seen and dest.read_bytes() == archive_file[0].read_bytes()


def test_falls_back_when_a_peer_to_peer_transfer_fails(shell, archive_file, tmp_path):
    (tmp_path / "spool" / "FAIL_RECEIVE").touch()
    result, dest = send(shell, archive_file, tmp_path)
    assert result.used == "channel"
    assert [w.split(":")[0] for w in result.warnings] == ["wyrm", "wormhole"]
    assert "receive broke" in result.warnings[0]


def test_falls_back_on_a_corrupt_delivery(shell, archive_file, tmp_path):
    (tmp_path / "spool" / "CORRUPT").touch()
    result, _ = send(shell, archive_file, tmp_path)
    assert result.used == "channel" and "does not match" in result.warnings[0]


@pytest.mark.parametrize(("variable", "message"), [("FAIL_SEND_CODE", "printed no code"), ("SEND_EXIT", "failed")])
def test_forced_transport_does_not_fall_back(shell, archive_file, tmp_path, monkeypatch, variable, message):
    monkeypatch.setenv(variable, "1")
    with pytest.raises(T3ccError, match=f"wormhole send {message}"):
        send(shell, archive_file, tmp_path, requested="wormhole")


def test_forced_transport_must_exist(shell, archive_file, tmp_path):
    with pytest.raises(T3ccError, match=r"transport wyrm is not available on both sides \(available: channel\)"):
        send(shell, archive_file, tmp_path, requested="wyrm", which=lambda name: None)


def test_every_transport_failing(shell, archive_file, tmp_path):
    path, digest = archive_file
    with pytest.raises(T3ccError, match="channel: "):
        transport.send(path, str(tmp_path / "missing" / "a"), shell, sha256=digest, which=lambda name: None)


def test_a_failed_receive_stops_the_local_sender(shell, archive_file, tmp_path, monkeypatch):
    (tmp_path / "spool" / "FAIL_RECEIVE").touch()
    monkeypatch.setenv("SEND_WAITS", "1")
    with pytest.raises(T3ccError, match="receive broke"):
        send(shell, archive_file, tmp_path, requested="wyrm")
