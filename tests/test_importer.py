import json
import os
from collections import Counter
from pathlib import Path

import pytest

from conftest import MR, assistant, bash, user
from t3cc import importer
from t3cc.claude import transcript
from t3cc.claude.store import ClaudeStore
from t3cc.claude.transcript import DEFAULT_MODEL
from t3cc.importer import (
    ImportOptions,
    ImportPlan,
    Placement,
    PlannedImport,
    Skip,
    apply_import,
    classify,
    missing_pull_requests,
    place,
    plan_import,
    resolve_project,
)
from t3cc.t3.repo import NewProject, Project, T3Repository

SID = "22222222-2222-4222-8222-222222222222"
PROJECTS = [Project("p1", "repo", "/r/repo"), Project("p2", "nested", "/r/repo/nested"), Project("p3", "x", "/x/")]


def resolved_id(cwd, override, create):
    project = resolve_project(PROJECTS, cwd, override, create)
    assert isinstance(project, Project)
    return project.id


def test_resolve_project_by_cwd():
    assert resolved_id("/r/repo", None, False) == "p1"
    assert resolved_id("/r/repo/nested/deep", None, False) == "p2"
    assert resolved_id("/x/sub", None, False) == "p3"
    assert resolved_id("/r/repo.worktrees/feature/a", None, False) == "p1"
    assert resolve_project(PROJECTS, "/r/repository", None, False) is None
    assert resolve_project(PROJECTS, None, None, True) is None


def test_resolve_project_creates_when_asked():
    assert resolve_project(PROJECTS, "/new/app", None, True) == NewProject("/new/app")
    assert resolve_project(PROJECTS, "/other.worktrees/b", None, False) is None


def test_resolve_project_override(tmp_path):
    assert resolved_id("/elsewhere", "p2", False) == "p2"
    assert resolved_id(None, "repo", False) == "p1"
    assert resolved_id(None, "/r/repo", False) == "p1"
    assert resolve_project(PROJECTS, None, "nope", False) is None
    created = resolve_project(PROJECTS, None, str(tmp_path / "fresh"), True)
    assert created == NewProject(str(tmp_path / "fresh"))


def fake_transcript(cwd, branch="feat", session_id=SID, messages=True):
    return transcript.Transcript(
        path=Path("/t.jsonl"),
        stat=os.stat(__file__),
        session_id=session_id,
        title="T",
        model=None,
        cwd=cwd,
        branch=branch,
        messages=(transcript.Message("user", "q", "t"),) if messages else (),
        updated_at="t",
    )


def test_place():
    project = Project("p", "r", "/r")
    exists, missing = (lambda p: True), (lambda p: False)
    assert place(fake_transcript("/r"), project, no_worktree=False, is_dir=exists) == Placement("/r", None, None, False)
    assert place(fake_transcript(None), project, no_worktree=False, is_dir=exists).cwd == "/r"
    assert place(fake_transcript("/r.wt/b"), project, no_worktree=False, is_dir=exists) == Placement(
        "/r.wt/b", "/r.wt/b", "feat", False
    )
    assert place(fake_transcript("/r.wt/b"), project, no_worktree=True, is_dir=exists) == Placement(
        "/r", None, None, True
    )
    assert place(fake_transcript("/gone"), project, no_worktree=False, is_dir=missing) == Placement(
        "/r", None, None, True
    )


def test_classify():
    worktrees = Path("/h/.t3/worktrees")

    def check(t, native=frozenset({"n" * 8 + SID[8:]}), imported=frozenset({SID})):
        return classify(t, native=set(native), imported=set(imported), t3_worktrees=worktrees)

    assert check(fake_transcript("/r", session_id="not-a-uuid")) is Skip.EMPTY
    assert check(fake_transcript("/r", messages=False)) is Skip.EMPTY
    other = "33333333-3333-4333-8333-333333333333"
    assert check(fake_transcript("/h/.t3/worktrees/a/b", session_id=other)) is Skip.T3_NATIVE
    assert check(fake_transcript(None, session_id=other)) is None
    assert check(fake_transcript("/r", session_id=other)) is None
    assert check(fake_transcript("/r"), native={SID}) is Skip.EXISTS
    assert check(fake_transcript("/r"), native={SID}, imported=set()) is Skip.T3_NATIVE


