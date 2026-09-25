"""Bring an imported session's original Claude Code transcript up to date with the copy T3 Code writes to.

T3 resumes a Claude session from the thread's directory, and Claude Code keeps a transcript per directory.
When that directory is not the one the session started in, T3's turns land in a second file with the same
session id, and the original stops moving.
"""

import datetime as dt
import os
import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from t3cc import procs
from t3cc.claude.store import ClaudeStore
from t3cc.errors import T3ccError
from t3cc.paths import backup_path
from t3cc.t3.repo import Thread, pick_threads


class SyncState(StrEnum):
    SAME_FILE = "same-file"
    IN_SYNC = "in-sync"
    FAST_FORWARD = "fast-forward"
    BEHIND = "behind"
    DIVERGED = "diverged"
    FORKED = "forked"
    MISSING = "missing"


@dataclass(frozen=True)
class SyncResult:
    thread: Thread
    state: SyncState
    original: Path
    t3_copy: Path
    backup: Path | None = None
    blocked_by: int | None = None


def compare(original: list[bytes], t3_copy: list[bytes]) -> SyncState:
    if original == t3_copy:
        return SyncState.IN_SYNC
    if t3_copy[: len(original)] == original:
        return SyncState.FAST_FORWARD
    if original[: len(t3_copy)] == t3_copy:
        return SyncState.BEHIND
    return SyncState.DIVERGED


def session_pid(session_id: str, proc_root: Path = procs.PROC) -> int | None:
    """A running `claude` process that has this session open."""
    needle = session_id.encode()
    for pid, args in procs.others(proc_root):
        if any(os.path.basename(arg) == b"claude" for arg in args[:2]) and any(arg.endswith(needle) for arg in args):
            return pid
    return None


def select_imported(threads: list[Thread], refs: list[str]) -> list[Thread]:
    imported = [t for t in threads if t.imported_from]
    return pick_threads(imported, refs, noun="imported threads", keys=lambda t: (t.id, t.resume_session_id))


def t3_copy_path(store: ClaudeStore, thread: Thread) -> Path:
    cwd = thread.runtime_cwd or thread.worktree_path or thread.workspace_root
    return store.project_dir(cwd) / f"{thread.resume_session_id}.jsonl"


def _lines(path: Path) -> list[bytes]:
    return path.read_bytes().splitlines()


def sync_thread(
    store: ClaudeStore,
    thread: Thread,
    *,
    force: bool = False,
    dry_run: bool = False,
    now: dt.datetime | None = None,
    proc_root: Path = procs.PROC,
) -> SyncResult:
    if not thread.imported_from or not thread.resume_session_id:
        raise T3ccError(f"thread {thread.id} was not imported from Claude Code")
    original = Path(thread.imported_from)
    t3_copy = t3_copy_path(store, thread)

    def result(state: SyncState, **extra) -> SyncResult:
        return SyncResult(thread, state, original, t3_copy, **extra)

    if thread.resume_session_id != original.stem:
        return result(SyncState.FORKED)
    if t3_copy == original:
        return result(SyncState.SAME_FILE)
    if not original.is_file() or not t3_copy.is_file():
        return result(SyncState.MISSING)
    state = compare(_lines(original), _lines(t3_copy))
    wants_write = state is SyncState.FAST_FORWARD or (state is SyncState.DIVERGED and force)
    if not wants_write or dry_run:
        return result(state)
    pid = session_pid(thread.resume_session_id, proc_root)
    if pid:
        return result(state, blocked_by=pid)
    backup = backup_path(original, now)
    shutil.copy2(original, backup)
    staging = original.with_name(f"{original.name}.t3cc-tmp")
    shutil.copy2(t3_copy, staging)
    os.replace(staging, original)
    return result(state, backup=backup)
