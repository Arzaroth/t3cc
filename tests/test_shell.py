import hashlib
import os

import pytest

from fake_t3 import FakeT3
from t3cc.errors import T3ccError
from t3cc.remote import shell as shell_module
from t3cc.remote.catalog import Environment
from t3cc.remote.rpc import Connection
from t3cc.remote.shell import RemoteShell, Result


@pytest.fixture
def server():
    with FakeT3() as fake:
        yield fake


@pytest.fixture
def conn(server):
    with Connection.open(Environment("e", "fake", "h", server.ws_url, "tok"), timeout=10) as connection:
        yield connection


@pytest.fixture
def sh(conn, tmp_path):
    with RemoteShell(conn, cwd=str(tmp_path)) as remote:
        yield remote


def test_run_returns_output_and_code(sh, tmp_path):
    assert sh.run("pwd") == Result(f"{tmp_path}\n", 0)
    assert sh.run("echo out; echo err >&2; exit 3") == Result("out\nerr\n", 3)
    assert not sh.run("false").ok


def test_run_large_output_past_the_ack_window(sh):
    result = sh.run("seq 1 30000")
    assert result.ok and result.output.splitlines()[-1] == "30000"


def test_check(sh):
    assert sh.check("echo hi") == "hi\n"
    with pytest.raises(T3ccError, match=r"fake: `echo boom; exit 4` failed \(4\): boom"):
        sh.check("echo boom; exit 4")


def test_upload_and_hash(sh, tmp_path):
    data = os.urandom(150_000)
    seen = []
    sh.upload(data, str(tmp_path / "blob bin"), progress=lambda sent, total: seen.append((sent, total)))
    assert (tmp_path / "blob bin").read_bytes() == data
    assert sh.sha256(str(tmp_path / "blob bin")) == hashlib.sha256(data).hexdigest()
    assert seen[-1] == (len(data), len(data)) and len(seen) > 1
    sh.upload(b"x", str(tmp_path / "small"))
    assert (tmp_path / "small").read_bytes() == b"x"


def test_sha256_falls_back_to_shasum(sh, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "shasum").write_text('#!/bin/sh\necho "abc  $3"\n')
    (bin_dir / "sha256sum").write_text("#!/bin/sh\nexit 1\n")
    for tool in bin_dir.iterdir():
        tool.chmod(0o755)
    (tmp_path / "f").write_text("x")
    sh.write(f"PATH={bin_dir}:$PATH; export PATH\r")
    assert sh.sha256(str(tmp_path / "f")) == "abc"


def test_upload_reports_a_failed_write(sh, tmp_path):
    with pytest.raises(T3ccError, match="exit \\$t3cc_upload"):
        sh.upload(b"data", str(tmp_path / "missing" / "dir" / "f"))


def test_command_too_long(sh):
    with pytest.raises(T3ccError, match="too long"):
        sh.run("x" * 5000)


def test_timeout(sh):
    with pytest.raises(T3ccError, match="timed out: sleep 5"):
        sh.run("sleep 5", timeout=0.3)


def test_shell_exit(sh):
    sh.write("exit\r")
    with pytest.raises(T3ccError, match="the remote shell exited"):
        sh.run("true")
    with pytest.raises(T3ccError, match="the remote terminal closed"):
        sh._read(5)


def test_terminal_closed_under_us(conn, tmp_path):
    remote = RemoteShell(conn, cwd=str(tmp_path))
    remote.open()
    conn.call("terminal.close", remote._ids)
    with pytest.raises(T3ccError, match="no such terminal|exited|terminal closed"):
        remote.run("true")
    remote.close()


def test_open_in_a_missing_directory(conn, tmp_path):
    with pytest.raises(T3ccError, match="cwd does not exist"), RemoteShell(conn, cwd=str(tmp_path / "gone")):
        pass


def test_long_writes_are_split(sh, monkeypatch, tmp_path):
    monkeypatch.setattr(shell_module, "WRITE_LIMIT", 7)
    assert sh.run("echo split") == Result("split\n", 0)
