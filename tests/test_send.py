import io
import sys
from pathlib import Path

import pytest

from conftest import World, assistant, user
from fake_t3 import FakeT3
from gitrepo import commit, git, isolated_git, origin_and_clone  # noqa: F401
from t3cc import cli
from t3cc.errors import T3ccError
from t3cc.send import sender
from t3cc.send.sender import SendOptions
from t3cc.t3.repo import T3Repository
from test_catalog import profile, write_catalog
from test_receive import WT

pytestmark = pytest.mark.usefixtures("isolated_git")

SID = "44444444-4444-4444-8444-444444444444"

T3CC = """#!/bin/sh
if [ -e "{fake}/old" ] && [ "$1" = capabilities ]; then exit 2; fi
if [ -e "{fake}/old" ] && [ "$1" = --version ]; then echo "t3cc 0.3.0"; exit 0; fi
exec env T3CODE_HOME="{home}" CLAUDE_CONFIG_DIR="{claude}" "{python}" -m t3cc "$@"
"""


class Env:
    def __init__(self, local: World, tmp: Path, monkeypatch):
        self.local = local
        self.tmp = tmp
        self.fake = tmp / "fake"
        self.fake.mkdir()
        self.remote_world = World(tmp / "remote-world")
        self.origin, self.clone = origin_and_clone(tmp / "local")
        self.remote = tmp / "remote" / "app"
        git(tmp, "clone", "-q", str(self.origin), str(self.remote))
        self.project = self.remote_world.project(str(self.remote))
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "t3cc").write_text(
            T3CC.format(
                fake=self.fake,
                home=self.remote_world.paths.t3_home,
                claude=self.remote_world.paths.claude_projects.parent,
                python=sys.executable,
            )
        )
        (bin_dir / "wt").write_text(WT)
        for tool in bin_dir.iterdir():
            tool.chmod(0o755)
        monkeypatch.setenv("HOME", str(tmp / "home"))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp / "xdg"))
        monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")

    def session(self, cwd: Path, records=None) -> Path:
        return self.local.transcript(cwd, records or [user("hello"), assistant("hi")], SID)

    def remote_ids(self):
        return T3Repository(self.remote_world.db()).imported_session_ids()


@pytest.fixture
def env(world, tmp_path, monkeypatch):
    environment = Env(world, tmp_path, monkeypatch)
    yield environment
    environment.remote_world.close()


@pytest.fixture
def server(env):
    with FakeT3() as fake:
        data = {
            "profiles": [profile("env-1", "box", fake.port)],
            "credentials": [
                {"connectionId": "bearer:env-1", "credential": {"_tag": "BearerConnectionCredential", "token": "tok"}}
            ],
        }
        write_catalog(env.local, data, password=b"peanuts", prefix=b"v10")
        yield fake


def send(env, **options):
    return sender.send(SID, SendOptions(env="box", **options), env.local.paths, which=lambda name: None)


def test_send_moves_the_session(env, server):
    source = env.session(env.clone)
    result = send(env)
    assert result.transport == "channel" and result.removed and result.archived is None
    assert result.received["cwd"] == str(env.remote)
    assert result.received["thread_id"] == f"import:claudeAgent:{SID}"
    assert env.remote_ids() == {SID}
    assert not source.exists()
    assert not list((env.tmp / "home" / ".cache" / "t3cc" / "incoming").iterdir())
    assert result.notes == []


def test_send_copy_keeps_the_session(env, server):
    source = env.session(env.clone)
    result = send(env, copy=True)
    assert not result.removed and source.exists() and env.remote_ids() == {SID}


def test_send_can_archive_what_it_moved(env, server):
    source = env.session(env.clone)
    result = send(env, archive_to=env.tmp / "moved")
    assert result.archived == env.tmp / "moved" / f"{SID}.tar.xz" and result.archived.stat().st_size > 0
    assert not source.exists()


def test_send_refuses_unpushed_work_without_dirty(env, server):
    commit(env.clone, "local.txt")
    (env.clone / "README").write_text("edited")
    env.session(env.clone)
    with pytest.raises(T3ccError, match="has 1 unpushed commits and uncommitted changes: push them, or send with"):
        send(env)
    git(env.clone, "checkout", "-q", "--", "README")
    with pytest.raises(T3ccError, match=r"has 1 unpushed commits: push"):
        send(env)


