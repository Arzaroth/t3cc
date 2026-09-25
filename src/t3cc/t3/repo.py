import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from t3cc.errors import T3ccError
from t3cc.t3.events import EventWriter, new_id

CLAUDE_PROVIDER = "claudeAgent"


@dataclass(frozen=True)
class Project:
    id: str | None
    title: str
    workspace_root: str


@dataclass(frozen=True)
class Thread:
    id: str
    title: str
    branch: str | None
    worktree_path: str | None
    model: str | None
    updated_at: str
    workspace_root: str
    provider: str | None
    resume_session_id: str | None
    runtime_cwd: str | None
    imported_from: str | None = None


@dataclass(frozen=True)
class ThreadMessage:
    role: str
    text: str
    attachment_names: tuple[str, ...]
    created_at: str


def _json(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _imported_from(runtime_payload: dict) -> str | None:
    transcripts = runtime_payload.get("importedTranscripts")
    if not isinstance(transcripts, list) or not transcripts or not isinstance(transcripts[0], dict):
        return None
    path = transcripts[0].get("filePath")
    return path if isinstance(path, str) else None


def pick_threads(
    threads: list[Thread],
    refs: list[str],
    *,
    noun: str = "threads",
    keys: Callable[[Thread], tuple[str | None, ...]] = lambda t: (t.id,),
) -> list[Thread]:
    """Every thread when refs is empty, else the one thread each ref names exactly or by prefix."""
    if not refs:
        return threads
    selected = []
    for ref in refs:
        hits = [t for t in threads if ref in keys(t)] or [
            t for t in threads if any(key and key.startswith(ref) for key in keys(t))
        ]
        if len(hits) != 1:
            raise T3ccError(f"'{ref}' matches {len(hits)} {noun}")
        selected.append(hits[0])
    return selected


def imported_thread_id(session_id: str) -> str:
    return f"import:{CLAUDE_PROVIDER}:{session_id}"


class T3Repository:
    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self.events = EventWriter(con)

    def projects(self) -> list[Project]:
        rows = self.con.execute(
            "SELECT project_id, title, workspace_root FROM projection_projects WHERE deleted_at IS NULL"
        ).fetchall()
        return [Project(r["project_id"], r["title"], r["workspace_root"]) for r in rows]

    def native_session_ids(self) -> set[str]:
        """Claude sessions T3 Code started itself, whatever its thread ids look like."""
        rows = self.con.execute("SELECT resume_cursor_json FROM provider_session_runtime")
        return {sid for (cursor,) in rows if isinstance(sid := _json(cursor).get("resume"), str)}

    def imported_session_ids(self) -> set[str]:
        prefix = imported_thread_id("")
        rows = self.con.execute(
            "SELECT DISTINCT stream_id FROM orchestration_events WHERE aggregate_kind = 'thread' AND stream_id GLOB ?",
            (prefix + "*",),
        )
        return {stream_id[len(prefix) :] for (stream_id,) in rows}

    def create_project(self, workspace_root: str, now: str) -> Project:
        project_id, command_id = new_id(), new_id()
        title = Path(workspace_root).name or workspace_root
        payload = {
            "projectId": project_id,
            "title": title,
            "workspaceRoot": workspace_root,
            "defaultModelSelection": None,
            "scripts": [],
            "createdAt": now,
            "updatedAt": now,
        }
        sequence = self.events.append(
            aggregate_kind="project",
            stream_id=project_id,
            event_type="project.created",
            occurred_at=now,
            command_id=command_id,
            payload=payload,
        )
        self.events.receipt(
            command_id=command_id, aggregate_kind="project", aggregate_id=project_id, accepted_at=now, sequence=sequence
        )
        return Project(project_id, title, workspace_root)

    def bind_claude_session(self, *, thread_id: str, session_id: str, cwd: str, source: dict, now: str) -> None:
        self.con.execute(
            """INSERT INTO provider_session_runtime (thread_id, provider_name, provider_instance_id, adapter_key,
                 runtime_mode, status, last_seen_at, resume_cursor_json, runtime_payload_json)
               VALUES (?, ?, ?, ?, 'full-access', 'stopped', ?, ?, ?)
               ON CONFLICT (thread_id) DO NOTHING""",
            (
                thread_id,
                CLAUDE_PROVIDER,
                CLAUDE_PROVIDER,
                CLAUDE_PROVIDER,
                now,
                json.dumps({"threadId": thread_id, "resume": session_id}),
                json.dumps({"cwd": cwd, "importedTranscripts": [source]}),
            ),
        )

    def threads(self) -> list[Thread]:
        rows = self.con.execute(
            """SELECT t.thread_id, t.title, t.branch, t.worktree_path, t.model_selection_json, t.updated_at,
                      p.workspace_root, r.provider_name, r.resume_cursor_json, r.runtime_payload_json
               FROM projection_threads t
               JOIN projection_projects p ON p.project_id = t.project_id
               LEFT JOIN provider_session_runtime r ON r.thread_id = t.thread_id
               WHERE t.deleted_at IS NULL
               ORDER BY t.updated_at DESC, t.thread_id"""
        ).fetchall()
        threads = []
        for r in rows:
            runtime_payload = _json(r["runtime_payload_json"])
            threads.append(
                Thread(
                    id=r["thread_id"],
                    title=r["title"],
                    branch=r["branch"],
                    worktree_path=r["worktree_path"],
                    model=_json(r["model_selection_json"]).get("model"),
                    updated_at=r["updated_at"],
                    workspace_root=r["workspace_root"],
                    provider=r["provider_name"],
                    resume_session_id=_json(r["resume_cursor_json"]).get("resume"),
                    runtime_cwd=runtime_payload.get("cwd"),
                    imported_from=_imported_from(runtime_payload),
                )
            )
        return threads

    def thread_messages(self, thread_id: str) -> list[ThreadMessage]:
        rows = self.con.execute(
            """SELECT role, text, attachments_json, created_at FROM projection_thread_messages
               WHERE thread_id = ? AND role IN ('user', 'assistant')
               ORDER BY created_at, message_id""",
            (thread_id,),
        ).fetchall()
        messages = []
        for r in rows:
            try:
                attachments = json.loads(r["attachments_json"] or "[]")
            except ValueError:
                attachments = []
            names = tuple(str(a.get("name") or a.get("type") or "file") for a in attachments if isinstance(a, dict))
            messages.append(ThreadMessage(r["role"], r["text"], names, r["created_at"]))
        return messages
