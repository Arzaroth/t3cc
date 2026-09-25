import json

import pytest

from conftest import user
from t3cc.claude.store import ClaudeStore
from t3cc.errors import T3ccError
from t3cc.exporter import ExportKind, export_thread, resolve_cwd, select_threads
from t3cc.t3.repo import T3Repository, Thread


def make_thread(thread_id="t1", root="/r", **kw):
    fields = {
        "title": "T",
        "branch": None,
        "worktree_path": None,
        "model": None,
        "updated_at": "u",
        "workspace_root": root,
        "provider": None,
        "resume_session_id": None,
        "runtime_cwd": None,
    } | kw
    return Thread(id=thread_id, **fields)


def test_select_threads(tmp_path):
    threads = [make_thread("abc-1", "/r"), make_thread("abc-2", str(tmp_path)), make_thread("abc", "/r")]
    assert select_threads(threads, [], None) == threads
    assert [t.id for t in select_threads(threads, [], "/r")] == ["abc-1", "abc"]
    assert [t.id for t in select_threads(threads, [], str(tmp_path))] == ["abc-2"]
    assert [t.id for t in select_threads(threads, ["abc", "abc-2"], None)] == ["abc", "abc-2"]
    with pytest.raises(T3ccError, match="matches 2"):
        select_threads(threads, ["abc-"], None)
    with pytest.raises(T3ccError, match="matches 0"):
        select_threads(threads, ["zzz"], None)


def test_resolve_cwd(tmp_path):
    exists = {"/w", "/rc"}.__contains__
    assert resolve_cwd(make_thread(), str(tmp_path), exists) == str(tmp_path)
    assert resolve_cwd(make_thread(worktree_path="/w", runtime_cwd="/rc"), None, exists) == "/w"
    assert resolve_cwd(make_thread(worktree_path="/gone", runtime_cwd="/rc"), None, exists) == "/rc"
    assert resolve_cwd(make_thread(runtime_cwd="/gone"), None, exists) == "/r"


def setup_native(world):
    project = world.project("/r")
    sid = "99999999-9999-4999-8999-999999999999"
    world.transcript("/r", [user("q")], sid)
    thread_id = world.thread(project, provider="claudeAgent", resume=sid, runtime_cwd="/r", messages=[("user", "q")])
    repo = T3Repository(world.db())
    return repo, ClaudeStore(world.paths.claude_projects), repo.threads()[0], sid, thread_id


def test_export_native_thread_in_place(world):
    repo, store, thread, sid, _ = setup_native(world)
    result = export_thread(repo, store, thread, is_dir=lambda p: True)
    assert (result.kind, result.session_id, result.cwd) == (ExportKind.NATIVE, sid, "/r")


def test_export_native_thread_elsewhere(world, tmp_path):
    repo, store, thread, sid, _ = setup_native(world)
    dry = export_thread(repo, store, thread, target=str(tmp_path), dry_run=True)
    assert dry.kind is ExportKind.COPIED and not dry.path.exists()
    done = export_thread(repo, store, thread, target=str(tmp_path))
    assert done.kind is ExportKind.COPIED and done.path.exists()
    assert export_thread(repo, store, thread, target=str(tmp_path)).kind is ExportKind.NATIVE


def test_export_flatten_synthesizes(world, tmp_path):
    repo, store, thread, sid, _ = setup_native(world)
    result = export_thread(repo, store, thread, target=str(tmp_path), flatten=True)
    assert result.kind is ExportKind.SYNTHESIZED and result.session_id != sid


def test_export_synthesizes_non_claude_threads(world, tmp_path):
    project = world.project(str(tmp_path))
    world.thread(
        project,
        provider="codex",
        title="Codex work",
        branch="dev",
        model="gpt-6",
        messages=[
            ("assistant", "hello"),
            ("user", "", json.dumps([{"name": "shot.png"}])),
            ("user", "do it"),
            ("assistant", "done"),
        ],
    )
    repo, store = T3Repository(world.db()), ClaudeStore(world.paths.claude_projects)
    thread = repo.threads()[0]
    dry = export_thread(repo, store, thread, dry_run=True)
    assert dry.kind is ExportKind.SYNTHESIZED and not dry.path.exists()
    result = export_thread(repo, store, thread)
    assert result.turns == 4 and result.cwd == str(tmp_path)
    records = [json.loads(line) for line in result.path.read_text().splitlines()]
    assert [r["type"] for r in records] == ["user", "assistant", "user", "assistant", "ai-title"]
    assert records[2]["message"]["content"] == "[attachment: shot.png]\n\ndo it"
    assert records[1]["message"]["model"] == "gpt-6" and records[0]["gitBranch"] == "dev"
    assert records[-1]["aiTitle"] == "Codex work"


def test_export_claude_thread_without_transcript_uses_default_model(world, tmp_path):
    project = world.project(str(tmp_path))
    world.thread(project, provider="claudeAgent", resume="missing", raw_model_json="{}", messages=[("user", "q")])
    repo = T3Repository(world.db())
    result = export_thread(repo, ClaudeStore(world.paths.claude_projects), repo.threads()[0])
    record = json.loads(result.path.read_text().splitlines()[0])
    assert result.kind is ExportKind.SYNTHESIZED and record["sessionId"] == result.session_id


def test_export_empty_thread(world, tmp_path):
    world.thread(world.project(str(tmp_path)), messages=[("user", "  ")])
    repo = T3Repository(world.db())
    result = export_thread(repo, ClaudeStore(world.paths.claude_projects), repo.threads()[0])
    assert result.kind is ExportKind.EMPTY and result.session_id is None
