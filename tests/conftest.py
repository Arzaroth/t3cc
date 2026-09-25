import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from t3cc.paths import Paths, claude_project_dir

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


class World:
    def __init__(self, root: Path):
        self.root = root
        self.paths = Paths(claude_projects=root / "claude" / "projects", t3_home=root / "t3")
        self.connections = []
        self.paths.t3_db.parent.mkdir(parents=True)
        with sqlite3.connect(self.paths.t3_db) as con:
            con.executescript(SCHEMA.read_text())
            con.execute(
                "INSERT INTO effect_sql_migrations (migration_id, name) VALUES (52, 'ProjectionThreadTitleState')"
            )
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
        path = claude_project_dir(self.paths.claude_projects, cwd) / f"{session_id}.jsonl"
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
        updated_at="2026-09-01T00:00:00.000Z",
        provider=None,
        resume=None,
        runtime_cwd=None,
        messages=(),
        deleted=False,
        raw_model_json=None,
        raw_payload=None,
    ):
        thread_id = thread_id or str(uuid.uuid4())
        model_json = raw_model_json if raw_model_json is not None else json.dumps({"instanceId": "x", "model": model})
        with self.db() as con:
            con.execute(
                "INSERT INTO projection_threads (thread_id, project_id, title, branch, worktree_path, created_at,"
                " updated_at, deleted_at, model_selection_json) VALUES (?, ?, ?, ?, ?, 'x', ?, ?, ?)",
                (thread_id, project_id, title, branch, worktree, updated_at, "x" if deleted else None, model_json),
            )
            if provider:
                payload = raw_payload if raw_payload is not None else json.dumps({"cwd": runtime_cwd})
                con.execute(
                    "INSERT INTO provider_session_runtime (thread_id, provider_name, adapter_key, status, last_seen_at,"
                    " resume_cursor_json, runtime_payload_json) VALUES (?, ?, ?, 'stopped', 'x', ?, ?)",
                    (thread_id, provider, provider, json.dumps({"resume": resume}) if resume else None, payload),
                )
            for index, (role, text, *rest) in enumerate(messages):
                attachments = rest[0] if rest else None
                con.execute(
                    "INSERT INTO projection_thread_messages (message_id, thread_id, role, text, is_streaming,"
                    " created_at, updated_at, attachments_json) VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
                    (
                        f"{thread_id}:{index}",
                        thread_id,
                        role,
                        text,
                        f"2026-09-01T00:00:{index:02d}.000Z",
                        "x",
                        attachments,
                    ),
                )
        return thread_id


@pytest.fixture
def world(tmp_path):
    world = World(tmp_path)
    yield world
    world.close()
