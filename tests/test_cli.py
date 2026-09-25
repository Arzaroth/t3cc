import io
import json
import os
import runpy
from pathlib import Path

import pytest

from conftest import assistant, user
from t3cc import cli
from t3cc.claude.store import ClaudeStore
from t3cc.paths import claude_project_dir
from t3cc.sync import SyncResult, SyncState
from t3cc.t3.repo import Thread

SID = "12345678-1234-4234-8234-123456789012"
OTHER = "abcdef01-1234-4234-8234-123456789012"


def run(world, *argv):
    out = io.StringIO()
    code = cli.main(list(argv), paths=world.paths, out=out)
    return code, out.getvalue()


def test_main_defaults_to_env_paths(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("T3CODE_HOME", str(tmp_path / "nothing"))
    assert cli.main(["list-t3"]) == 1
    assert "not found" in capsys.readouterr().err


def test_list_claude(world):
    project = world.project("/r")
    world.thread(project, provider="claudeAgent", resume=SID)
    world.transcript("/r", [user("native")], SID)
    world.transcript("/r", [{"type": "mode"}], "00000000-0000-4000-8000-000000000000")
    world.transcript("/r", [user("mine"), assistant("ok")], OTHER)
    code, out = run(world, "list-claude")
    assert code == 0
    assert f"{SID}" in out and "t3 " in out
    assert f"{OTHER}" in out and "    2 msgs  /r  'mine'" in out
    assert "00000000" not in out
    assert run(world, "list-claude", "-n", "1")[1].count("msgs") == 1


def test_list_claude_marks_imported(world):
    world.project("/r")
    world.transcript("/r", [user("x")], SID)
    run(world, "import", SID)
    assert "imported" in run(world, "list-claude")[1]


def test_list_t3(world):
    project = world.project("/r")
    world.transcript("/r", [user("x")], SID)
    world.thread(project, thread_id="claude-thread", provider="claudeAgent", resume=SID)
    world.thread(project, thread_id="codex-thread", provider="codex")
    world.thread(project, thread_id="bare-thread")
    lines = run(world, "list-t3")[1].splitlines()
    kinds = {line.split()[0]: line.split()[2] for line in lines}
    assert kinds == {"claude-thread": "claude", "codex-thread": "codex", "bare-thread": "-"}
    assert run(world, "list-t3", "--project", "/elsewhere")[1] == ""


def test_import_requires_sources(world, capsys):
    assert run(world, "import")[0] == 1
    assert "--all" in capsys.readouterr().err


def test_import_single_sessions(world):
    world.project("/r")
    world.transcript("/r", [user("hello"), assistant("hi")], SID)
    world.transcript("/nowhere", [user("x")], OTHER)
    code, out = run(world, "import", SID[:8], OTHER, "--dry-run")
    assert code == 0
    assert f"would import {SID} -> r: 'hello' [2 msgs]" in out
    assert f"skip {OTHER}: no-project (/nowhere)" in out
    assert not list(world.paths.t3_db.parent.glob("*.bak"))

    code, out = run(world, "import", SID)
    assert f"imported {SID} -> r" in out and "backup: " in out and "import: 1" in out
    assert len(list(world.paths.t3_db.parent.glob("*.bak"))) == 1
    assert f"skip {SID}: exists" in run(world, "import", SID)[1]


def test_import_all_reports_missing_projects(world, tmp_path):
    world.project("/r")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    world.transcript("/r", [user("old")], SID)
    os.utime(world.paths.claude_projects / "-r" / f"{SID}.jsonl", (0, 0))
    world.transcript(str(worktree), [user("in worktree")], OTHER)
    world.transcript("/nowhere", [user("x")], "fedcba98-1234-4234-8234-123456789012")
    code, out = run(world, "import", "--all", "--project", "/r", "--dry-run")
    assert f"worktree={worktree}" in out and "import: 3" in out
    code, out = run(world, "import", "--all", "--since", "2000-01-01")
    assert "no T3 project for these directories" in out
    assert "    1  /nowhere" in out and f"    1  {worktree}" in out
    assert "import: 0" in out and "exists: 0" in out
    code, out = run(world, "import", "--all", "--project", "/r")
    assert "import: 3" in out and "backup: " in out


def test_import_copies_transcript_when_cwd_is_gone(world):
    world.project("/r")
    world.transcript("/r/deleted", [user("x")], SID)
    out = run(world, "import", SID, "--no-worktree")[1]
    assert "(transcript copied to -r)" in out


def test_import_nothing_to_do(world):
    world.transcript("/r", [assistant("only")], SID)
    code, out = run(world, "import", SID)
    assert code == 0 and "import: 0" in out and "backup" not in out


def test_import_refuses_unknown_schema(world, capsys):
    world.project("/r")
    world.transcript("/r", [user("x")], SID)
    with world.db() as con:
        con.execute("INSERT INTO effect_sql_migrations (migration_id, name) VALUES (60, 'Future')")
    assert run(world, "import", SID)[0] == 1
    assert "--allow-unknown-schema" in capsys.readouterr().err
    assert run(world, "import", SID, "--allow-unknown-schema")[0] == 0


def test_export(world, tmp_path, capsys):
    assert run(world, "export")[0] == 1
    assert "--all" in capsys.readouterr().err
    assert run(world, "export", "--all")[0] == 1
    assert "no threads selected" in capsys.readouterr().err

    project = world.project(str(tmp_path))
    world.transcript(str(tmp_path), [user("x")], SID)
    world.thread(project, thread_id="native", title="Native", provider="claudeAgent", resume=SID)
    world.thread(project, thread_id="codex", title="Codex", provider="codex", messages=[("user", "q")])
    world.thread(project, thread_id="empty", title="Empty")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    out = run(world, "export", "--all")[1]
    assert f"already a Claude Code session: cd {tmp_path} && claude --resume {SID}   # 'Native'" in out
    assert "wrote 1 turns" in out and "empty                                 skipped: no text messages" in out

    out = run(world, "export", "native", "codex", "--to", str(elsewhere), "--dry-run")[1]
    assert "would copy to" in out and "would write 1 turns" in out
    out = run(world, "export", "native", "--to", str(elsewhere))[1]
    assert f"  copied to {claude_project_dir(world.paths.claude_projects, elsewhere).name}: cd {elsewhere}" in out


def test_version(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert capsys.readouterr().out.startswith("t3cc ")


def test_module_entry_point(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["t3cc", "--version"])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("t3cc", run_name="__main__")
    assert exit_info.value.code == 0
    assert "t3cc" in capsys.readouterr().out


def imported_session(world, original: list[str], t3_copy: list[str] | None) -> Path:
    store = ClaudeStore(world.paths.claude_projects)
    original_path = store.project_dir("/r/deleted") / f"{SID}.jsonl"
    original_path.parent.mkdir(parents=True)
    original_path.write_text("".join(line + "\n" for line in original))
    if t3_copy is not None:
        copy_path = store.project_dir("/r") / f"{SID}.jsonl"
        copy_path.parent.mkdir(parents=True)
        copy_path.write_text("".join(line + "\n" for line in t3_copy))
    payload = {"cwd": "/r", "importedTranscripts": [{"filePath": str(original_path)}]}
    project = world.project("/r")
    world.thread(
        project,
        thread_id=f"import:claudeAgent:{SID}",
        provider="claudeAgent",
        resume=SID,
        raw_payload=json.dumps(payload),
    )
    world.thread(project, thread_id="native", provider="claudeAgent", resume=OTHER)
    return original_path


def test_sync_requires_targets(world, capsys):
    assert run(world, "sync")[0] == 1
    assert "--all" in capsys.readouterr().err


def test_sync_fast_forwards_the_original(world):
    original = imported_session(world, ["a"], ["a", "b"])
    assert run(world, "sync", "--all", "--dry-run") == (0, f"{SID}  would fast-forward\n")
    assert original.read_text() == "a\n"
    code, out = run(world, "sync", SID[:8])
    assert code == 0 and f"{SID}  fast-forward done, backup: {SID}.jsonl.t3cc-" in out
    assert original.read_text() == "a\nb\n"
    assert run(world, "sync", "--all") == (0, f"{SID}  in sync\n")


def test_sync_diverged_needs_force(world):
    original = imported_session(world, ["a", "x"], ["a", "b"])
    code, out = run(world, "sync", "--all")
    assert code == 1 and "diverged: both sides changed" in out
    assert run(world, "sync", "--all", "--force", "--dry-run") == (0, f"{SID}  would overwrite diverged original\n")
    assert original.read_text() == "a\nx\n"
    code, out = run(world, "sync", "--all", "--force")
    assert code == 0 and "overwrite diverged original done" in out
    assert original.read_text() == "a\nb\n"


THREAD = Thread(
    id="t",
    title="T",
    branch=None,
    worktree_path=None,
    model=None,
    updated_at="u",
    workspace_root="/r",
    provider="claudeAgent",
    resume_session_id=OTHER,
    runtime_cwd="/r",
    imported_from="/o.jsonl",
)


@pytest.mark.parametrize(
    ("state", "extra", "expected"),
    [
        (SyncState.SAME_FILE, {}, "in sync: T3 writes to the original transcript"),
        (SyncState.FORKED, {}, f"T3 moved to another session, resume it with: claude --resume {OTHER}"),
        (SyncState.MISSING, {}, "missing transcript: /nope/original.jsonl, /nope/copy.jsonl"),
        (SyncState.BEHIND, {}, "original is ahead of T3's copy (continued in Claude Code), left alone"),
        (SyncState.FAST_FORWARD, {"blocked_by": 7}, "fast-forward blocked: claude (pid 7) has the session open"),
    ],
)
def test_sync_messages(state, extra, expected):
    result = SyncResult(THREAD, state, Path("/nope/original.jsonl"), Path("/nope/copy.jsonl"), **extra)
    assert cli._sync_message(result, force=False) == expected


def test_sync_reports_blocked_sessions_as_unresolved(world, monkeypatch):
    imported_session(world, ["a"], ["a", "b"])
    monkeypatch.setattr(
        "t3cc.sync.sync_thread",
        lambda store, thread, **kw: SyncResult(thread, SyncState.FAST_FORWARD, Path("/o"), Path("/c"), blocked_by=9),
    )
    assert run(world, "sync", "--all") == (1, f"{SID}  fast-forward blocked: claude (pid 9) has the session open\n")
