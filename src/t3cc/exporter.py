"""T3 Code threads -> Claude Code sessions resumable with `claude --resume`."""

import os
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from t3cc.claude.store import ClaudeStore
from t3cc.claude.synth import Turn, build_session, merge_turns
from t3cc.claude.transcript import DEFAULT_MODEL
from t3cc.t3.repo import CLAUDE_PROVIDER, T3Repository, Thread, pick_threads


class ExportKind(StrEnum):
    NATIVE = "native"
    COPIED = "copied"
    SYNTHESIZED = "synthesized"
    EMPTY = "empty"


@dataclass(frozen=True)
class ExportResult:
    thread: Thread
    kind: ExportKind
    cwd: str
    session_id: str | None = None
    path: Path | None = None
    turns: int = 0


def select_threads(threads: list[Thread], refs: list[str], project: str | None) -> list[Thread]:
    if project:
        resolved = str(Path(project).expanduser().resolve())
        threads = [t for t in threads if t.workspace_root in (project, resolved)]
    return pick_threads(threads, refs)


def resolve_cwd(thread: Thread, target: str | None, is_dir: Callable[[str], bool]) -> str:
    if target:
        return str(Path(target).expanduser().resolve())
    for candidate in (thread.worktree_path, thread.runtime_cwd):
        if candidate and is_dir(candidate):
            return candidate
    return thread.workspace_root


def thread_turns(repo: T3Repository, thread: Thread) -> list[Turn]:
    turns = []
    for message in repo.thread_messages(thread.id):
        text = "\n".join([message.text.strip(), *(f"[attachment: {name}]" for name in message.attachment_names)])
        turns.append(Turn(message.role, text, message.created_at))
    return merge_turns(turns)


def export_thread(
    repo: T3Repository,
    store: ClaudeStore,
    thread: Thread,
    *,
    target: str | None = None,
    flatten: bool = False,
    dry_run: bool = False,
    is_dir: Callable[[str], bool] = os.path.isdir,
) -> ExportResult:
    cwd = resolve_cwd(thread, target, is_dir)
    session_id = thread.resume_session_id if thread.provider == CLAUDE_PROVIDER else None
    native = store.by_session_id(session_id) if session_id else None
    if native and not flatten:
        dest = store.project_dir(cwd) / native.name
        if dest.exists():
            return ExportResult(thread, ExportKind.NATIVE, cwd, session_id, dest)
        if not dry_run:
            store.copy_into(native, cwd)
        return ExportResult(thread, ExportKind.COPIED, cwd, session_id, dest)

    turns = thread_turns(repo, thread)
    if not turns:
        return ExportResult(thread, ExportKind.EMPTY, cwd)
    new_session, records = build_session(
        turns, cwd=cwd, branch=thread.branch, model=thread.model or DEFAULT_MODEL, title=thread.title
    )
    path = store.session_path(cwd, new_session)
    if not dry_run:
        path = store.write_session(cwd, new_session, records)
    return ExportResult(thread, ExportKind.SYNTHESIZED, cwd, new_session, path, len(turns))