def test_plan_counts_and_missing():
    plan = ImportPlan(
        items=[PlannedImport(fake_transcript("/r"), PROJECTS[0], Placement("/r", None, None, False))],
        skipped=[
            (fake_transcript("/a"), Skip.NO_PROJECT),
            (fake_transcript(None), Skip.NO_PROJECT),
            (fake_transcript("/a"), Skip.EMPTY),
        ],
    )
    assert plan.counts() == {"import": 1, "empty": 1, "t3-native": 0, "exists": 0, "no-project": 2}
    assert plan.missing_projects() == Counter({"/a": 1, "?": 1})
    assert plan.items[0].thread_id == f"import:claudeAgent:{SID}"


def test_plan_import(world):
    world.project("/r/repo")
    good = world.transcript("/r/repo", [user("q"), assistant("a")], SID)
    duplicate = world.transcript("/r/copy", [user("q")], SID)
    orphan = world.transcript("/nowhere", [user("q")], "44444444-4444-4444-8444-444444444444")
    empty = world.transcript("/r/repo", [assistant("a")], "55555555-5555-4555-8555-555555555555")
    repo = T3Repository(world.db())
    plan = plan_import(
        repo,
        [transcript.parse(p) for p in (good, duplicate, orphan, empty)],
        ImportOptions(),
        t3_worktrees=world.paths.t3_worktrees,
    )
    assert [i.transcript.session_id for i in plan.items] == [SID]
    assert [(t.session_id[:4], r) for t, r in plan.skipped] == [("4444", Skip.NO_PROJECT), ("5555", Skip.EMPTY)]


def read_events(world):
    with world.db() as con:
        return con.execute("SELECT * FROM orchestration_events ORDER BY sequence").fetchall()


def one(world, sql, *args):
    with world.db() as con:
        return con.execute(sql, args).fetchone()


