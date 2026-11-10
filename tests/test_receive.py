import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from conftest import assistant, user
from gitrepo import commit, git, isolated_git, origin_and_clone  # noqa: F401
from t3cc import cli
from t3cc.claude.store import ClaudeStore
from t3cc.errors import T3ccError
from t3cc.send import archive, gitstate, receive
from t3cc.send.archive import Manifest
from t3cc.t3.repo import T3Repository

pytestmark = pytest.mark.usefixtures("isolated_git")

SID = "33333333-3333-4333-8333-333333333333"
NO_WT = staticmethod(lambda name: None)

WT = """#!/bin/sh
root=$2; shift 3
create=""; base=""
while [ $# -gt 0 ]; do
  case $1 in --create) create=1;; --base) base=$2; shift;; --no-cd|--yes) ;; *) branch=$1;; esac
  shift
done
path="$(dirname "$root")/$(basename "$root").worktrees/$branch"
if [ -n "$create" ]; then git -C "$root" worktree add -q -b "$branch" "$path" "$base"
else git -C "$root" worktree add -q "$path" "$branch"; fi
"""


class Setup:
    def __init__(self, world, tmp_path):
        self.world = world
        self.tmp = tmp_path
        self.origin, self.local = origin_and_clone(tmp_path / "local")
        self.remote = tmp_path / "remote" / "app"
        git(tmp_path, "clone", "-q", str(self.origin), str(self.remote))
        self.project = world.project(str(self.remote))
        self.store = ClaudeStore(world.paths.claude_projects)

    def source(self, cwd: Path, records=None) -> Path:
        path = self.tmp / "src-claude" / "proj" / f"{SID}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        artifacts = path.with_suffix("")
        if records is None:
            see = {"type": "user", "message": {"role": "user", "content": f"see {artifacts}/tool-results/r.txt"}}
            records = [user("hello"), assistant("hi"), see]
        lines = [{"sessionId": SID, "cwd": str(cwd), **r} for r in records]
        path.write_text("".join(json.dumps(line, separators=(",", ":")) + "\n" for line in lines))
        if any("tool-results" in json.dumps(r) for r in records):
            (artifacts / "tool-results").mkdir(parents=True, exist_ok=True)
            (artifacts / "tool-results" / "r.txt").write_text("result")
        return path

    def archive(self, cwd: Path, *, dirty_ok=True, records=None, branch=None, **extra) -> Path:
        transcript = self.source(cwd, records)
        state = gitstate.inspect(str(cwd), branch=branch)
        payload = gitstate.payload(state) if state and dirty_ok else {}
        manifest = Manifest(
            SID,
            str(cwd),
            str(transcript.with_suffix("")),
            archive.sha256(transcript.read_bytes()),
            git=state.as_dict() if state else None,
            payload=sorted(payload),
            **extra,
        )
        path = self.tmp / "incoming.tar.xz"
        path.write_bytes(archive.build(transcript, manifest, payload))
        return path

    def receive(self, path: Path, which=lambda name: None):
        return receive.receive(path, self.world.paths, which=which)

    def threads(self):
        return T3Repository(self.world.db()).imported_session_ids()


@pytest.fixture
def setup(world, tmp_path):
    return Setup(world, tmp_path)


def fake_wt(tmp_path, script=WT):
    tool = tmp_path / "bin" / "wt"
    tool.parent.mkdir(exist_ok=True)
    tool.write_text(script)
    tool.chmod(0o755)
    os.environ["PATH"] = f"{tool.parent}:{os.environ['PATH']}"
    return lambda name: str(tool)


def test_receive_into_the_project_root(setup):
    hooks = {"post-checkout": "touch checked-out", "post-import": "echo nope >&2; exit 3"}
    result = setup.receive(setup.archive(setup.local, hooks=hooks))
    assert result.cwd == str(setup.remote) and not result.worktree_created and not result.cloned
    assert result.thread_id == f"import:claudeAgent:{SID}" and result.project == str(setup.remote)
    assert result.warnings == ["post-import hook `echo nope >&2; exit 3` failed (3): nope"]
    assert (setup.remote / "checked-out").exists()
    placed = Path(result.transcript)
    assert placed == setup.store.session_path(setup.remote, SID)
    text = placed.read_text()
    assert f'"cwd":"{setup.remote}"' in text and str(setup.local) not in text
    assert f"{placed.with_suffix('')}/tool-results/r.txt" in text
    assert (placed.with_suffix("") / "tool-results" / "r.txt").read_text() == "result"
    assert setup.threads() == {SID}
    assert json.loads(result.to_json())["cwd"] == str(setup.remote)


def make_feature(setup, name="feature/x"):
    worktree = setup.local.parent / "app.worktrees" / name
    git(setup.local, "worktree", "add", "-q", "-b", name, str(worktree))
    commit(worktree, "feature.txt", "work")
    (worktree / "README").write_text("edited")
    (worktree / "notes").mkdir()
    (worktree / "notes" / "n.txt").write_text("untracked")
    (worktree / "sub").mkdir()
    return worktree


