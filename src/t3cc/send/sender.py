import json
import shlex
import shutil
import tempfile
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from t3cc import capabilities, procs, sync
from t3cc.claude import transcript as claude_transcript
from t3cc.claude.store import ClaudeStore
from t3cc.errors import T3ccError
from t3cc.paths import Paths
from t3cc.remote import catalog
from t3cc.remote.rpc import Connection
from t3cc.remote.shell import RemoteShell
from t3cc.send import archive, gitstate, transport
from t3cc.send.archive import Manifest
from t3cc.send.gitstate import GitState
from t3cc.t3 import db
from t3cc.t3.repo import T3Repository

REQUIRED = frozenset({"receive"})
INCOMING = "$HOME/.cache/t3cc/incoming"


@dataclass(frozen=True)
class SendOptions:
    env: str
    transport: str | None = None
    dirty: bool = False
    copy: bool = False
    archive_to: Path | None = None
    map: str | None = None
    clone: str | None = None
    hooks: dict[str, str] = field(default_factory=dict)
    dry_run: bool = False


@dataclass
class Prepared:
    path: Path
    session_id: str
    state: GitState | None
    manifest: Manifest
    data: bytes


@dataclass
class SendResult:
    prepared: Prepared
    received: dict | None = None
    transport: str | None = None
    archived: Path | None = None
    removed: bool = False
    notes: list[str] = field(default_factory=list)


def map_root(mapping: str, base: str) -> str:
    local, sep, remote = mapping.partition("=")
    if not sep or not local or not remote:
        raise T3ccError("--map takes LOCAL=REMOTE")
    local_path = Path(local).expanduser().resolve()
    base_path = Path(base).resolve()
    if not base_path.is_relative_to(local_path):
        raise T3ccError(f"--map {local} does not contain the session's checkout {base}")
    return str(Path(remote, base_path.relative_to(local_path)))


def prepare(ref: str, options: SendOptions, paths: Paths, proc_root: Path = procs.PROC) -> Prepared:
    path = ClaudeStore(paths.claude_projects).find(ref)
    parsed = claude_transcript.parse(path)
    if not parsed.has_user_message:
        raise T3ccError(f"{parsed.session_id} has no user message: nothing to send")
    if pid := sync.session_pid(parsed.session_id, proc_root):
        raise T3ccError(f"claude (pid {pid}) has {parsed.session_id} open: close it first")
    if not parsed.cwd:
        raise T3ccError(f"{parsed.session_id} records no working directory")
    state = gitstate.inspect(parsed.cwd)
    if state and (state.dirty or state.unpushed) and not options.dirty:
        raise T3ccError(
            f"{state.top} has {state.unpushed} unpushed commits"
            f"{' and uncommitted changes' if state.dirty else ''}: push them, or send with --dirty to carry them"
        )
    payload = gitstate.payload(state) if state and options.dirty else {}
    target_root = map_root(options.map, state.root if state else parsed.cwd) if options.map else None
    transcript_bytes = path.read_bytes()
    manifest = Manifest(
        session_id=parsed.session_id,
        source_cwd=parsed.cwd,
        source_artifacts=str(path.with_suffix("")),
        transcript_sha256=archive.sha256(transcript_bytes),
        git=state.as_dict() if state else None,
        target_root=target_root,
        clone=options.clone,
        hooks={k: v for k, v in options.hooks.items() if k in ("post-checkout", "post-import")},
        payload=sorted(payload),
    )
    return Prepared(path, parsed.session_id, state, manifest, archive.build(path, manifest, payload))


def local_thread_note(paths: Paths, session_id: str) -> str | None:
    if not paths.t3_db.exists():
        return None
    with closing(db.connect(paths, write=False)) as con:
        repo = T3Repository(con)
        known = repo.imported_session_ids() | repo.native_session_ids()
    if session_id in known:
        return "the local T3 Code thread of this session stays: archive it in T3 Code once you are done with it"
    return None


def _received(output: str) -> dict:
    for line in reversed(output.strip().splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and "thread_id" in data:
            return data
    raise T3ccError(f"t3cc receive printed no result: {output.strip()}")


def deliver(
    prepared: Prepared,
    options: SendOptions,
    shell: RemoteShell,
    label: str,
    which: transport.Which = shutil.which,
    progress: transport.Progress | None = None,
) -> tuple[dict, str, list[str]]:
    peer = capabilities.probe(shell)
    missing = peer.missing(REQUIRED)
    if peer.version is None or not peer.protocol or missing:
        raise T3ccError(f"{label}: {peer.status()}")
    incoming = shell.check(f'mkdir -p "{INCOMING}" && cd "{INCOMING}" && pwd').strip()
    dest = f"{incoming}/{prepared.session_id}.tar.xz"
    with tempfile.TemporaryDirectory() as scratch:
        local = Path(scratch, "session.tar.xz")
        local.write_bytes(prepared.data)
        try:
            sent = transport.send(
                local,
                dest,
                shell,
                sha256=archive.sha256(prepared.data),
                requested=options.transport,
                which=which,
                progress=progress,
            )
            result = shell.run(f"t3cc receive {shlex.quote(dest)}", timeout=1800)
        finally:
            shell.run(f"rm -f {shlex.quote(dest)}")
    if not result.ok:
        raise T3ccError(f"{label}: t3cc receive failed: {result.output.strip()}")
    return _received(result.output), sent.used, sent.warnings


def finish(prepared: Prepared, options: SendOptions) -> tuple[Path | None, bool]:
    if options.copy:
        return None, False
    archived = None
    if options.archive_to:
        options.archive_to.mkdir(parents=True, exist_ok=True)
        archived = options.archive_to / f"{prepared.session_id}.tar.xz"
        archived.write_bytes(prepared.data)
    prepared.path.unlink()
    shutil.rmtree(prepared.path.with_suffix(""), ignore_errors=True)
    return archived, True


def send(
    ref: str,
    options: SendOptions,
    paths: Paths,
    *,
    which: transport.Which = shutil.which,
    proc_root: Path = procs.PROC,
    progress: transport.Progress | None = None,
) -> SendResult:
    prepared = prepare(ref, options, paths, proc_root)
    result = SendResult(prepared)
    if options.dry_run:
        return result
    env = catalog.find(catalog.load(paths), options.env)
    with Connection.open(env) as conn, RemoteShell(conn) as shell:
        result.received, result.transport, warnings = deliver(prepared, options, shell, env.label, which, progress)
    result.notes.extend(warnings)
    result.notes.extend(result.received.get("warnings", []))
    result.archived, result.removed = finish(prepared, options)
    if note := local_thread_note(paths, prepared.session_id):
        result.notes.append(note)
    if prepared.state and prepared.state.linked and result.removed:
        result.notes.append(f"{prepared.state.top} is still checked out here: `wt remove` it when you are done")
    return result
