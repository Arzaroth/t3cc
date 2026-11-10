"""Real git repositories for tests: a bare origin and checkouts of it."""

import subprocess
from pathlib import Path

import pytest


def git(cwd, *args) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout


def commit(repo: Path, name: str, content: str = "x", message: str | None = None) -> str:
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(content)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message or f"add {name}")
    return git(repo, "rev-parse", "HEAD").strip()


def origin_and_clone(root: Path, name: str = "app") -> tuple[Path, Path]:
    origin = root / "origin" / f"{name}.git"
    seed = root / "seed" / name
    origin.parent.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "--bare", "-b", "main", str(origin))
    git(root, "clone", "-q", str(origin), str(seed))
    commit(seed, "README", "hello")
    git(seed, "push", "-q", "origin", "main")
    clone = root / "repos" / name
    git(root, "clone", "-q", str(origin), str(clone))
    return origin, clone


@pytest.fixture
def isolated_git(tmp_path, monkeypatch):
    config = tmp_path / "gitconfig"
    config.write_text("[user]\n\tname = Test\n\temail = test@example.com\n[init]\n\tdefaultBranch = main\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return config