def test_send_dirty_worktree(env, server):
    worktree = env.clone.parent / "app.worktrees" / "feature" / "x"
    git(env.clone, "worktree", "add", "-q", "-b", "feature/x", str(worktree))
    commit(worktree, "feature.txt", "work")
    (worktree / "scratch.txt").write_text("untracked")
    env.session(worktree)
    seen = env.tmp / "seen"
    commands = {
        "post-import": "exit 5",
        "pre-send": f'echo "pre $T3CC_SESSION $T3CC_ENV $T3CC_BRANCH [$T3CC_REMOTE_DIR]" >> {seen}',
        "post-checkout": f'echo "checkout $T3CC_ENV $T3CC_BRANCH $T3CC_REMOTE_DIR" >> {seen}',
        "post-send": f'echo "post $T3CC_REMOTE_DIR" >> {seen}; exit 6',
    }
    result = send(env, dirty=True, hooks=commands)
    target = env.remote.parent / "app.worktrees" / "feature" / "x"
    assert result.received["cwd"] == str(target)
    assert (target / "scratch.txt").read_text() == "untracked"
    assert result.notes[0].startswith("post-import hook `exit 5` failed (5)")
    assert result.notes[1].startswith("post-send hook `echo") and result.notes[1].endswith("failed (6): ")
    assert result.notes[-1] == f"{worktree} is still checked out here: `wt remove` it when you are done"
    assert seen.read_text().splitlines() == [
        f"pre {SID} box feature/x []",
        f"checkout box feature/x {target}",
        f"post {target}",
    ]


def test_send_with_a_map(env, server):
    plain = env.tmp / "local" / "plain" / "app"
    plain.mkdir(parents=True)
    env.session(plain)
    result = send(env, map=f"{plain.parent}={env.remote.parent}")
    assert result.received["cwd"] == str(env.remote)


@pytest.mark.parametrize(("mapping", "message"), [("nope", "LOCAL=REMOTE"), ("/elsewhere=/x", "does not contain")])
def test_send_rejects_bad_maps(env, mapping, message):
    env.session(env.clone)
    with pytest.raises(T3ccError, match=message):
        send(env, map=mapping)


def test_send_reports_what_receive_refused(env, server):
    source = env.session(env.clone)
    with env.remote_world.db() as con:
        con.execute("UPDATE projection_projects SET deleted_at = 'x'")
    with pytest.raises(T3ccError, match="box: t3cc receive failed: t3cc: no T3 project here for"):
        send(env)
    assert source.exists()
    assert not list((env.tmp / "home" / ".cache" / "t3cc" / "incoming").iterdir())


def test_send_needs_a_recent_t3cc_there(env, server):
    env.session(env.clone)
    (env.fake / "old").touch()
    with pytest.raises(T3ccError, match="box: t3cc 0.3.0, too old"):
        send(env)


def test_send_refuses_an_open_session(env, tmp_path):
    env.session(env.clone)
    proc = tmp_path / "proc" / "77"
    proc.mkdir(parents=True)
    (proc / "cmdline").write_bytes(b"claude\0--resume\0" + SID.encode())
    with pytest.raises(T3ccError, match="pid 77"):
        sender.send(SID, SendOptions(env="box"), env.local.paths, proc_root=tmp_path / "proc")


def test_send_refuses_empty_or_homeless_sessions(env):
    env.local.transcript(env.clone, [{"type": "mode"}], SID)
    with pytest.raises(T3ccError, match="no user message"):
        send(env)
    env.local.transcript(env.clone, [user("x")], SID, stamp_cwd=False)
    with pytest.raises(T3ccError, match="records no working directory"):
        send(env)


def test_dry_run_does_not_connect(env):
    env.session(env.clone)
    result = send(env, dry_run=True)
    assert result.received is None and result.prepared.manifest.payload == []