def test_apply_import_writes_what_t3_writes(world, tmp_path):
    project_id = world.project("/r/repo")
    worktree = tmp_path / "repo.worktrees" / "feat"
    worktree.mkdir(parents=True)
    in_root = world.transcript(
        "/r/repo",
        [
            user("question", ts="2026-09-01T10:00:00Z", gitBranch="main"),
            assistant("answer", ts="2026-09-01T10:05:00Z", model="claude-fable-5"),
        ],
        SID,
    )
    in_worktree = world.transcript(str(worktree), [user("w", gitBranch="feat")], "66666666-6666-4666-8666-666666666666")
    gone = world.transcript("/r/repo/deleted", [user("g")], "77777777-7777-4777-8777-777777777777")
    store = ClaudeStore(world.paths.claude_projects)
    repo = T3Repository(world.db())
    plan = plan_import(
        repo,
        [transcript.parse(p) for p in (in_root, in_worktree, gone)],
        ImportOptions(project="/r/repo"),
        t3_worktrees=world.paths.t3_worktrees,
    )
    results = apply_import(repo, store, plan, now="2026-09-25T00:00:00.000Z")
    assert [r.project.id for r in results] == [project_id] * 3
    assert [r.copied_to for r in results] == [None, None, store.project_dir("/r/repo") / gone.name]

    thread_id = f"import:claudeAgent:{SID}"
    provider_thread_id = f"provider-thread:provider:claudeAgent:provider-instance:claudeAgent:native-thread:{SID}"
    prefix = "agent-session-import:v2"
    events = [e for e in read_events(world) if e["stream_id"] == thread_id]
    assert [(e["event_id"], e["event_type"], e["stream_version"]) for e in events] == [
        (f"{prefix}:thread:{thread_id}:created", "thread.created", 0),
        (f"{prefix}:message:{thread_id}:000000", "message.updated", 1),
        (f"{prefix}:turn-item:{thread_id}:000000", "turn-item.updated", 2),
        (f"{prefix}:message:{thread_id}:000001", "message.updated", 3),
        (f"{prefix}:turn-item:{thread_id}:000001", "turn-item.updated", 4),
        (f"{prefix}:provider-thread:{provider_thread_id}", "provider-thread.updated", 5),
    ]
    assert {(e["application_event_version"], e["actor_kind"], e["command_id"]) for e in events} == {(2, "server", None)}
    assert [json.loads(e["metadata_json"]) for e in (events[0], events[1], events[5])] == [
        {"providerInstanceId": "claudeAgent"},
        {},
        {"driver": "claudeAgent", "providerInstanceId": "claudeAgent"},
    ]
    user_item, assistant_item = (json.loads(e["payload_json"]) for e in (events[2], events[4]))
    assert (user_item["type"], user_item["ordinal"], user_item["createdBy"]) == ("user_message", 1, "user")
    assert (assistant_item["type"], assistant_item["messageId"]) == ("assistant_message", f"{thread_id}:000001")

    thread = json.loads(one(world, "SELECT payload_json FROM orchestration_v2_projection_threads")[0])
    assert thread["modelSelection"] == {"instanceId": "claudeAgent", "model": "claude-fable-5"}
    assert (thread["branch"], thread["worktreePath"], thread["historyOrigin"]) == (None, None, "v1_import")
    assert (thread["activeProviderThreadId"], thread["settledOverride"]) == (provider_thread_id, "settled")
    assert (thread["createdAt"], thread["settledAt"]) == ("2026-09-01T10:00:00.000Z", "2026-09-01T10:05:00.000Z")
    provider_thread = one(
        world, "SELECT * FROM orchestration_v2_projection_provider_threads WHERE thread_id = ?", thread_id
    )
    assert json.loads(provider_thread["payload_json"])["nativeThreadRef"]["nativeId"] == SID
    positions = one(
        world, "SELECT group_concat(ordinal) FROM orchestration_v2_turn_item_positions WHERE thread_id = ?", thread_id
    )
    assert positions[0] == "1,2"
    search = "SELECT COUNT(*) FROM orchestration_v2_message_search WHERE orchestration_v2_message_search MATCH ?"
    assert one(world, search, "answer")[0] == 1
    metadata = one(world, "SELECT * FROM orchestration_v2_projection_metadata")
    assert metadata["last_sequence"] == read_events(world)[-1]["sequence"] and metadata["schema_version"] == 2
    assert one(world, "SELECT COUNT(*) FROM orchestration_command_receipts")[0] == 0

    worktree_thread = next(t for t in repo.threads() if t.id.endswith("6666"))
    assert (worktree_thread.worktree_path, worktree_thread.branch, worktree_thread.model) == (
        str(worktree),
        "feat",
        DEFAULT_MODEL,
    )
    imported = next(t for t in repo.threads() if t.id == thread_id)
    assert (imported.provider, imported.resume_session_id, imported.imported_from) == ("claudeAgent", SID, str(in_root))
    assert repo.imported_session_ids() == {SID, "66666666-6666-4666-8666-666666666666", gone.stem}

    runtime = one(world, "SELECT * FROM provider_session_runtime WHERE thread_id = ?", thread_id)
    assert json.loads(runtime["resume_cursor_json"]) == {"threadId": thread_id, "resume": SID}
    assert runtime["last_seen_at"] == "2026-09-01T10:05:00.000Z"
    payload = json.loads(runtime["runtime_payload_json"])
    assert payload["cwd"] == "/r/repo"
    assert payload["importedTranscripts"][0]["filePath"] == str(in_root)


