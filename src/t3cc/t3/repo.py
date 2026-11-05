import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from t3cc.errors import T3ccError
from t3cc.t3.events import EventWriter, new_id

CLAUDE_PROVIDER = "claudeAgent"


@dataclass(frozen=True)
class Project:
    id: str
    title: str
    workspace_root: str


@dataclass(frozen=True)
class NewProject:
    """A project to create for a workspace root no T3 project covers yet."""

    workspace_root: str

    @property
    def title(self) -> str:
        return Path(self.workspace_root).name or self.workspace_root


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


def provider_thread_id(driver: str, provider_instance_id: str, native_thread_id: str) -> str:
    parts = ("provider", driver, "provider-instance", provider_instance_id, "native-thread", native_thread_id)
    return ":".join(["provider-thread", *(quote(part, safe="!*'()") for part in parts)])


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
        cursors = self.con.execute("SELECT resume_cursor_json FROM provider_session_runtime")
        native = {sid for (cursor,) in cursors if isinstance(sid := _json(cursor).get("resume"), str)}
        provider_threads = self.con.execute(
            "SELECT json_extract(payload_json, '$.nativeThreadRef.nativeId') FROM "
            "orchestration_v2_projection_provider_threads WHERE driver = ?",
            (CLAUDE_PROVIDER,),
        )
        return native | {sid for (sid,) in provider_threads if sid}

    def imported_session_ids(self) -> set[str]:
        prefix = imported_thread_id("")
        rows = self.con.execute(
            "SELECT thread_id FROM orchestration_v2_projection_threads WHERE thread_id GLOB ?", (prefix + "*",)
        )
        return {thread_id[len(prefix) :] for (thread_id,) in rows}

    def create_project(self, new: NewProject, now: str) -> Project:
        project = Project(new_id(), new.title, new.workspace_root)
        self.events.project_created(
            {
                "projectId": project.id,
                "title": project.title,
                "workspaceRoot": project.workspace_root,
                "defaultModelSelection": None,
                "faviconPath": None,
                "projectIcon": None,
                "scripts": [],
                "createdAt": now,
                "updatedAt": now,
            }
        )
        return project

    def bind_claude_session(
        self, *, thread_id: str, session_id: str, cwd: str, source: dict, last_seen_at: str
    ) -> None:
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
                last_seen_at,
                json.dumps({"threadId": thread_id, "resume": session_id}),
                json.dumps({"cwd": cwd, "importedTranscripts": [source]}),
            ),
        )

    def threads(self) -> list[Thread]:
        rows = self.con.execute(
            """SELECT t.thread_id, t.title, t.payload_json, t.updated_at, p.workspace_root,
                      pt.driver, pt.payload_json AS provider_thread_json,
                      r.provider_name, r.resume_cursor_json, r.runtime_payload_json
               FROM orchestration_v2_projection_threads t
               JOIN projection_projects p ON p.project_id = t.project_id
               LEFT JOIN orchestration_v2_projection_provider_threads pt
                 ON pt.provider_thread_id = t.active_provider_thread_id
               LEFT JOIN provider_session_runtime r ON r.thread_id = t.thread_id
               WHERE t.deleted_at IS NULL
                 AND json_extract(t.payload_json, '$.lineage.relationshipToParent') IS NOT 'subagent'
                 AND json_extract(t.payload_json, '$.lineage.sideChat') IS NOT 1
               ORDER BY t.updated_at DESC, t.thread_id"""
        ).fetchall()
        threads = []
        for r in rows:
            payload = _json(r["payload_json"])
            native_ref = _json(r["provider_thread_json"]).get("nativeThreadRef")
            native_id = native_ref.get("nativeId") if isinstance(native_ref, dict) else None
            model_selection = payload.get("modelSelection")
            runtime_payload = _json(r["runtime_payload_json"])
            threads.append(
                Thread(
                    id=r["thread_id"],
                    title=r["title"],
                    branch=payload.get("branch"),
                    worktree_path=payload.get("worktreePath"),
                    model=model_selection.get("model") if isinstance(model_selection, dict) else None,
                    updated_at=r["updated_at"],
                    workspace_root=r["workspace_root"],
                    provider=r["driver"] or r["provider_name"],
                    resume_session_id=native_id or _json(r["resume_cursor_json"]).get("resume"),
                    runtime_cwd=runtime_payload.get("cwd"),
                    imported_from=_imported_from(runtime_payload),
                )
            )
        return threads

    def thread_messages(self, thread_id: str) -> list[ThreadMessage]:
        rows = self.con.execute(
            """SELECT role, payload_json, created_at FROM orchestration_v2_projection_messages
               WHERE thread_id = ? AND streaming = 0
                 AND (role = 'assistant'
                      OR (role = 'user' AND COALESCE(json_extract(payload_json, '$.createdBy'), 'user') = 'user'))
               ORDER BY created_at, message_id""",
            (thread_id,),
        ).fetchall()
        messages = []
        for r in rows:
            payload = _json(r["payload_json"])
            attachments = payload.get("attachments")
            names = tuple(
                str(a.get("name") or a.get("type") or "file")
                for a in (attachments if isinstance(attachments, list) else [])
                if isinstance(a, dict)
            )
            messages.append(ThreadMessage(r["role"], str(payload.get("text") or ""), names, r["created_at"]))
        return messages
