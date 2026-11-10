import json
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass, field
from pathlib import Path

from resumove.transcript import relocate

from t3cc import importer
from t3cc.claude import transcript as claude_transcript
from t3cc.claude.store import ClaudeStore
from t3cc.errors import T3ccError
from t3cc.paths import Paths
from t3cc.send import archive, hooks
from t3cc.send.archive import Manifest
from t3cc.send.gitstate import GitState, git, normalize_origin, origin_url, remote_urls
from t3cc.t3 import db
from t3cc.t3.repo import Project, T3Repository

Which = Callable[[str], str | None]


@dataclass(frozen=True)
class Received:
    thread_id: str
    project: str
    cwd: str
    transcript: str
    worktree_created: bool = False
    cloned: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self))


def hook_variables(manifest: Manifest, cwd: str) -> dict[str, str]:
    return {
        "T3CC_SESSION": manifest.session_id,
        "T3CC_ENV": manifest.environment or "",
        "T3CC_BRANCH": (manifest.git or {}).get("branch") or "",
        "T3CC_REMOTE_DIR": cwd,
    }


def find_project(projects: list[Project], manifest: Manifest, state: GitState | None) -> Project | None:
    if manifest.target_root:
        root = str(Path(manifest.target_root).expanduser().resolve())
        return next((p for p in projects if p.workspace_root == root), None)
    if state is None or state.origin is None:
        raise T3ccError("the session did not run in a checkout with an origin: send it with --map LOCAL=REMOTE")
    origin = normalize_origin(state.origin)
    wanted = set(state.remotes) | {origin}
    remotes = {p.workspace_root: remote_urls(p.workspace_root) for p in projects if Path(p.workspace_root).is_dir()}
    matches = [p for p in projects if remotes.get(p.workspace_root, set()) & wanted]
    if len(matches) > 1:
        matches = [p for p in matches if origin_url(p.workspace_root) == origin] or matches
    if len(matches) > 1:
        roots = ", ".join(p.workspace_root for p in matches)
        raise T3ccError(f"several T3 projects share a remote with {origin}: {roots}; pick one with --map")
    return matches[0] if matches else None


