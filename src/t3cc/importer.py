"""Claude Code sessions -> T3 Code threads that resume the same Claude session.

Threads are written the way T3 Code's own AgentSessionImporter writes them (same thread ids, events, projections
and provider binding), so T3 recognises them as imported and never imports them twice.
"""

import os
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from t3cc import timeutil
from t3cc.claude.pull_requests import CreatedPullRequest
from t3cc.claude.store import ClaudeStore
from t3cc.claude.transcript import DEFAULT_MODEL, Message, Transcript, parse
from t3cc.t3.events import ThreadEvent
from t3cc.t3.repo import (
    CLAUDE_PROVIDER,
    NewProject,
    Project,
    T3Repository,
    Thread,
    imported_thread_id,
    provider_thread_id,
)

SESSION_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
WORKTREES_PARENT = re.compile(r"^(.*)\.worktrees/")
IMPORT_EVENT_PREFIX = "agent-session-import:v2"


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


def _message_events(thread_id: str, index: int, message: Message) -> list[ThreadEvent]:
    suffix = f"{index:06d}"
    message_id = f"{thread_id}:{suffix}"
    turn_item_id = f"{IMPORT_EVENT_PREFIX}:turn-item:{thread_id}:{suffix}"
    at = message.created_at
    turn_item: dict = {
        "id": turn_item_id,
        "threadId": thread_id,
        "runId": None,
        "nodeId": None,
        "providerThreadId": None,
        "providerTurnId": None,
        "nativeItemRef": None,
        "parentItemId": None,
        "ordinal": index + 1,
        "status": "completed",
        "title": None,
        "startedAt": at,
        "completedAt": at,
        "updatedAt": at,
    }
    if message.role == "user":
        turn_item |= {
            "createdBy": "user",
            "creationSource": "server",
            "type": "user_message",
            "messageId": message_id,
            "inputIntent": "turn_start",
            "text": message.text,
            "attachments": [],
        }
    else:
        turn_item |= {"type": "assistant_message", "messageId": message_id, "text": message.text, "streaming": False}
    conversation_message = {
        "createdBy": "user" if message.role == "user" else "agent",
        "creationSource": "server",
        "id": message_id,
        "threadId": thread_id,
        "runId": None,
        "nodeId": None,
        "role": message.role,
        "text": message.text,
        "attachments": [],
        "streaming": False,
        "createdAt": at,
        "updatedAt": at,
    }
    return [
        ThreadEvent(f"{IMPORT_EVENT_PREFIX}:message:{thread_id}:{suffix}", "message.updated", at, conversation_message),
        ThreadEvent(turn_item_id, "turn-item.updated", at, turn_item),
    ]


def pull_request_link(pr: CreatedPullRequest) -> dict:
    return {
        "host": pr.host,
        "repository": pr.repository,
        "number": pr.number,
        "url": pr.url,
        "source": "created",
        "linkedAt": pr.created_at,
        "snapshot": None,
        "stack": None,
    }


def write_thread(repo: T3Repository, item: PlannedImport, project: Project) -> None:
    transcript, thread_id = item.transcript, item.thread_id
    messages = transcript.messages
    created_at = messages[0].created_at
    updated_at = max(m.created_at for m in messages)
    session_id = transcript.session_id
    provider_thread = provider_thread_id(CLAUDE_PROVIDER, CLAUDE_PROVIDER, session_id)
    links = [pull_request_link(pr) for pr in transcript.pull_requests]
    app_thread = {
        "createdBy": "system",
        "creationSource": "server",
        "id": thread_id,
        "projectId": project.id,
        "title": transcript.title.strip() or "Untitled thread",
        "providerInstanceId": CLAUDE_PROVIDER,
        "modelSelection": {"instanceId": CLAUDE_PROVIDER, "model": transcript.model or DEFAULT_MODEL},
        "runtimeMode": "full-access",
        "interactionMode": "default",
        "branch": item.placement.branch or None,
        "worktreePath": item.placement.worktree,
        "linkedPullRequest": None,
        "branchPullRequest": None,
        **({"pullRequests": links} if links else {}),
        "activeProviderThreadId": provider_thread,
        "historyOrigin": "v1_import",
        "lineage": {"parentThreadId": None, "relationshipToParent": None, "rootThreadId": thread_id},
        "forkedFrom": None,
        "createdAt": created_at,
        "updatedAt": updated_at,
        "archivedAt": None,
        "settledOverride": None if links else "settled",
        "settledAt": None if links else updated_at,
        "unsettledAt": None,
        "snoozedUntil": None,
        "snoozedAt": None,
        "pinnedAt": None,
        "pinOrderKey": None,
        "activeOrderKey": None,
        "lastVisitedAt": None,
        "deletedAt": None,
    }
    native_thread = {
        "id": provider_thread,
        "driver": CLAUDE_PROVIDER,
        "providerInstanceId": CLAUDE_PROVIDER,
        "providerSessionId": None,
        "appThreadId": thread_id,
        "ownerNodeId": None,
        "nativeThreadRef": {"driver": CLAUDE_PROVIDER, "nativeId": session_id, "strength": "strong"},
        "nativeConversationHeadRef": None,
        "status": "idle",
        "firstRunOrdinal": None,
        "lastRunOrdinal": None,
        "handoffIds": [],
        "forkedFrom": None,
        "pendingBackgroundTasks": [],
        "createdAt": created_at,
        "updatedAt": updated_at,
    }
    events = [
        ThreadEvent(
            f"{IMPORT_EVENT_PREFIX}:thread:{thread_id}:created",
            "thread.created",
            created_at,
            app_thread,
            provider_instance_id=CLAUDE_PROVIDER,
        ),
        *(event for index, message in enumerate(messages) for event in _message_events(thread_id, index, message)),
        ThreadEvent(
            f"{IMPORT_EVENT_PREFIX}:provider-thread:{provider_thread}",
            "provider-thread.updated",
            updated_at,
            native_thread,
            driver=CLAUDE_PROVIDER,
            provider_instance_id=CLAUDE_PROVIDER,
        ),
    ]
    repo.events.thread_events(thread_id, events)

    stat = transcript.stat
    repo.bind_claude_session(
        thread_id=thread_id,
        session_id=session_id,
        cwd=item.placement.cwd,
        source={
            "provider": CLAUDE_PROVIDER,
            "providerInstanceId": CLAUDE_PROVIDER,
            "providerSessionId": session_id,
            "filePath": str(transcript.path),
            "size": stat.st_size,
            "mtimeMs": stat.st_mtime * 1000,
            "device": stat.st_dev,
            "inode": stat.st_ino,
            "birthtimeMs": None,
        },
        last_seen_at=updated_at,
    )


def missing_pull_requests(repo: T3Repository, thread: Thread) -> list[CreatedPullRequest]:
    source = Path(thread.imported_from or "")
    if not thread.imported_from or not source.is_file():
        return []
    linked = repo.pull_request_keys(thread.id)
    return [pr for pr in parse(source).pull_requests if pr.key.lower() not in linked]


def apply_import(
    repo: T3Repository, store: ClaudeStore, plan: ImportPlan, now: str | None = None
) -> list[ImportResult]:
    now = now or timeutil.now_iso()
    created: dict[NewProject, Project] = {}
    results = []
    for item in plan.items:
        with repo.con:
            project = item.project
            if isinstance(project, NewProject):
                if project not in created:
                    created[project] = repo.create_project(project, now)
                project = created[project]
            copied = (
                store.copy_into(item.transcript.path, item.placement.cwd) if item.placement.copy_transcript else None
            )
            write_thread(repo, item, project)
        results.append(ImportResult(item, project, copied))
    return results
