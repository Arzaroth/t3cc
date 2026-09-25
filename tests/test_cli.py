import io
import os
import runpy

import pytest

from conftest import assistant, user
from t3cc import cli
from t3cc.paths import claude_project_dir

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
