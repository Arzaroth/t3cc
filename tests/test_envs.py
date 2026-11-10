import io
import json
import socket

import pytest

from fake_t3 import FakeT3
from t3cc import capabilities, cli
from t3cc.capabilities import Peer
from test_catalog import profile, write_catalog

CAPS = {"t3cc": "9.9.9", "protocol": 1, "features": ["import.live", "receive"]}


def run(world, *argv):
    out = io.StringIO()
    code = cli.main(list(argv), paths=world.paths, out=out)
    return code, out.getvalue()


def catalog_for(world, port, label="box"):
    data = {
        "profiles": [profile("env-1", label, port)],
        "credentials": [
            {"connectionId": "bearer:env-1", "credential": {"_tag": "BearerConnectionCredential", "token": "tok"}}
        ],
    }
    write_catalog(world, data, password=b"peanuts", prefix=b"v10")


def fake_t3cc(tmp_path, monkeypatch, script):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    if script is not None:
        tool = bin_dir / "t3cc"
        tool.write_text(f"#!/bin/sh\n{script}\n")
        tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")


@pytest.fixture
def server():
    with FakeT3() as fake:
        yield fake


def test_envs_reports_a_current_t3cc(world, server, tmp_path, monkeypatch):
    catalog_for(world, server.port)
    fake_t3cc(tmp_path, monkeypatch, f"echo '{json.dumps(CAPS)}'")
    code, out = run(world, "envs")
    assert code == 0
    assert out.split(None, 2) == ["box", f"http://127.0.0.1:{server.port}/", "t3cc 9.9.9, protocol 1\n"]


def test_envs_reports_an_old_t3cc(world, server, tmp_path, monkeypatch):
    catalog_for(world, server.port)
    fake_t3cc(tmp_path, monkeypatch, '[ "$1" = --version ] && echo "t3cc 0.3.0" || exit 2')
    out = run(world, "envs")[1]
    assert "t3cc 0.3.0, too old, upgrade it with: uv tool install --force git+https://github.com/Arzaroth/t3cc@v" in out


def test_envs_reports_a_missing_t3cc(world, server, tmp_path, monkeypatch):
    catalog_for(world, server.port)
    fake_t3cc(tmp_path, monkeypatch, None)
    assert "no t3cc, install it with: uv tool install" in run(world, "envs")[1]


def test_envs_reports_unreachable_environments(world):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    catalog_for(world, port)
    assert "unreachable: box is unreachable" in run(world, "envs")[1]
    assert run(world, "envs", "--no-probe")[1].rstrip().endswith("-")


def test_envs_without_environments(world):
    assert run(world, "envs") == (0, "no environments connected in T3 Code\n")


def test_peer_missing():
    peer = Peer("1.0", 1, frozenset({"a"}))
    assert peer.missing({"a", "c", "b"}) == ["b", "c"]
    assert capabilities.install_command("1.2.3").endswith("@v1.2.3")
