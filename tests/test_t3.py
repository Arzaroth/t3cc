import datetime as dt
import json
import sqlite3

import pytest

from t3cc.errors import T3ccError
from t3cc.t3 import db
from t3cc.t3.events import EventWriter, ThreadEvent
from t3cc.t3.repo import NewProject, T3Repository, Thread, imported_thread_id, pick_threads, provider_thread_id


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
    with pytest.raises(T3ccError, match="statev2.sqlite not found$"):
        db.connect(world.paths, write=False)
    world.paths.t3_legacy_db.touch()
    with pytest.raises(T3ccError, match="older than 0.1 needs t3cc 0.1"):
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
        con.execute("INSERT INTO effect_sql_migrations (migration_id, name) VALUES (73, 'Future')")
    with pytest.raises(T3ccError, match="schema version 73"):
        db.connect(world.paths, write=True)
    db.connect(world.paths, write=True, allow_unknown_schema=True).close()
    db.connect(world.paths, write=False).close()


def test_backup(world):
    world.project("/p")
    con = db.connect(world.paths, write=False)
    dest = db.backup(con, world.paths.t3_db, now=dt.datetime(2026, 1, 2, 3, 4, 5))
    con.close()
    assert dest.name == "statev2.sqlite.t3cc-20260102-030405.bak"
    with sqlite3.connect(dest) as copy:
        assert copy.execute("SELECT COUNT(*) FROM projection_projects").fetchone()[0] == 1
    copy.close()
    assert db.backup(world.db(), world.paths.t3_db).exists()


def thread_created(thread_id, updated_at="t0"):
    return {
        "id": thread_id,
        "projectId": "p",
        "title": "é",
        "providerInstanceId": "claudeAgent",
        "runtimeMode": "full-access",
        "interactionMode": "default",
        "activeProviderThreadId": None,
        "createdAt": "t0",
        "updatedAt": updated_at,
        "archivedAt": None,
        "deletedAt": None,
    }


def provider_thread(app_thread_id):
    return {
        "id": f"pt-{app_thread_id}",
        "appThreadId": app_thread_id,
        "ownerNodeId": None,
        "providerInstanceId": "claudeAgent",
        "driver": "claudeAgent",
        "providerSessionId": None,
        "status": "idle",
        "firstRunOrdinal": None,
        "lastRunOrdinal": None,
        "updatedAt": "t9",
    }


def test_event_writer_appends_and_projects(world):
    con = world.db()
    events = EventWriter(con)
    events.thread_events(
        "s", [ThreadEvent("e0", "thread.created", "t0", thread_created("s"), provider_instance_id="c")]
    )
    events.thread_events("s", [ThreadEvent("e1", "thread.created", "t1", thread_created("s", "t1"))], now="n")
    events.thread_events("s", [ThreadEvent("e2", "provider-thread.updated", "t9", provider_thread("s"), driver="d")])
    events.thread_events("x", [ThreadEvent("e3", "provider-thread.updated", "t9", provider_thread("x"))])
    rows = con.execute("SELECT * FROM orchestration_events ORDER BY sequence").fetchall()
    assert [(r["stream_id"], r["stream_version"]) for r in rows] == [("s", 0), ("s", 1), ("s", 2), ("x", 0)]
    assert [json.loads(r["metadata_json"]) for r in rows] == [{"providerInstanceId": "c"}, {}, {"driver": "d"}, {}]
    assert rows[0]["payload_json"].startswith('{"id":"s","projectId":"p","title":"é"')
    thread = con.execute("SELECT * FROM orchestration_v2_projection_threads").fetchone()
    assert (thread["active_provider_thread_id"], thread["updated_at"]) == ("pt-s", "t9")
    assert json.loads(thread["payload_json"])["activeProviderThreadId"] == "pt-s"
    owners = con.execute("SELECT thread_id FROM orchestration_v2_projection_provider_threads ORDER BY 1").fetchall()
    assert [r[0] for r in owners] == ["s", "x"]
    metadata = con.execute("SELECT last_sequence, schema_version FROM orchestration_v2_projection_metadata").fetchone()
    assert tuple(metadata) == (rows[-1]["sequence"], 2)
    with pytest.raises(ValueError, match="no projection for thread.settled"):
        events.thread_events("s", [ThreadEvent("e4", "thread.settled", "t", {})])


def test_repo_projects_skip_deleted(world):
    keep = world.project("/keep")
    world.project("/gone", deleted=True)
    assert [p.id for p in T3Repository(world.db()).projects()] == [keep]


def test_repo_session_sets(world):
    project = world.project("/p")
    world.thread(project, provider="claudeAgent", resume="s1")
    world.thread(project, provider="codex")
    world.thread(project, provider="claudeAgent", raw_payload="{}", resume=None)
    world.thread(project, native_session="s4")
    world.thread(project, thread_id=imported_thread_id("s2"))
    world.thread(project, thread_id="import:codex:s3")
    with world.db() as con:
        con.execute(
            "INSERT INTO provider_session_runtime (thread_id, provider_name, adapter_key, status, last_seen_at,"
            " resume_cursor_json) VALUES ('bad', 'x', 'x', 'x', 'x', 'not json'), ('list', 'x', 'x', 'x', 'x', '[1]')"
        )
        con.execute(
            "INSERT INTO orchestration_v2_projection_provider_threads (provider_thread_id, provider, driver, status,"
            " updated_at, payload_json) VALUES ('codex', 'codex', 'codex', 'idle', 'x', ?), ('none', 'claudeAgent',"
            " 'claudeAgent', 'idle', 'x', '{}')",
            (json.dumps({"nativeThreadRef": {"nativeId": "c1"}}),),
        )
    repo = T3Repository(world.db())
    assert repo.native_session_ids() == {"s1", "s4"}
    assert repo.imported_session_ids() == {"s2"}