def test_local_thread_note(env, server):
    project = env.local.project(str(env.clone))
    env.local.thread(project, provider="claudeAgent", resume=SID)
    env.session(env.clone)
    assert "local T3 Code thread" in send(env).notes[-1]
    env.local.paths.t3_db.unlink()
    assert sender.local_thread_note(env.local.paths, SID) is None


def test_received_needs_a_result():
    with pytest.raises(T3ccError, match="printed no result: oops"):
        sender._received("oops\n[1]\n")


def run_cli(env, *argv):
    out = io.StringIO()
    code = cli.main(list(argv), paths=env.local.paths, out=out)
    return code, out.getvalue()


def test_cli_send(env, server, monkeypatch, capsys):
    env.session(env.clone)
    code, out = run_cli(env, "send", SID, "--env", "box", "--dry-run")
    assert code == 0 and out.startswith(f"would send {SID} to box: ") and out.endswith("payload: none\n")
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    code, out = run_cli(env, "send", SID, "--env", "box", "--archive", str(env.tmp / "moved"))
    assert code == 0
    assert f"sent {SID} to box over channel: thread import:claudeAgent:{SID} in {env.remote}" in out
    assert f"removed the local copy, archived to {env.tmp / 'moved' / SID}.tar.xz" in out
    assert "sending through the terminal: 100%\n" in capsys.readouterr().err


def test_cli_send_notes_and_plain_removal(env, server):
    project = env.local.project(str(env.clone))
    env.local.thread(project, provider="claudeAgent", resume=SID)
    env.session(env.clone)
    code, out = run_cli(env, "send", SID, "--env", "box")
    assert "removed the local copy\n" in out and "note: the local T3 Code thread" in out


def test_default_archive_dir(tmp_path):
    assert cli.default_archive_dir({"XDG_STATE_HOME": str(tmp_path)}) == tmp_path / "t3cc" / "moved"
    assert cli.default_archive_dir({}) == Path.home() / ".local" / "state" / "t3cc" / "moved"


def test_progress_is_quiet_off_a_terminal(monkeypatch, capsys):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)
    cli._progress(1, 2)
    assert capsys.readouterr().err == ""


def test_cli_send_copy(env, server):
    source = env.session(env.clone)
    code, out = run_cli(env, "send", SID, "--env", "box", "--copy")
    assert code == 0 and "removed" not in out and source.exists()


def test_send_stops_on_a_failed_pre_send_hook(env, server):
    source = env.session(env.clone)
    with pytest.raises(T3ccError, match="pre-send hook `exit 3` failed"):
        send(env, hooks={"pre-send": "exit 3"})
    assert source.exists() and env.remote_ids() == set()


def test_send_reads_hooks_from_the_config(env, server):
    seen = env.tmp / "seen"
    config = env.tmp / "config.toml"
    origin = f"{env.origin}".removesuffix(".git")
    config.write_text(
        f'''[hooks]
pre-send = "echo global >> {seen}"
post-send = "echo global-post >> {seen}"

[env.box.hooks]
pre-send = "echo env >> {seen}"

[repo."{origin}".hooks]
post-send = "echo repo >> {seen}"
'''
    )
    env.session(env.clone)
    send(env, config=config)
    assert seen.read_text().splitlines() == ["env", "repo"]


def test_send_runs_pre_send_from_home_when_the_directory_is_gone(env, server, tmp_path):
    gone = tmp_path / "gone"
    gone.mkdir()
    env.session(gone)
    gone.rmdir()
    (tmp_path / "home").mkdir()
    with pytest.raises(T3ccError, match="pre-send hook `pwd; exit 1` failed \\(1\\): " + str(tmp_path / "home")):
        send(env, hooks={"pre-send": "pwd; exit 1"})


def test_cli_send_without_hooks(env, server, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    (tmp_path / "xdg" / "t3cc").mkdir(parents=True)
    (tmp_path / "xdg" / "t3cc" / "config.toml").write_text('[hooks]\npre-send = "exit 9"\n')
    env.session(env.clone)
    assert run_cli(env, "send", SID, "--env", "box", "--copy")[0] == 1
    assert run_cli(env, "send", SID, "--env", "box", "--copy", "--no-hooks")[0] == 0