@pytest.mark.parametrize("with_wt", [False, True])
def test_receive_unpushed_work_into_a_new_worktree(setup, with_wt):
    worktree = make_feature(setup)
    which = fake_wt(setup.tmp) if with_wt else NO_WT
    result = setup.receive(setup.archive(worktree / "notes"), which=which)
    target = setup.remote.parent / "app.worktrees" / "feature" / "x"
    assert result.worktree_created and result.cwd == str(target / "notes")
    assert (target / "feature.txt").read_text() == "work"
    assert (target / "README").read_text() == "edited"
    assert (target / "notes" / "n.txt").read_text() == "untracked"
    assert setup.threads() == {SID}


def test_receive_reuses_a_worktree_at_the_same_commit(setup):
    worktree = make_feature(setup)
    git(worktree, "checkout", "-q", "--", "README")
    for path in ("notes/n.txt",):
        (worktree / path).unlink()
    git(worktree, "push", "-q", "origin", "feature/x")
    git(setup.remote, "fetch", "-q", "origin")
    existing = setup.remote.parent / "app.worktrees" / "feature" / "x"
    git(setup.remote, "worktree", "add", "-q", "-b", "feature/x", str(existing), "origin/feature/x")
    result = setup.receive(setup.archive(worktree))
    assert result.cwd == str(existing) and not result.worktree_created


def test_receive_refuses_a_worktree_elsewhere(setup):
    worktree = make_feature(setup)
    existing = setup.remote.parent / "elsewhere"
    git(setup.remote, "worktree", "add", "-q", "-b", "feature/x", str(existing))
    with pytest.raises(T3ccError, match="is not at"):
        setup.receive(setup.archive(worktree))


def test_receive_fast_forwards_an_idle_branch(setup):
    worktree = make_feature(setup)
    git(setup.remote, "branch", "feature/x")
    result = setup.receive(setup.archive(worktree))
    assert result.worktree_created
    assert git(setup.remote, "rev-parse", "feature/x").strip() == git(worktree, "rev-parse", "HEAD").strip()


def test_receive_fast_forwards_with_wt(setup):
    worktree = make_feature(setup)
    git(setup.remote, "branch", "feature/x")
    assert setup.receive(setup.archive(worktree), which=fake_wt(setup.tmp)).worktree_created


def test_receive_refuses_a_diverged_branch(setup):
    worktree = make_feature(setup)
    git(setup.remote, "checkout", "-q", "-b", "feature/x")
    commit(setup.remote, "other.txt")
    git(setup.remote, "checkout", "-q", "main")
    with pytest.raises(T3ccError, match="does not contain"):
        setup.receive(setup.archive(worktree))


def test_receive_needs_the_commit(setup):
    worktree = make_feature(setup)
    with pytest.raises(T3ccError, match="lacks commit"):
        setup.receive(setup.archive(worktree, dirty_ok=False))


def test_receive_on_another_branch_of_the_main_checkout(setup):
    git(setup.local, "checkout", "-q", "-b", "topic")
    git(setup.local, "push", "-q", "origin", "topic")
    result = setup.receive(setup.archive(setup.local))
    assert result.worktree_created and result.cwd.endswith("app.worktrees/topic")


def test_receive_detached(setup):
    git(setup.local, "checkout", "-q", "--detach")
    assert setup.receive(setup.archive(setup.local)).cwd == str(setup.remote)


def test_receive_detached_elsewhere(setup):
    commit(setup.local, "later.txt")
    git(setup.local, "push", "-q", "origin", "main")
    git(setup.local, "checkout", "-q", "--detach")
    with pytest.raises(T3ccError, match="detached"):
        setup.receive(setup.archive(setup.local))


def test_receive_with_a_map_and_no_git(setup):
    plain = setup.tmp / "plain"
    plain.mkdir()
    result = setup.receive(setup.archive(plain, target_root=str(setup.remote)))
    assert result.cwd == str(setup.remote)


def test_receive_without_origin_or_map(setup):
    plain = setup.tmp / "plain"
    plain.mkdir()
    with pytest.raises(T3ccError, match="--map LOCAL=REMOTE"):
        setup.receive(setup.archive(plain))


def test_receive_map_without_a_project(setup):
    with pytest.raises(T3ccError, match=r"no T3 project here for /nowhere: add one, or send with --clone PATH"):
        setup.receive(setup.archive(setup.local, target_root="/nowhere"))


def test_receive_without_a_matching_project(setup):
    with setup.world.db() as con:
        con.execute("UPDATE projection_projects SET deleted_at = 'x'")
    with pytest.raises(T3ccError, match="no T3 project here for "):
        setup.receive(setup.archive(setup.local))


