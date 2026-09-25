import datetime as dt
import json
import sqlite3

import pytest

from t3cc.errors import T3ccError
from t3cc.t3 import db
from t3cc.t3.events import Event, EventWriter
from t3cc.t3.repo import NewProject, T3Repository, Thread, imported_thread_id, pick_threads


def fake_proc(tmp_path, pid, cmdline):
    proc = tmp_path / "proc"
    (proc / str(pid)).mkdir(parents=True)
    (proc / str(pid) / "cmdline").write_bytes(cmdline)
    return proc


@pytest.mark.parametrize("runtime", [None, "not json", '{"nopid": 1}', '{"pid": null}', '{"pid": 999}'])
def test_running_server_pid_absent(tmp_path, runtime):
    runtime_file = tmp_path / "runtime.json"
    if runtime is not None:
        runtime_file.write_text(runtime)
    assert db.running_server_pid(runtime_file, fake_proc(tmp_path, 1, b"x")) is None


def test_running_server_pid_checks_cmdline(tmp_path):
    runtime_file = tmp_path / "runtime.json"
    runtime_file.write_text('{"pid": 7}')
    assert db.running_server_pid(runtime_file, fake_proc(tmp_path, 7, b"/usr/lib/t3code/t3code\0")) == 7
    runtime_file.write_text('{"pid": 8}')
    assert db.running_server_pid(runtime_file, fake_proc(tmp_path / "b", 8, b"bash")) is None


def test_connect_missing_db(world):
    world.paths.t3_db.unlink()
    with pytest.raises(T3ccError, match="not found"):
        db.connect(world.paths, write=False)


def test_connect_read_only(world):
    con = db.connect(world.paths, write=False)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM projection_projects")
    con.close()


def test_connect_refuses_while_t3_runs(world, tmp_path):
    world.paths.t3_runtime.write_text('{"pid": 5}')
    with pytest.raises(T3ccError, match="pid 5"):
        db.connect(world.paths, write=True, proc_root=fake_proc(tmp_path, 5, b"t3code"))


def test_connect_schema_guard(world):
    db.connect(world.paths, write=True).close()
    with world.db() as con:
        con.execute("INSERT INTO effect_sql_migrations (migration_id, name) VALUES (53, 'Future')")
    with pytest.raises(T3ccError, match="schema version 53"):
        db.connect(world.paths, write=True)
    db.connect(world.paths, write=True, allow_unknown_schema=True).close()
    db.connect(world.paths, write=False).close()


def test_backup(world):
    world.project("/p")
    con = db.connect(world.paths, write=False)
    dest = db.backup(con, world.paths.t3_db, now=dt.datetime(2026, 1, 2, 3, 4, 5))
    con.close()
    assert dest.name == "state.sqlite.t3cc-20260102-030405.bak"
    with sqlite3.connect(dest) as copy:
        assert copy.execute("SELECT COUNT(*) FROM projection_projects").fetchone()[0] == 1
    copy.close()
    assert db.backup(world.db(), world.paths.t3_db).exists()


def test_event_writer_versions_streams_and_receipts(world):
    con = world.db()
    events = EventWriter(con)
    events.command("thread", "s", [Event("thread.created", "t0", {"x": "é"})])
    events.command(
        "thread", "s", [Event("thread.message-sent", "t1", {}), Event("thread.settled", "t2", {})], metadata={"k": 1}
    )
    rows = con.execute("SELECT * FROM orchestration_events ORDER BY sequence").fetchall()
    assert [r["stream_version"] for r in rows] == [0, 1, 2]
    assert rows[0]["actor_kind"] == "client" and rows[0]["correlation_id"] == rows[0]["command_id"]
    assert rows[1]["command_id"] == rows[2]["command_id"] != rows[0]["command_id"]
    assert json.loads(rows[0]["payload_json"]) == {"x": "é"}
    assert [json.loads(r["metadata_json"]) for r in rows] == [{}, {"k": 1}, {"k": 1}]
    receipts = con.execute("SELECT * FROM orchestration_command_receipts ORDER BY result_sequence").fetchall()
    assert [(r["command_id"], r["accepted_at"], r["result_sequence"], r["status"]) for r in receipts] == [
        (rows[0]["command_id"], "t0", rows[0]["sequence"], "accepted"),
        (rows[2]["command_id"], "t2", rows[2]["sequence"], "accepted"),
    ]


def test_repo_projects_skip_deleted(world):
    keep = world.project("/keep")
    world.project("/gone", deleted=True)
    assert [p.id for p in T3Repository(world.db()).projects()] == [keep]


def test_repo_session_sets(world):
    project = world.project("/p")
    world.thread(project, provider="claudeAgent", resume="s1")
    world.thread(project, provider="codex")
    world.thread(project, provider="claudeAgent", raw_payload="{}", resume=None)
    with world.db() as con:
        con.execute(
            "INSERT INTO provider_session_runtime (thread_id, provider_name, adapter_key, status, last_seen_at,"
            " resume_cursor_json) VALUES ('bad', 'x', 'x', 'x', 'x', 'not json'), ('list', 'x', 'x', 'x', 'x', '[1]')"
        )
    repo = T3Repository(world.db())
    assert repo.native_session_ids() == {"s1"}
    repo.events.command("thread", imported_thread_id("s2"), [Event("e", "t", {})])
    repo.events.command("project", imported_thread_id("s3"), [Event("e", "t", {})])
    assert repo.imported_session_ids() == {"s2"}


