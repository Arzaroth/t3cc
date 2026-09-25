"""Claude Code sessions -> T3 Code threads that resume the same Claude session.

Threads are written the way T3 Code's own AgentSessionImporter writes them (same thread ids, events and
provider binding), so T3 recognises them as imported and never imports them twice.
"""

import os
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from t3cc import timeutil
from t3cc.claude.store import ClaudeStore
from t3cc.claude.transcript import DEFAULT_MODEL, Transcript
from t3cc.t3.events import new_id
from t3cc.t3.repo import CLAUDE_PROVIDER, Project, T3Repository, imported_thread_id

SESSION_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
HISTORY_IMPORT = {"historyImport": True}


class Skip(StrEnum):
    EMPTY = "empty"
    T3_NATIVE = "t3-native"
    EXISTS = "exists"
    NO_PROJECT = "no-project"


@dataclass(frozen=True)
class ImportOptions:
    project: str | None = None
    create_project: bool = False
    no_worktree: bool = False


@dataclass(frozen=True)
class Placement:
    cwd: str
    worktree: str | None
    branch: str | None
    copy_transcript: bool


@dataclass(frozen=True)
class PlannedImport:
    transcript: Transcript
    project: Project
    placement: Placement

    @property
    def thread_id(self) -> str:
        return imported_thread_id(self.transcript.session_id)


@dataclass
class ImportPlan:
    items: list[PlannedImport] = field(default_factory=list)
    skipped: list[tuple[Transcript, Skip]] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        counts = {"import": len(self.items)} | {reason.value: 0 for reason in Skip}
        for _, reason in self.skipped:
            counts[reason.value] += 1
        return counts

    def missing_projects(self) -> Counter[str]:
        return Counter(t.cwd or "?" for t, reason in self.skipped if reason is Skip.NO_PROJECT)


@dataclass(frozen=True)
class ImportResult:
    item: PlannedImport
    project: Project
    copied_to: Path | None


def _within(path: str, root: str) -> bool:
    root = root.rstrip("/")
    return path == root or path.startswith(root + "/")


def resolve_project(projects: list[Project], cwd: str | None, override: str | None, create: bool) -> Project | None:
    if override:
        resolved = str(Path(override).expanduser().resolve())
        match = next(
            (p for p in projects if override in (p.id, p.title) or p.workspace_root == resolved),
            None,
        )
        return match or (Project(None, Path(resolved).name, resolved) if create else None)
    if not cwd:
        return None
    candidates = [cwd]
    worktree_parent = re.match(r"^(.*)\.worktrees/", cwd)
    if worktree_parent:
        candidates.append(worktree_parent.group(1))
    for candidate in candidates:
        matches = [p for p in projects if _within(candidate, p.workspace_root)]
        if matches:
            return max(matches, key=lambda p: len(p.workspace_root))
    return Project(None, Path(cwd).name, cwd) if create else None


def place(transcript: Transcript, project: Project, *, no_worktree: bool, is_dir: Callable[[str], bool]) -> Placement:
    root = project.workspace_root
    cwd = transcript.cwd or root
    if cwd == root:
        return Placement(root, None, None, copy_transcript=False)
    if is_dir(cwd) and not no_worktree:
        return Placement(cwd, cwd, transcript.branch, copy_transcript=False)
    return Placement(root, None, None, copy_transcript=True)


def classify(transcript: Transcript, *, native: set[str], imported: set[str], t3_worktrees: Path) -> Skip | None:
    if not SESSION_ID.match(transcript.session_id) or not transcript.has_user_message:
        return Skip.EMPTY
    if transcript.session_id in imported:
        return Skip.EXISTS
    if transcript.session_id in native or _within(transcript.cwd or "", str(t3_worktrees)):
        return Skip.T3_NATIVE
    return None


