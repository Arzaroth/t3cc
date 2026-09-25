import dataclasses
import datetime as dt
import os
from pathlib import Path

import pytest

from t3cc.claude.store import ClaudeStore
from t3cc.errors import T3ccError
from t3cc.sync import SyncState, compare, select_imported, session_pid, sync_thread, t3_copy_path
from t3cc.t3.repo import Thread

SID = "aaaaaaaa-0000-4000-8000-000000000001"
OTHER = "bbbbbbbb-0000-4000-8000-000000000002"
NOW = dt.datetime(2026, 9, 25, 12, 0, 0)

BASE = Thread(
    id=f"import:claudeAgent:{SID}",
    title="T",
    branch=None,
    worktree_path=None,
    model=None,
    updated_at="u",
    workspace_root="/root",
    provider="claudeAgent",
    resume_session_id=SID,
    runtime_cwd="/root",
    imported_from=None,
)


def write(path: Path, lines: list[bytes] | None) -> Path:
    if lines is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"".join(line + b"\n" for line in lines))
    return path


def setup(world, original, t3_copy, **thread_fields):
    store = ClaudeStore(world.paths.claude_projects)
    original_path = write(store.project_dir("/started-here") / f"{SID}.jsonl", original)
    thread = dataclasses.replace(BASE, imported_from=str(original_path), **thread_fields)
    copy_path = write(t3_copy_path(store, thread), t3_copy)
    return store, thread, original_path, copy_path


def fake_proc(tmp_path: Path, processes: dict[str, bytes | None]) -> Path:
    proc = tmp_path / "proc"
    proc.mkdir()
    for name, cmdline in processes.items():
        (proc / name).mkdir(parents=True)
        if cmdline is not None:
            (proc / name / "cmdline").write_bytes(cmdline)
    return proc


def test_compare():
    assert compare([b"a", b"b"], [b"a", b"b"]) is SyncState.IN_SYNC
    assert compare([b"a"], [b"a", b"b"]) is SyncState.FAST_FORWARD
    assert compare([], [b"a"]) is SyncState.FAST_FORWARD
    assert compare([b"a", b"b"], [b"a"]) is SyncState.BEHIND
    assert compare([b"a", b"b"], [b"a", b"c"]) is SyncState.DIVERGED


def test_session_pid(tmp_path):
    proc = fake_proc(
        tmp_path,
        {
            "self": b"x",
            str(os.getpid()): b"/usr/bin/claude\0--resume\0" + SID.encode(),
            "10": None,
            "11": b"/usr/bin/t3cc\0sync\0" + SID.encode(),
            "12": b"/usr/bin/claude\0--resume\0" + OTHER.encode(),
            "13": b"node\0/opt/claude\0--session-id=" + SID.encode(),
        },
    )
    assert session_pid(SID, proc) == 13
    assert session_pid("cccccccc-0000-4000-8000-000000000003", proc) is None


def test_select_imported():
    native = dataclasses.replace(BASE, id="native", resume_session_id=OTHER)
    first = dataclasses.replace(BASE, imported_from="/a.jsonl")
    second = dataclasses.replace(BASE, id="import:claudeAgent:x", resume_session_id=None, imported_from="/b.jsonl")
    threads = [native, first, second]
    assert select_imported(threads, []) == [first, second]
    assert select_imported(threads, [SID]) == [first]
    assert select_imported(threads, [first.id]) == [first]
    assert select_imported(threads, ["aaaa"]) == [first]
    assert select_imported(threads, ["import:claudeAgent:x"]) == [second]
    with pytest.raises(T3ccError, match="matches 2"):
        select_imported(threads, ["import:"])
    with pytest.raises(T3ccError, match="matches 0"):
        select_imported(threads, [OTHER])


