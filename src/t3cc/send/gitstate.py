import io
import re
import subprocess
import tarfile
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from t3cc.errors import T3ccError

SCP_LIKE = re.compile(r"^(?:[^@/]+@)?(?P<host>[^:/]+):(?P<path>[^/].*)$")


def git(cwd: str | Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise T3ccError(f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip()}")
    return result.stdout


def normalize_origin(url: str) -> str:
    url = url.strip()
    if "://" in url:
        rest = url.split("://", 1)[1]
        host_part, _, path = rest.partition("/")
        host = host_part.rsplit("@", 1)[-1].split(":", 1)[0]
    elif match := SCP_LIKE.match(url):
        host, path = match.group("host"), match.group("path")
    else:
        return url.removesuffix("/").removesuffix(".git").lower()
    return f"{host}/{path.strip('/').removesuffix('.git')}".lower()


def origin_url(cwd: str | Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(cwd), "remote", "get-url", "origin"], capture_output=True, text=True, check=False
    )
    return normalize_origin(result.stdout) if result.returncode == 0 else None


def remote_urls(cwd: str | Path) -> set[str]:
    result = subprocess.run(["git", "-C", str(cwd), "remote", "-v"], capture_output=True, text=True, check=False)
    return {normalize_origin(line.split()[1]) for line in result.stdout.splitlines() if len(line.split()) >= 2}


@dataclass(frozen=True)
class GitState:
    top: str
    root: str
    origin: str | None
    branch: str | None
    head: str
    linked: bool
    relative_cwd: str
    dirty: bool
    unpushed: int
    remotes: list[str] = field(default_factory=list)
    ref: str = "HEAD"

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "GitState":
        return cls(**data)


def _local_branch(top: str, branch: str | None) -> str | None:
    if not branch or branch == "HEAD":
        return None
    probe = subprocess.run(
        ["git", "-C", top, "rev-parse", "--verify", "-q", f"refs/heads/{branch}"], capture_output=True, check=False
    )
    return f"refs/heads/{branch}" if probe.returncode == 0 else None


def inspect(cwd: str, branch: str | None = None) -> GitState | None:
    if not Path(cwd).is_dir():
        return None
    probe = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False
    )
    if probe.returncode != 0:
        return None
    top = probe.stdout.strip()
    remotes = git(top, "remote").split()
    origin = git(top, "remote", "get-url", "origin").strip() if "origin" in remotes else None
    current = git(top, "branch", "--show-current").strip() or None
    ref = _local_branch(top, branch) if branch != current else None
    if ref is None:
        branch, ref = current, "HEAD"
    head = git(top, "rev-parse", ref).strip()
    git_dir = Path(top, git(top, "rev-parse", "--git-dir").strip()).resolve()
    common_dir = Path(top, git(top, "rev-parse", "--git-common-dir").strip()).resolve()
    not_on_origin = ["--not", "--remotes=origin"] if origin else []
    unpushed = int(git(top, "rev-list", "--count", ref, *not_on_origin).strip())
    return GitState(
        top=top,
        root=str(common_dir.parent) if common_dir.name == ".git" else top,
        origin=origin,
        branch=branch,
        head=head,
        linked=git_dir != common_dir,
        relative_cwd=str(Path(cwd).resolve().relative_to(Path(top).resolve())),
        dirty=ref == "HEAD" and bool(git(top, "status", "--porcelain").strip()),
        unpushed=unpushed,
        remotes=sorted(remote_urls(top)),
        ref=ref,
    )


def payload(state: GitState) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    if state.unpushed:
        with tempfile.TemporaryDirectory() as scratch:
            bundle = Path(scratch, "head.bundle")
            not_on_origin = ["--not", "--remotes=origin"] if state.origin else []
            git(state.top, "bundle", "create", str(bundle), state.ref, *not_on_origin)
            files["bundle"] = bundle.read_bytes()
    if not state.dirty:
        return files
    diff = subprocess.run(["git", "-C", state.top, "diff", "--binary", "HEAD"], capture_output=True, check=True).stdout
    if diff:
        files["diff"] = diff
    untracked = [
        name for name in git(state.top, "ls-files", "--others", "--exclude-standard", "-z").split("\0") if name
    ]
    if untracked:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            for name in untracked:
                tar.add(Path(state.top, name), arcname=name, recursive=False)
        files["untracked"] = buffer.getvalue()
    return files
