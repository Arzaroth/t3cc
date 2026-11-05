import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from t3cc.claude.store import ClaudeStore
from t3cc.paths import Paths

SCHEMA = Path(__file__).parent / "fixtures" / "t3_schema.sql"


def user(text, ts="2026-09-01T10:00:00.000Z", **extra):
    return {"type": "user", "message": {"role": "user", "content": text}, "timestamp": ts, **extra}


def assistant(text, ts="2026-09-01T10:00:01.000Z", model="claude-opus-5-5", **extra):
    content = [{"type": "text", "text": text}] if isinstance(text, str) else text
    return {
        "type": "assistant",
        "message": {"role": "assistant", "model": model, "content": content},
        "timestamp": ts,
        **extra,
    }


MR = "https://git.example.com/group/repo/-/merge_requests"


def bash(tool_id, command, output, ts="2026-09-01T10:00:02.000Z"):
    """A Bash tool call and its result, as two transcript records."""
    call = assistant([{"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": command}}], ts=ts)
    result = {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": output}]},
        "timestamp": ts,
    }
    return [call, result]


class World:
    def __init__(self, root: Path):
        self.root = root
        self.paths = Paths(claude_projects=root / "claude" / "projects", t3_home=root / "t3")
        self.connections = []
        self.paths.t3_db.parent.mkdir(parents=True)
        with sqlite3.connect(self.paths.t3_db) as con:
            con.executescript(SCHEMA.read_text())
            con.execute("INSERT INTO effect_sql_migrations (migration_id, name) VALUES (72, 'WaitingLinkRequestIndex')")
        con.close()

    def db(self):
        con = sqlite3.connect(self.paths.t3_db)
        con.row_factory = sqlite3.Row
        self.connections.append(con)
        return con

    def close(self):
        for con in self.connections:
            con.close()

    def transcript(self, cwd, records, session_id=None, *, stamp_cwd=True):
        session_id = session_id or str(uuid.uuid4())
        path = ClaudeStore(self.paths.claude_projects).session_path(cwd, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as fh:
            for record in records:
                if isinstance(record, str):
                    fh.write(record + "\n")
                    continue
                base = {"sessionId": session_id, **({"cwd": str(cwd)} if stamp_cwd else {})}
                fh.write(json.dumps({**base, **record}) + "\n")
        return path

    def project(self, root, project_id=None, title=None, deleted=False):
        project_id = project_id or str(uuid.uuid4())
        with self.db() as con:
            con.execute(
                "INSERT INTO projection_projects (project_id, title, workspace_root, scripts_json, created_at,"
                " updated_at, deleted_at) VALUES (?, ?, ?, '[]', 'x', 'x', ?)",
                (project_id, title or Path(root).name, str(root), "x" if deleted else None),
            )
        return project_id

    def thread(
        self,
        project_id,
        *,
        thread_id=None,
        title="Thread",
        branch=None,
        worktree=None,
        model="claude-opus-5-5",
        model_selection=None,
        updated_at="2026-09-01T00:00:00.000Z",
        provider=None,
        resume=None,
        runtime_cwd=None,
        native_session=None,
        messages=(),
        deleted=False,
        subagent=False,
        side_chat=False,
        raw_payload=None,
    ):
        thread_id = thread_id or str(uuid.uuid4())
        provider_thread_id = f"provider-thread:{thread_id}" if native_session else None
        payload = {
            "id": thread_id,
            "projectId": project_id,
            "title": title,
            "providerInstanceId": "claudeAgent",
            "runtimeMode": "full-access",
            "interactionMode": "default",
            "createdAt": "x",
            "archivedAt": None,
            "deletedAt": None,
            "branch": branch,
            "worktreePath": worktree,
            "modelSelection": model_selection if model_selection is not None else {"instanceId": "x", "model": model},
            "activeProviderThreadId": provider_thread_id,
            "lineage": {"relationshipToParent": "subagent" if subagent else None, "sideChat": side_chat},
        }
        with self.db() as con:
            con.execute(
                "INSERT INTO orchestration_v2_projection_threads (thread_id, project_id, title, default_provider,"
                " runtime_mode, interaction_mode, active_provider_thread_id, created_at, updated_at, deleted_at,"
                " payload_json) VALUES (?, ?, ?, 'x', 'full-access', 'default', ?, 'x', ?, ?, ?)",
                (
                    thread_id,
                    project_id,
                    title,
                    provider_thread_id,
                    updated_at,
                    "x" if deleted else None,
                    json.dumps(payload),
                ),
            )
            if native_session:
                native = {"nativeThreadRef": {"driver": "claudeAgent", "nativeId": native_session}}
                con.execute(
                    "INSERT INTO orchestration_v2_projection_provider_threads (provider_thread_id, thread_id, provider,"
                    " driver, status, updated_at, payload_json) VALUES (?, ?, 'claudeAgent', 'claudeAgent', 'idle',"
                    " 'x', ?)",
                    (provider_thread_id, thread_id, json.dumps(native)),
                )
            if provider:
                runtime = raw_payload if raw_payload is not None else json.dumps({"cwd": runtime_cwd})
                con.execute(
                    "INSERT INTO provider_session_runtime (thread_id, provider_name, adapter_key, status, last_seen_at,"
                    " resume_cursor_json, runtime_payload_json) VALUES (?, ?, ?, 'stopped', 'x', ?, ?)",
                    (thread_id, provider, provider, json.dumps({"resume": resume}) if resume else None, runtime),
                )
            for index, (role, text, *rest) in enumerate(messages):
                created_at = f"2026-09-01T00:00:{index:02d}.000Z"
                attachments = rest[0] if rest else []
                created_by = rest[1] if len(rest) > 1 else "user"
                message = {"role": role, "text": text, "attachments": attachments, "createdBy": created_by}
                con.execute(
                    "INSERT INTO orchestration_v2_projection_messages (message_id, thread_id, role, streaming,"
                    " created_at, updated_at, payload_json) VALUES (?, ?, ?, 0, ?, ?, ?)",
                    (f"{thread_id}:{index}", thread_id, role, created_at, created_at, json.dumps(message)),
                )
        return thread_id


@pytest.fixture
def world(tmp_path):
    world = World(tmp_path)
    yield world
    world.close()