def test_t3_copy_path_prefers_the_recorded_cwd():
    store = ClaudeStore(Path("/p"))
    assert t3_copy_path(store, BASE).parent == store.project_dir("/root")
    no_runtime = dataclasses.replace(BASE, runtime_cwd=None, worktree_path="/wt")
    assert t3_copy_path(store, no_runtime).parent == store.project_dir("/wt")
    assert t3_copy_path(store, dataclasses.replace(no_runtime, worktree_path=None)).parent == store.project_dir("/root")


@pytest.mark.parametrize("fields", [{"imported_from": None}, {"imported_from": "/x", "resume_session_id": None}])
def test_sync_rejects_threads_that_were_not_imported(fields):
    with pytest.raises(T3ccError, match="not imported"):
        sync_thread(ClaudeStore(Path("/p")), dataclasses.replace(BASE, **fields))


def test_sync_reports_forked_sessions(world):
    store, thread, *_ = setup(world, [b"a"], [b"a", b"b"])
    forked = dataclasses.replace(thread, resume_session_id=OTHER)
    assert sync_thread(store, forked).state is SyncState.FORKED


def test_sync_same_file(world):
    store, thread, *_ = setup(world, [b"a"], None, runtime_cwd="/started-here")
    assert sync_thread(store, thread).state is SyncState.SAME_FILE


@pytest.mark.parametrize(("original", "t3_copy"), [(None, [b"a"]), ([b"a"], None)])
def test_sync_missing_files(world, original, t3_copy):
    store, thread, *_ = setup(world, original, t3_copy)
    assert sync_thread(store, thread).state is SyncState.MISSING


@pytest.mark.parametrize(
    ("original", "t3_copy", "force", "expected"),
    [
        ([b"a", b"b"], [b"a", b"b"], True, SyncState.IN_SYNC),
        ([b"a", b"b"], [b"a"], True, SyncState.BEHIND),
        ([b"a", b"b"], [b"a", b"c"], False, SyncState.DIVERGED),
    ],
)
def test_sync_leaves_the_original_alone(world, original, t3_copy, force, expected):
    store, thread, original_path, _ = setup(world, original, t3_copy)
    before = original_path.read_bytes()
    result = sync_thread(store, thread, force=force)
    assert (result.state, result.backup) == (expected, None)
    assert original_path.read_bytes() == before


def test_sync_dry_run_writes_nothing(world):
    store, thread, original_path, _ = setup(world, [b"a"], [b"a", b"b"])
    result = sync_thread(store, thread, dry_run=True)
    assert result.state is SyncState.FAST_FORWARD and result.backup is None
    assert original_path.read_bytes() == b"a\n"


@pytest.mark.parametrize(
    ("original", "force", "expected"),
    [([b"a"], False, SyncState.FAST_FORWARD), ([b"a", b"x"], True, SyncState.DIVERGED)],
)
def test_sync_replaces_the_original_after_a_backup(world, tmp_path, original, force, expected):
    store, thread, original_path, copy_path = setup(world, original, [b"a", b"b", b"c"])
    before = original_path.read_bytes()
    result = sync_thread(store, thread, force=force, now=NOW, proc_root=fake_proc(tmp_path, {}))
    assert result.state is expected
    backup = original_path.with_name(f"{SID}.jsonl.t3cc-20260925-120000.bak")
    assert result.backup == backup
    assert backup.read_bytes() == before
    assert original_path.read_bytes() == copy_path.read_bytes()
    assert sorted(p.name for p in original_path.parent.iterdir()) == [f"{SID}.jsonl", backup.name]


def test_sync_waits_for_a_running_session(world, tmp_path):
    store, thread, original_path, _ = setup(world, [b"a"], [b"a", b"b"])
    proc = fake_proc(tmp_path, {"42": b"claude\0--resume\0" + SID.encode()})
    result = sync_thread(store, thread, proc_root=proc)
    assert (result.state, result.blocked_by, result.backup) == (SyncState.FAST_FORWARD, 42, None)
    assert original_path.read_bytes() == b"a\n"
