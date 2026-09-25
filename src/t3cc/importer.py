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
from t3cc.t3.events import Event
from t3cc.t3.repo import CLAUDE_PROVIDER, NewProject, Project, T3Repository, imported_thread_id

SESSION_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
WORKTREES_PARENT = re.compile(r"^(.*)\.worktrees/")
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
    project: Project | NewProject
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


def resolve_project(
    projects: list[Project], cwd: str | None, override: str | None, create: bool
) -> Project | NewProject | None:
    if override:
        root = str(Path(override).expanduser().resolve())
        found = next((p for p in projects if override in (p.id, p.title) or p.workspace_root == root), None)
    elif cwd:
        root, found = cwd, _enclosing_project(projects, cwd)
    else:
        return None
    return found or (NewProject(root) if create else None)


def _enclosing_project(projects: list[Project], cwd: str) -> Project | None:
    candidates = [cwd]
    if worktrees := WORKTREES_PARENT.match(cwd):
        candidates.append(worktrees.group(1))
    for candidate in candidates:
        matches = [p for p in projects if Path(candidate).is_relative_to(p.workspace_root)]
        if matches:
            return max(matches, key=lambda p: len(p.workspace_root))
    return None


def place(
    transcript: Transcript, project: Project | NewProject, *, no_worktree: bool, is_dir: Callable[[str], bool]
) -> Placement:
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
    if transcript.session_id in native or (transcript.cwd and Path(transcript.cwd).is_relative_to(t3_worktrees)):
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
        project = None if reason else resolve_project(projects, transcript.cwd, options.project, options.create_project)
        if project is None:
            plan.skipped.append((transcript, reason or Skip.NO_PROJECT))
        else:
            placement = place(transcript, project, no_worktree=options.no_worktree, is_dir=is_dir)
            plan.items.append(PlannedImport(transcript, project, placement))
    return plan


def write_thread(repo: T3Repository, item: PlannedImport, project: Project, now: str) -> None:
    transcript, thread_id = item.transcript, item.thread_id
    messages = transcript.messages
    created_at = messages[0].created_at
    settled_at = max(m.created_at for m in messages)
    created = {
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
    }
    history = [
        Event(
            "thread.message-sent",
            message.created_at,
            {
                "threadId": thread_id,
                "messageId": f"{thread_id}:{index:06d}",
                "role": message.role,
                "text": message.text,
                "turnId": None,
                "streaming": False,
                "createdAt": message.created_at,
                "updatedAt": message.created_at,
            },
        )
        for index, message in enumerate(messages)
    ]
    settled = {"threadId": thread_id, "settledAt": settled_at, "updatedAt": settled_at}
    repo.events.command("thread", thread_id, [Event("thread.created", created_at, created)], metadata=HISTORY_IMPORT)
    repo.events.command(
        "thread", thread_id, [*history, Event("thread.settled", settled_at, settled)], metadata=HISTORY_IMPORT
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
    created: dict[NewProject, Project] = {}
    results = []
    with repo.con:
        for item in plan.items:
            project = item.project
            if isinstance(project, NewProject):
                if project not in created:
                    created[project] = repo.create_project(project, now)
                project = created[project]
            copied = (
                store.copy_into(item.transcript.path, item.placement.cwd) if item.placement.copy_transcript else None
            )
            write_thread(repo, item, project, now)
            results.append(ImportResult(item, project, copied))
    return results