def test_receive_clones_and_creates_the_project(setup):
    with setup.world.db() as con:
        con.execute("UPDATE projection_projects SET deleted_at = 'x'")
    clone = setup.tmp / "remote" / "fresh"
    result = setup.receive(setup.archive(setup.local, clone=str(clone)))
    assert result.cloned and result.project == str(clone) and (clone / "README").exists()
    with pytest.raises(T3ccError, match="already exists"):
        receive._clone(Manifest(SID, "", "", "", clone=str(clone)), gitstate.inspect(str(setup.local)))
    with pytest.raises(T3ccError, match="--clone needs"):
        receive._clone(Manifest(SID, "", "", "", clone=str(clone)), None)


def test_receive_refuses_ambiguous_origins(setup):
    twin = setup.tmp / "remote" / "twin"
    git(setup.tmp, "clone", "-q", str(setup.origin), str(twin))
    setup.world.project(str(twin))
    setup.world.project(str(setup.tmp / "gone"))
    with pytest.raises(T3ccError, match="several T3 projects"):
        setup.receive(setup.archive(setup.local))


def test_receive_refuses_mixing_with_local_changes(setup):
    (setup.local / "README").write_text("session edit")
    (setup.remote / "README").write_text("local edit")
    with pytest.raises(T3ccError, match="has local changes"):
        setup.receive(setup.archive(setup.local))
    assert (setup.remote / "README").read_text() == "local edit"


def test_receive_refuses_untracked_clashes(setup, tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo("README")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    (tmp_path / "payload").mkdir()
    (tmp_path / "payload" / "untracked").write_bytes(buffer.getvalue())
    with pytest.raises(T3ccError, match="untracked files already exist"):
        receive.apply_payload(str(setup.remote), Manifest(SID, "", "", "", payload=["untracked"]), tmp_path)


def test_receive_refuses_a_session_already_here(setup):
    setup.world.transcript("/elsewhere", [user("x")], SID)
    with pytest.raises(T3ccError, match="already on this environment"):
        setup.receive(setup.archive(setup.local))


def test_receive_discards_what_t3_would_not_import(setup):
    with pytest.raises(T3ccError, match="would not import the session: empty"):
        setup.receive(setup.archive(setup.local, records=[{"type": "mode"}]))
    assert setup.store.by_session_id(SID) is None


def test_receive_stops_on_a_failed_post_checkout_hook(setup):
    with pytest.raises(T3ccError, match="post-checkout hook `false` failed"):
        setup.receive(setup.archive(setup.local, hooks={"post-checkout": "false"}))
    assert setup.store.by_session_id(SID) is None


def test_receive_reports_wt_failures(setup):
    worktree = make_feature(setup)
    with pytest.raises(T3ccError, match="wt switch feature/x failed: broken"):
        setup.receive(setup.archive(worktree), which=fake_wt(setup.tmp, "#!/bin/sh\necho broken >&2\nexit 1\n"))
    with pytest.raises(T3ccError, match="no worktree for feature/x"):
        setup.receive(setup.archive(worktree), which=fake_wt(setup.tmp, "#!/bin/sh\nexit 0\n"))


def test_cli_receive(setup, capsys):
    out = io.StringIO()
    assert cli.main(["receive", str(setup.archive(setup.local))], paths=setup.world.paths, out=out) == 0
    assert json.loads(out.getvalue())["thread_id"] == f"import:claudeAgent:{SID}"
    assert cli.main(["receive", str(setup.tmp / "missing.tar.xz")], paths=setup.world.paths, out=out) == 1
    assert "could not read the session archive" in capsys.readouterr().err


def test_receive_matches_a_project_through_another_remote(setup):
    git(setup.remote, "remote", "set-url", "origin", "git@mirror.example.com:Someone/App.git")
    git(setup.remote, "remote", "add", "upstream", str(setup.origin))
    assert setup.receive(setup.archive(setup.local)).project == str(setup.remote)


def test_receive_prefers_the_project_with_the_same_origin(setup):
    twin = setup.tmp / "remote" / "fork"
    git(setup.tmp, "clone", "-q", str(setup.origin), str(twin))
    git(twin, "remote", "set-url", "origin", "https://example.com/fork/app.git")
    git(twin, "remote", "add", "upstream", str(setup.origin))
    setup.world.project(str(twin))
    assert setup.receive(setup.archive(setup.local)).project == str(setup.remote)


def test_receive_the_branch_the_session_last_ran_on(setup):
    git(setup.local, "checkout", "-q", "-b", "topic")
    tip = commit(setup.local, "topic.txt", "topic work")
    git(setup.local, "checkout", "-q", "main")
    result = setup.receive(setup.archive(setup.local, branch="topic"))
    target = setup.remote.parent / "app.worktrees" / "topic"
    assert result.cwd == str(target) and result.worktree_created
    assert git(target, "rev-parse", "HEAD").strip() == tip