def test_apply_import_creates_each_project_once(world):
    first = world.transcript("/new/app", [user("a")], SID)
    second = world.transcript("/new/app", [user("b")], "88888888-8888-4888-8888-888888888888")
    repo = T3Repository(world.db())
    plan = plan_import(
        repo,
        [transcript.parse(first), transcript.parse(second)],
        ImportOptions(create_project=True),
        t3_worktrees=world.paths.t3_worktrees,
    )
    results = apply_import(repo, ClaudeStore(world.paths.claude_projects), plan)
    assert results[0].project.id == results[1].project.id is not None
    project = [e for e in read_events(world) if e["event_type"] == "project.created"]
    assert len(project) == 1 and project[0]["stream_id"] == results[0].project.id
    assert one(world, "SELECT workspace_root FROM projection_projects")[0] == "/new/app"


def test_apply_import_commits_each_thread_on_its_own(world, monkeypatch):
    world.project("/r/repo")
    first = world.transcript("/r/repo", [user("a")], SID)
    second = world.transcript("/r/repo", [user("b")], "88888888-8888-4888-8888-888888888888")
    repo = T3Repository(world.db())
    plan = plan_import(
        repo,
        [transcript.parse(first), transcript.parse(second)],
        ImportOptions(),
        t3_worktrees=world.paths.t3_worktrees,
    )
    write = importer.write_thread
    calls = []

    def fail_second(repo, item, project):
        calls.append(item)
        if len(calls) == 2:
            raise RuntimeError("boom")
        write(repo, item, project)

    monkeypatch.setattr(importer, "write_thread", fail_second)
    with pytest.raises(RuntimeError):
        apply_import(repo, ClaudeStore(world.paths.claude_projects), plan)
    assert T3Repository(world.db()).imported_session_ids() == {SID}


def test_apply_import_links_created_pull_requests_and_leaves_the_thread_active(world):
    world.project("/r/repo")
    world.transcript(
        "/r/repo", [user("open it"), *bash("t", "glab mr create --fill", f"{MR}/7", ts="2026-09-01T10:00:09Z")], SID
    )
    repo = T3Repository(world.db())
    store = ClaudeStore(world.paths.claude_projects)
    plan = plan_import(
        repo, [transcript.parse(store.find(SID))], ImportOptions(), t3_worktrees=world.paths.t3_worktrees
    )
    apply_import(repo, store, plan)
    thread = json.loads(one(world, "SELECT payload_json FROM orchestration_v2_projection_threads")[0])
    assert thread["pullRequests"] == [
        {
            "host": "git.example.com",
            "repository": "group/repo",
            "number": 7,
            "url": f"{MR}/7",
            "source": "created",
            "linkedAt": "2026-09-01T10:00:09.000Z",
            "snapshot": None,
            "stack": None,
        }
    ]
    assert (thread["settledOverride"], thread["settledAt"]) == (None, None)


def test_missing_pull_requests(world):
    project = world.project("/r")
    source = world.transcript("/r", [user("q"), *bash("a", "glab mr create && glab mr create", f"{MR}/1\n{MR}/2")], SID)
    payload = json.dumps({"importedTranscripts": [{"filePath": str(source)}]})
    gone = json.dumps({"importedTranscripts": [{"filePath": str(source.with_name("gone.jsonl"))}]})
    world.thread(project, thread_id="linked", provider="claudeAgent", raw_payload=payload)
    world.thread(project, thread_id="moved", provider="claudeAgent", raw_payload=gone)
    world.thread(project, thread_id="native", provider="claudeAgent")
    repo = T3Repository(world.db())
    threads = {t.id: t for t in repo.threads()}
    assert [pr.number for pr in missing_pull_requests(repo, threads["linked"])] == [1, 2]
    repo.link_pull_requests("linked", [{"host": "GIT.example.com", "repository": "group/repo", "number": 1}], "n")
    assert [pr.number for pr in missing_pull_requests(repo, threads["linked"])] == [2]
    assert missing_pull_requests(repo, threads["moved"]) == []
    assert missing_pull_requests(repo, threads["native"]) == []