def test_repo_create_project(world):
    repo = T3Repository(world.db())
    project = repo.create_project(NewProject("/home/me/app"), "2026-01-01T00:00:00.000Z")
    assert project.title == "app"
    event = repo.con.execute("SELECT * FROM orchestration_events").fetchone()
    assert event["event_type"] == "project.created" and event["stream_id"] == project.id
    assert json.loads(event["payload_json"])["workspaceRoot"] == "/home/me/app"
    assert repo.create_project(NewProject("/"), "t").title == "/"


def test_repo_bind_claude_session_keeps_existing(world):
    repo = T3Repository(world.db())
    repo.bind_claude_session(thread_id="t", session_id="s", cwd="/c", source={"a": 1}, now="n")
    repo.bind_claude_session(thread_id="t", session_id="other", cwd="/d", source={}, now="n")
    row = repo.con.execute("SELECT * FROM provider_session_runtime").fetchone()
    assert json.loads(row["resume_cursor_json"]) == {"threadId": "t", "resume": "s"}
    assert json.loads(row["runtime_payload_json"]) == {"cwd": "/c", "importedTranscripts": [{"a": 1}]}


def test_repo_threads_and_messages(world):
    project = world.project("/p")
    newest = world.thread(
        project,
        title="new",
        updated_at="2026-09-02",
        provider="claudeAgent",
        resume="s",
        runtime_cwd="/p/sub",
        branch="b",
        worktree="/w",
    )
    world.thread(project, title="old", updated_at="2026-09-01", raw_model_json="not json")
    world.thread(project, title="gone", deleted=True)
    threads = T3Repository(world.db()).threads()
    assert [t.title for t in threads] == ["new", "old"]
    first, second = threads
    assert (first.id, first.provider, first.resume_session_id, first.runtime_cwd) == (
        newest,
        "claudeAgent",
        "s",
        "/p/sub",
    )
    assert (first.branch, first.worktree_path, first.model, first.workspace_root) == (
        "b",
        "/w",
        "claude-opus-5-5",
        "/p",
    )
    assert (second.provider, second.resume_session_id, second.model) == (None, None, None)

    thread = world.thread(
        project,
        messages=[
            ("user", "q", json.dumps([{"name": "a.png"}, {"type": "image"}, {}, "loose"])),
            ("system", "hidden"),
            ("assistant", "a", "not json"),
            ("assistant", "b", json.dumps({"name": "not a list"})),
        ],
    )
    messages = T3Repository(world.db()).thread_messages(thread)
    assert [(m.role, m.text, m.attachment_names) for m in messages] == [
        ("user", "q", ("a.png", "image", "file")),
        ("assistant", "a", ()),
        ("assistant", "b", ()),
    ]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"importedTranscripts": [{"filePath": "/c/s.jsonl"}, {"filePath": "/d/s.jsonl"}]}, "/c/s.jsonl"),
        ({"importedTranscripts": []}, None),
        ({"importedTranscripts": "nope"}, None),
        ({"importedTranscripts": ["nope"]}, None),
        ({"importedTranscripts": [{"filePath": 3}]}, None),
        ({"cwd": "/c"}, None),
    ],
)
def test_repo_threads_expose_imported_transcript(world, payload, expected):
    world.thread(world.project("/p"), provider="claudeAgent", raw_payload=json.dumps(payload))
    assert T3Repository(world.db()).threads()[0].imported_from == expected


def make_thread(thread_id, session_id=None):
    return Thread(thread_id, "T", None, None, None, "u", "/r", None, session_id, None)


def test_pick_threads_prefers_exact_ids_over_prefixes():
    threads = [make_thread("abc"), make_thread("abc-1"), make_thread("abc-2")]
    assert pick_threads(threads, []) == threads
    assert pick_threads(threads, ["abc", "abc-2"]) == [threads[0], threads[2]]
    with pytest.raises(T3ccError, match="'abc-' matches 2 threads"):
        pick_threads(threads, ["abc-"])
    with pytest.raises(T3ccError, match="'zzz' matches 0 threads"):
        pick_threads(threads, ["zzz"])


def test_pick_threads_matches_every_key():
    threads = [make_thread("t1", "s1"), make_thread("t2")]
    pick = {"noun": "sessions", "keys": lambda t: (t.id, t.resume_session_id)}
    assert pick_threads(threads, ["s1", "t2"], **pick) == threads
    assert pick_threads(threads, ["s"], **pick) == [threads[0]]
    with pytest.raises(T3ccError, match="'t' matches 2 sessions"):
        pick_threads(threads, ["t"], **pick)
