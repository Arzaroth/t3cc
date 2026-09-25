import json
from collections import Counter
from pathlib import Path

from conftest import assistant, user
from t3cc.claude import transcript
from t3cc.claude.store import ClaudeStore
from t3cc.importer import (
    DEFAULT_MODEL,
    ImportOptions,
    ImportPlan,
    Placement,
    PlannedImport,
    Skip,
    apply_import,
    classify,
    place,
    plan_import,
    resolve_project,
)
from t3cc.t3.repo import Project, T3Repository

SID = "22222222-2222-4222-8222-222222222222"
PROJECTS = [Project("p1", "repo", "/r/repo"), Project("p2", "nested", "/r/repo/nested"), Project("p3", "x", "/x/")]


def test_resolve_project_by_cwd():
    assert resolve_project(PROJECTS, "/r/repo", None, False).id == "p1"
    assert resolve_project(PROJECTS, "/r/repo/nested/deep", None, False).id == "p2"
    assert resolve_project(PROJECTS, "/x/sub", None, False).id == "p3"
    assert resolve_project(PROJECTS, "/r/repo.worktrees/feature/a", None, False).id == "p1"
    assert resolve_project(PROJECTS, "/r/repository", None, False) is None
    assert resolve_project(PROJECTS, None, None, True) is None


def test_resolve_project_creates_when_asked():
    assert resolve_project(PROJECTS, "/new/app", None, True) == Project(None, "app", "/new/app")
    assert resolve_project(PROJECTS, "/other.worktrees/b", None, False) is None


def test_resolve_project_override(tmp_path):
    assert resolve_project(PROJECTS, "/elsewhere", "p2", False).id == "p2"
    assert resolve_project(PROJECTS, None, "repo", False).id == "p1"
    assert resolve_project(PROJECTS, None, "/r/repo", False).id == "p1"
    assert resolve_project(PROJECTS, None, "nope", False) is None
    created = resolve_project(PROJECTS, None, str(tmp_path / "fresh"), True)
    assert created == Project(None, "fresh", str(tmp_path / "fresh"))


def fake_transcript(cwd, branch="feat", session_id=SID, messages=True):
    return transcript.Transcript(
        path=Path("/t.jsonl"),
        stat=None,
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
    kwargs = {"native": {"n" * 8 + SID[8:]}, "imported": {SID}, "t3_worktrees": worktrees}
    assert classify(fake_transcript("/r", session_id="not-a-uuid"), **kwargs) is Skip.EMPTY
    assert classify(fake_transcript("/r", messages=False), **kwargs) is Skip.EMPTY
    other = "33333333-3333-4333-8333-333333333333"
    assert classify(fake_transcript("/h/.t3/worktrees/a/b", session_id=other), **kwargs) is Skip.T3_NATIVE
    assert classify(fake_transcript(None, session_id=other), **kwargs) is None
    assert classify(fake_transcript("/r", session_id=other), **kwargs) is None
    kwargs["native"] = {SID}
    assert classify(fake_transcript("/r"), **kwargs) is Skip.EXISTS
    kwargs["imported"] = set()
    assert classify(fake_transcript("/r"), **kwargs) is Skip.T3_NATIVE


def test_plan_counts_and_missing():
    plan = ImportPlan(
        items=[PlannedImport(fake_transcript("/r"), PROJECTS[0], None)],
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


def test_apply_import_writes_what_t3_writes(world, tmp_path):
    project_id = world.project("/r/repo")
    worktree = tmp_path / "repo.worktrees" / "feat"
    worktree.mkdir(parents=True)
    in_root = world.transcript(
        "/r/repo",
        [
            user("q", ts="2026-09-01T10:00:00Z", gitBranch="main"),
            assistant("a", ts="2026-09-01T10:05:00Z", model="claude-fable-5"),
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

    events = [e for e in read_events(world) if e["stream_id"] == f"import:claudeAgent:{SID}"]
    assert [e["event_type"] for e in events] == [
        "thread.created",
        "thread.message-sent",
        "thread.message-sent",
        "thread.settled",
    ]
    assert all(json.loads(e["metadata_json"]) == {"historyImport": True} for e in events)
    created = json.loads(events[0]["payload_json"])
    assert created["modelSelection"] == {"instanceId": "claudeAgent", "model": "claude-fable-5"}
    assert (created["branch"], created["worktreePath"], created["createdAt"]) == (
        None,
        None,
        "2026-09-01T10:00:00.000Z",
    )
    message = json.loads(events[2]["payload_json"])
    assert message["messageId"] == f"import:claudeAgent:{SID}:000001" and message["role"] == "assistant"
    assert json.loads(events[3]["payload_json"])["settledAt"] == "2026-09-01T10:05:00.000Z"
    assert events[1]["command_id"] == events[3]["command_id"] != events[0]["command_id"]

    worktree_created = json.loads(
        next(
            e["payload_json"]
            for e in read_events(world)
            if e["stream_id"].endswith("6666") and e["stream_version"] == 0
        )
    )
    assert (worktree_created["worktreePath"], worktree_created["branch"]) == (str(worktree), "feat")
    assert worktree_created["modelSelection"]["model"] == DEFAULT_MODEL

    with world.db() as con:
        receipts = con.execute("SELECT COUNT(*) FROM orchestration_command_receipts").fetchone()[0]
        runtime = con.execute(
            "SELECT * FROM provider_session_runtime WHERE thread_id = ?", (f"import:claudeAgent:{SID}",)
        ).fetchone()
    assert receipts == 6
    assert json.loads(runtime["resume_cursor_json"]) == {"threadId": f"import:claudeAgent:{SID}", "resume": SID}
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
    assert [e["event_type"] for e in read_events(world)].count("project.created") == 1