def test_provider_thread_id_matches_t3_derivation():
    assert provider_thread_id("claudeAgent", "claudeAgent", "abc-1") == (
        "provider-thread:provider:claudeAgent:provider-instance:claudeAgent:native-thread:abc-1"
    )
    assert provider_thread_id("d", "a b", "x:y/z!*'()~") == (
        "provider-thread:provider:d:provider-instance:a%20b:native-thread:x%3Ay%2Fz!*'()~"
    )


def test_repo_create_project(world):
    repo = T3Repository(world.db())
    project = repo.create_project(NewProject("/home/me/app"), "2026-01-01T00:00:00.000Z")
    assert project.title == "app"
    event = repo.con.execute("SELECT * FROM orchestration_events").fetchone()
    assert (event["event_type"], event["stream_id"], event["application_event_version"]) == (
        "project.created",
        project.id,
        2,
    )
    assert event["event_id"].startswith(f"event:command:{event['command_id']}:")
    assert (event["actor_kind"], event["correlation_id"]) == ("client", event["command_id"])
    assert json.loads(event["payload_json"])["workspaceRoot"] == "/home/me/app"
    receipt = repo.con.execute("SELECT * FROM orchestration_command_receipts").fetchone()
    assert (receipt["command_id"], receipt["aggregate_id"], receipt["result_sequence"], receipt["command_type"]) == (
        event["command_id"],
        project.id,
        event["sequence"],
        "project.create",
    )
    assert [p.workspace_root for p in repo.projects()] == ["/home/me/app"]
    assert repo.create_project(NewProject("/"), "t").title == "/"


def test_repo_bind_claude_session_keeps_existing(world):
    repo = T3Repository(world.db())
    repo.bind_claude_session(thread_id="t", session_id="s", cwd="/c", source={"a": 1}, last_seen_at="n")
    repo.bind_claude_session(thread_id="t", session_id="other", cwd="/d", source={}, last_seen_at="m")
    row = repo.con.execute("SELECT * FROM provider_session_runtime").fetchone()
    assert json.loads(row["resume_cursor_json"]) == {"threadId": "t", "resume": "s"}
    assert json.loads(row["runtime_payload_json"]) == {"cwd": "/c", "importedTranscripts": [{"a": 1}]}
    assert row["last_seen_at"] == "n"


def test_repo_threads_and_messages(world):
    project = world.project("/p")
    newest = world.thread(
        project,
        title="new",
        updated_at="2026-09-02",
        provider="claudeAgent",
        resume="legacy",
        native_session="s",
        runtime_cwd="/p/sub",
        branch="b",
        worktree="/w",
    )
    world.thread(project, title="old", updated_at="2026-09-01", model_selection="not a dict")
    world.thread(project, title="legacy", updated_at="2026-08-01", provider="claudeAgent", resume="r")
    world.thread(project, title="gone", deleted=True)
    world.thread(project, title="child", subagent=True)
    world.thread(project, title="side", side_chat=True)
    threads = T3Repository(world.db()).threads()
    assert [t.title for t in threads] == ["new", "old", "legacy"]
    first, second, third = threads
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
    assert (third.provider, third.resume_session_id) == ("claudeAgent", "r")

    thread = world.thread(
        project,
        messages=[
            ("user", "q", [{"name": "a.png"}, {"type": "image"}, {}, "loose"]),
            ("system", "hidden"),
            ("assistant", None, "not a list"),
            ("assistant", "b", {"name": "not a list"}),
            ("user", "Background task completed.", [], "agent"),
        ],
    )
    messages = T3Repository(world.db()).thread_messages(thread)
    assert [(m.role, m.text, m.attachment_names) for m in messages] == [
        ("user", "q", ("a.png", "image", "file")),
        ("assistant", "", ()),
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


def test_repo_reads_legacy_messages_until_t3_imports_the_transcript(world):
    thread = world.thread(world.project("/p"), messages=[("user", "preview only")])
    with world.db() as con:
        con.execute(
            "INSERT INTO orchestration_v2_legacy_imports (thread_id, source_updated_at, shell_imported_at)"
            " VALUES (?, 'x', 'x')",
            (thread,),
        )
        con.executemany(
            "INSERT INTO projection_thread_messages (message_id, thread_id, role, text, is_streaming, created_at,"
            " updated_at, attachments_json) VALUES (?, ?, ?, ?, 0, ?, 'x', ?)",
            [
                ("m0", thread, "user", "first", "t0", json.dumps([{"name": "a.png"}])),
                ("m1", thread, "system", "hidden", "t1", None),
                ("m2", thread, "assistant", "second", "t2", "not json"),
                ("m3", thread, "assistant", "third", "t3", json.dumps({"name": "not a list"})),
            ],
        )
    repo = T3Repository(world.db())
    assert [(m.role, m.text, m.attachment_names) for m in repo.thread_messages(thread)] == [
        ("user", "first", ("a.png",)),
        ("assistant", "second", ()),
        ("assistant", "third", ()),
    ]
    with world.db() as con:
        con.execute("UPDATE orchestration_v2_legacy_imports SET transcript_imported_at = 'x'")
    assert [m.text for m in repo.thread_messages(thread)] == ["preview only"]