def _worktree_of(root: str, branch: str) -> str | None:
    current = None
    for line in git(root, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            current = line.removeprefix("worktree ")
        elif line == f"branch refs/heads/{branch}":
            return current
    return None


def _has_commit(root: str, sha: str) -> bool:
    probe = subprocess.run(["git", "-C", root, "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True, check=False)
    return probe.returncode == 0


def _branch_tip(root: str, branch: str) -> str | None:
    probe = subprocess.run(
        ["git", "-C", root, "rev-parse", "--verify", "-q", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return probe.stdout.strip() if probe.returncode == 0 else None


def _create_worktree(root: str, branch: str, head: str, which: Which) -> str:
    tip = _branch_tip(root, branch)
    if tip and tip != head:
        if subprocess.run(["git", "-C", root, "merge-base", "--is-ancestor", tip, head], check=False).returncode:
            raise T3ccError(f"branch {branch} here is at {tip[:12]}, which the session's {head[:12]} does not contain")
        git(root, "branch", "-f", branch, head)
    if which("wt"):
        create = [] if tip else ["--create", "--base", head]
        result = subprocess.run(
            ["wt", "-C", root, "switch", *create, branch, "--no-cd", "--yes"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise T3ccError(f"wt switch {branch} failed: {(result.stderr or result.stdout).strip()}")
    else:
        path = Path(root).parent / f"{Path(root).name}.worktrees" / branch
        new_branch = [] if tip else ["-b", branch]
        git(root, "worktree", "add", "-q", *new_branch, str(path), branch if tip else head)
    created = _worktree_of(root, branch)
    if created is None:
        raise T3ccError(f"no worktree for {branch} after creating it")
    return created


def _fast_forward_root(root: str, branch: str, root_head: str, head: str) -> str:
    if subprocess.run(["git", "-C", root, "merge-base", "--is-ancestor", root_head, head], check=False).returncode:
        raise T3ccError(f"{root} is on {branch} at {root_head[:12]}, which the session's {head[:12]} does not contain")
    if git(root, "status", "--porcelain", "--untracked-files=no").strip():
        raise T3ccError(f"{root} is behind the session on {branch} and has local changes: commit or stash them")
    git(root, "merge", "-q", "--ff-only", head)
    return head


def checkout(root: str, manifest: Manifest, state: GitState, staging: Path, which: Which) -> tuple[str, bool]:
    subprocess.run(["git", "-C", root, "fetch", "-q", "origin"], capture_output=True, check=False)
    if "bundle" in manifest.payload:
        git(root, "fetch", "-q", str(staging / archive.PAYLOAD / "bundle"), state.ref)
    if not _has_commit(root, state.head):
        raise T3ccError(f"this checkout lacks commit {state.head[:12]}: push it, or send with --dirty")
    root_head = git(root, "rev-parse", "HEAD").strip()
    root_branch = git(root, "branch", "--show-current").strip() or None
    if state.branch is not None and not state.linked and state.branch == root_branch and root_head != state.head:
        root_head = _fast_forward_root(root, root_branch, root_head, state.head)
    if state.branch is None or (not state.linked and state.branch == root_branch and root_head == state.head):
        if root_head != state.head:
            raise T3ccError(f"{root} is at {root_head[:12]}, the session ran at {state.head[:12]} (detached)")
        return root, False
    existing = _worktree_of(root, state.branch)
    if existing:
        if git(existing, "rev-parse", "HEAD").strip() != state.head:
            raise T3ccError(f"the worktree for {state.branch} at {existing} is not at {state.head[:12]}")
        return existing, False
    return _create_worktree(root, state.branch, state.head, which), True


def apply_payload(target: str, manifest: Manifest, staging: Path) -> None:
    payload = staging / archive.PAYLOAD
    dirty_payload = "diff" in manifest.payload or "untracked" in manifest.payload
    if dirty_payload and git(target, "status", "--porcelain").strip():
        raise T3ccError(f"{target} has local changes: the session's uncommitted work would mix with them")
    if "diff" in manifest.payload:
        git(target, "apply", "--binary", str(payload / "diff"))
    if "untracked" in manifest.payload:
        with tarfile.open(payload / "untracked") as tar:
            clashes = [name for name in tar.getnames() if Path(target, name).exists()]
            if clashes:
                raise T3ccError(f"untracked files already exist in {target}: {', '.join(clashes)}")
            tar.extractall(target, filter="data")


def place(manifest: Manifest, staging: Path, cwd: Path, store: ClaudeStore) -> Path:
    if store.by_session_id(manifest.session_id):
        raise T3ccError(f"session {manifest.session_id} is already on this environment")
    dest = store.session_path(cwd, manifest.session_id)
    artifacts = dest.with_suffix("")
    data = relocate(
        (staging / archive.TRANSCRIPT).read_bytes(),
        Path(manifest.source_artifacts),
        artifacts,
        manifest.source_cwd,
        cwd,
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    if (staging / archive.ARTIFACTS).is_dir():
        shutil.copytree(staging / archive.ARTIFACTS, artifacts)
    dest.write_bytes(data)
    return dest


def _discard(path: Path) -> None:
    path.unlink(missing_ok=True)
    shutil.rmtree(path.with_suffix(""), ignore_errors=True)


def _clone(manifest: Manifest, state: GitState | None) -> str:
    assert manifest.clone is not None
    if state is None or state.origin is None:
        raise T3ccError("--clone needs a session that ran in a checkout with an origin")
    target = Path(manifest.clone).expanduser()
    if target.exists():
        raise T3ccError(f"cannot clone into {target}: it already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    git(target.parent, "clone", "-q", state.origin, str(target))
    return str(target.resolve())


def receive(archive_path: Path, paths: Paths, which: Which = shutil.which) -> Received:
    staging = Path(tempfile.mkdtemp(prefix="t3cc-receive-"))
    try:
        manifest = archive.extract(archive_path, staging)
        state = GitState.from_dict(manifest.git) if manifest.git else None
        store = ClaudeStore(paths.claude_projects)
        with closing(db.connect(paths, write=True)) as con:
            repo = T3Repository(con)
            project = find_project(repo.projects(), manifest, state)
            cloned = False
            if project is None:
                if not manifest.clone:
                    wanted = manifest.target_root or normalize_origin(state.origin if state and state.origin else "")
                    raise T3ccError(f"no T3 project here for {wanted}: add one, or send with --clone PATH")
                root, cloned = _clone(manifest, state), True
            else:
                root = project.workspace_root
            if state is None:
                target, created = root, False
            else:
                target, created = checkout(root, manifest, state, staging, which)
                apply_payload(target, manifest, staging)
            cwd = Path(target, state.relative_cwd) if state else Path(target)
            failure = hooks.run(manifest.hooks.get("post-checkout"), str(cwd), hook_variables(manifest, str(cwd)))
            if failure:
                raise T3ccError(f"post-checkout hook {failure}")
            placed = place(manifest, staging, cwd, store)
            try:
                options = importer.ImportOptions(project=root, create_project=cloned)
                plan = importer.plan_import(
                    repo, [claude_transcript.parse(placed)], options, t3_worktrees=paths.t3_worktrees
                )
                if not plan.items:
                    raise T3ccError(f"T3 Code would not import the session: {plan.skipped[0][1].value}")
                result = importer.apply_import(repo, store, plan)[0]
            except BaseException:
                _discard(placed)
                raise
        warnings = []
        failure = hooks.run(manifest.hooks.get("post-import"), str(cwd), hook_variables(manifest, str(cwd)))
        if failure:
            warnings.append(f"post-import hook {failure}")
        return Received(
            thread_id=result.item.thread_id,
            project=result.project.workspace_root,
            cwd=str(cwd),
            transcript=str(placed),
            worktree_created=created,
            cloned=cloned,
            warnings=warnings,
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)
