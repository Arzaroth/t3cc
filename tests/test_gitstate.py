import io
import tarfile

import pytest

from gitrepo import commit, git, isolated_git, origin_and_clone  # noqa: F401
from t3cc.errors import T3ccError
from t3cc.send import gitstate
from t3cc.send.gitstate import GitState

pytestmark = pytest.mark.usefixtures("isolated_git")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:Arzaroth/gliff.git", "github.com/Arzaroth/gliff"),
        ("ssh://git@Box.Example.com:2222/Arzaroth/t3cc.git", "box.example.com/Arzaroth/t3cc"),
        ("https://github.com/Arzaroth/gliff", "github.com/Arzaroth/gliff"),
        ("https://user:pw@github.com/Arzaroth/gliff.git/", "github.com/Arzaroth/gliff"),
        ("/srv/git/app.git", "/srv/git/app"),
    ],
)
def test_normalize_origin(url, expected):
    assert gitstate.normalize_origin(url) == expected


def test_inspect_a_clean_pushed_checkout(tmp_path):
    origin, clone = origin_and_clone(tmp_path)
    (clone / "sub").mkdir()
    state = gitstate.inspect(str(clone / "sub"))
    assert state == GitState(
        top=str(clone),
        root=str(clone),
        origin=str(origin),
        branch="main",
        head=git(clone, "rev-parse", "HEAD").strip(),
        linked=False,
        relative_cwd="sub",
        dirty=False,
        unpushed=0,
    )
    assert GitState.from_dict(state.as_dict()) == state
    assert gitstate.payload(state) == {}


def test_inspect_outside_git(tmp_path):
    assert gitstate.inspect(str(tmp_path)) is None
    assert gitstate.inspect(str(tmp_path / "gone")) is None


def test_inspect_a_linked_worktree_with_unpushed_work(tmp_path):
    _, clone = origin_and_clone(tmp_path)
    worktree = tmp_path / "repos" / "app.worktrees" / "feature" / "x"
    git(clone, "worktree", "add", "-q", "-b", "feature/x", str(worktree))
    commit(worktree, "a.txt", "one")
    commit(worktree, "b.txt", "two")
    (worktree / "README").write_text("changed")
    (worktree / "new dir").mkdir()
    (worktree / "new dir" / "n.txt").write_text("untracked")
    (worktree / ".gitignore").write_text("ignored\n")
    (worktree / "ignored").write_text("nope")
    state = gitstate.inspect(str(worktree))
    assert state is not None
    assert state.linked and state.branch == "feature/x" and state.dirty and state.unpushed == 2
    assert state.root == str(clone) and state.top == str(worktree)
    files = gitstate.payload(state)
    assert sorted(files) == ["bundle", "diff", "untracked"]
    assert b"changed" in files["diff"]
    with tarfile.open(fileobj=io.BytesIO(files["untracked"])) as tar:
        assert sorted(tar.getnames()) == [".gitignore", "new dir/n.txt"]
    bundle = tmp_path / "x.bundle"
    bundle.write_bytes(files["bundle"])
    assert git(clone, "bundle", "verify", str(bundle)).count("\n") >= 1


def test_inspect_without_origin(tmp_path):
    repo = tmp_path / "solo"
    repo.mkdir()
    git(repo, "init", "-q")
    commit(repo, "f")
    git(repo, "checkout", "-q", "--detach")
    state = gitstate.inspect(str(repo))
    assert state is not None
    assert state.origin is None and state.branch is None and state.unpushed == 1
    assert state.root == str(repo)
    assert list(gitstate.payload(state)) == ["bundle"]


def test_git_failure(tmp_path):
    with pytest.raises(T3ccError, match="git rev-parse HEAD failed"):
        gitstate.git(tmp_path, "rev-parse", "HEAD")