def plan_import(
    repo: T3Repository,
    transcripts: Iterable[Transcript],
    options: ImportOptions,
    *,
    t3_worktrees: Path,
    is_dir: Callable[[str], bool] = os.path.isdir,
) -> ImportPlan:
    projects = repo.projects()
    native = repo.native_session_ids()
    imported = repo.imported_session_ids()
    plan, seen = ImportPlan(), set()
    for transcript in transcripts:
        if transcript.session_id in seen:
            continue
        seen.add(transcript.session_id)
        reason = classify(transcript, native=native, imported=imported, t3_worktrees=t3_worktrees)
        if reason is None:
            project = resolve_project(projects, transcript.cwd, options.project, options.create_project)
            if project is not None:
                placement = place(transcript, project, no_worktree=options.no_worktree, is_dir=is_dir)
                plan.items.append(PlannedImport(transcript, project, placement))
                continue
            reason = Skip.NO_PROJECT
        plan.skipped.append((transcript, reason))
    return plan


def write_thread(repo: T3Repository, item: PlannedImport, project: Project, now: str) -> None:
    transcript, thread_id = item.transcript, item.thread_id
    messages = transcript.messages
    created_at = messages[0].created_at
    create_command = new_id()
    sequence = repo.events.append(
        aggregate_kind="thread",
        stream_id=thread_id,
        event_type="thread.created",
        occurred_at=created_at,
        command_id=create_command,
        payload={
            "threadId": thread_id,
            "projectId": project.id,
            "title": transcript.title,
            "modelSelection": {"instanceId": CLAUDE_PROVIDER, "model": transcript.model or DEFAULT_MODEL},
            "runtimeMode": "full-access",
            "interactionMode": "default",
            "branch": item.placement.branch,
            "worktreePath": item.placement.worktree,
            "createdAt": created_at,
            "updatedAt": created_at,
        },
        metadata=HISTORY_IMPORT,
    )
    repo.events.receipt(
        command_id=create_command,
        aggregate_kind="thread",
        aggregate_id=thread_id,
        accepted_at=created_at,
        sequence=sequence,
    )

    history_command = new_id()
    for index, message in enumerate(messages):
        repo.events.append(
            aggregate_kind="thread",
            stream_id=thread_id,
            event_type="thread.message-sent",
            occurred_at=message.created_at,
            command_id=history_command,
            payload={
                "threadId": thread_id,
                "messageId": f"{thread_id}:{index:06d}",
                "role": message.role,
                "text": message.text,
                "turnId": None,
                "streaming": False,
                "createdAt": message.created_at,
                "updatedAt": message.created_at,
            },
            metadata=HISTORY_IMPORT,
        )
    settled_at = max(m.created_at for m in messages)
    sequence = repo.events.append(
        aggregate_kind="thread",
        stream_id=thread_id,
        event_type="thread.settled",
        occurred_at=settled_at,
        command_id=history_command,
        payload={"threadId": thread_id, "settledAt": settled_at, "updatedAt": settled_at},
        metadata=HISTORY_IMPORT,
    )
    repo.events.receipt(
        command_id=history_command,
        aggregate_kind="thread",
        aggregate_id=thread_id,
        accepted_at=settled_at,
        sequence=sequence,
    )

    stat = transcript.stat
    repo.bind_claude_session(
        thread_id=thread_id,
        session_id=transcript.session_id,
        cwd=item.placement.cwd,
        source={
            "provider": CLAUDE_PROVIDER,
            "providerInstanceId": CLAUDE_PROVIDER,
            "providerSessionId": transcript.session_id,
            "filePath": str(transcript.path),
            "size": stat.st_size,
            "mtimeMs": stat.st_mtime * 1000,
            "device": stat.st_dev,
            "inode": stat.st_ino,
            "birthtimeMs": None,
        },
        now=now,
    )


def apply_import(
    repo: T3Repository, store: ClaudeStore, plan: ImportPlan, now: str | None = None
) -> list[ImportResult]:
    now = now or timeutil.now_iso()
    created: dict[str, Project] = {}
    results = []
    with repo.con:
        for item in plan.items:
            project = item.project
            if project.id is None:
                root = project.workspace_root
                project = created.get(root) or repo.create_project(root, now)
                created[root] = project
            copied = (
                store.copy_into(item.transcript.path, item.placement.cwd) if item.placement.copy_transcript else None
            )
            write_thread(repo, item, project, now)
            results.append(ImportResult(item, project, copied))
    return results
