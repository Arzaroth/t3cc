import argparse
import datetime as dt
import sys
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from typing import TextIO

from t3cc import __version__, exporter, importer, sync
from t3cc.claude import transcript as claude_transcript
from t3cc.claude.store import ClaudeStore
from t3cc.errors import T3ccError
from t3cc.exporter import ExportKind
from t3cc.paths import Paths
from t3cc.sync import SyncState
from t3cc.t3 import db
from t3cc.t3.repo import T3Repository, Thread


def _summary(counts: dict[str, int]) -> str:
    return ", ".join(f"{key}: {value}" for key, value in counts.items())


def cmd_list_claude(args: argparse.Namespace, paths: Paths, out: TextIO) -> int:
    with closing(db.connect(paths, write=False)) as con:
        repo = T3Repository(con)
        native, imported = repo.native_session_ids(), repo.imported_session_ids()
    shown = 0
    for path in ClaudeStore(paths.claude_projects).all_transcripts():
        if shown >= args.limit:
            break
        t = claude_transcript.parse(path)
        if not t.messages:
            continue
        shown += 1
        state = "imported" if t.session_id in imported else "t3" if t.session_id in native else "-"
        print(
            f"{t.session_id}  {t.updated_at[:16]}  {state:8}  {len(t.messages):5} msgs  "
            f"{t.cwd or '?'}  {t.title[:60]!r}",
            file=out,
        )
    return 0


def cmd_list_t3(args: argparse.Namespace, paths: Paths, out: TextIO) -> int:
    with closing(db.connect(paths, write=False)) as con:
        threads = exporter.select_threads(T3Repository(con).threads(), [], args.project)
    store = ClaudeStore(paths.claude_projects)
    for thread in threads[: args.limit]:
        kind = "claude" if exporter.native_transcript(store, thread) else thread.provider or "-"
        print(f"{thread.id:<60}  {thread.updated_at[:16]}  {kind:12}  {thread.title[:60]!r}", file=out)
    return 0


def _import_sources(args: argparse.Namespace, store: ClaudeStore) -> list[Path]:
    if args.all:
        paths = store.all_transcripts()
        if args.since:
            cutoff = dt.datetime.fromisoformat(args.since).timestamp()
            paths = [p for p in paths if p.stat().st_mtime >= cutoff]
        return paths
    if not args.sessions:
        raise T3ccError("give session ids/paths, or --all")
    return [store.find(ref) for ref in args.sessions]


def cmd_import(args: argparse.Namespace, paths: Paths, out: TextIO) -> int:
    store = ClaudeStore(paths.claude_projects)
    sources = _import_sources(args, store)
    with closing(db.connect(paths, write=not args.dry_run, allow_unknown_schema=args.allow_unknown_schema)) as con:
        repo = T3Repository(con)
        options = importer.ImportOptions(args.project, args.create_project, args.no_worktree)
        plan = importer.plan_import(
            repo, (claude_transcript.parse(p) for p in sources), options, t3_worktrees=paths.t3_worktrees
        )
        if args.all:
            _print_missing_projects(plan, out)
        else:
            for t, reason in plan.skipped:
                print(f"skip {t.session_id}: {reason.value} ({t.cwd or '?'})", file=out)
        if args.dry_run or not plan.items:
            for item in plan.items:
                print(f"would import {_describe(item)}", file=out)
            print(_summary(plan.counts()), file=out)
            return 0
        print(f"backup: {db.backup(con, paths.t3_db)}", file=out)
        for result in importer.apply_import(repo, store, plan):
            copied = f" (transcript copied to {result.copied_to.parent.name})" if result.copied_to else ""
            print(f"imported {_describe(result.item)}{copied}", file=out)
    print(_summary(plan.counts()), file=out)
    print("Start T3 Code: it picks the new threads up on startup.", file=out)
    return 0


def _print_missing_projects(plan: importer.ImportPlan, out: TextIO) -> None:
    missing = plan.missing_projects()
    if missing:
        print("no T3 project for these directories (sessions):", file=out)
        for cwd, count in missing.most_common(15):
            print(f"  {count:5}  {cwd}", file=out)


def _describe(item: importer.PlannedImport) -> str:
    t = item.transcript
    worktree = f" worktree={item.placement.worktree}" if item.placement.worktree else ""
    return f"{t.session_id} -> {item.project.title}: {t.title[:60]!r} [{len(t.messages)} msgs]{worktree}"


def cmd_export(args: argparse.Namespace, paths: Paths, out: TextIO) -> int:
    if not args.threads and not args.all:
        raise T3ccError("give thread ids (see `t3cc list-t3`), or --all")
    store = ClaudeStore(paths.claude_projects)
    with closing(db.connect(paths, write=False)) as con:
        repo = T3Repository(con)
        threads = exporter.select_threads(repo.threads(), args.threads, args.project)
        if not threads:
            raise T3ccError("no threads selected")
        for thread in threads:
            result = exporter.export_thread(
                repo, store, thread, target=args.to, flatten=args.flatten, dry_run=args.dry_run
            )
            print(_export_line(thread, result, args.dry_run), file=out)
    return 0


def cmd_sync(args: argparse.Namespace, paths: Paths, out: TextIO) -> int:
    if not args.threads and not args.all:
        raise T3ccError("give imported thread or session ids, or --all")
    store = ClaudeStore(paths.claude_projects)
    with closing(db.connect(paths, write=False)) as con:
        threads = sync.select_imported(T3Repository(con).threads(), args.threads)
    unresolved = False
    for thread in threads:
        result = sync.sync_thread(store, thread, force=args.force, dry_run=args.dry_run)
        unresolved |= result.unresolved
        print(f"{thread.resume_session_id}  {_sync_message(result)}", file=out)
    return 1 if unresolved else 0


def _sync_message(result: sync.SyncResult) -> str:
    state = result.state
    if state is SyncState.SAME_FILE:
        return "in sync: T3 writes to the original transcript"
    if state is SyncState.IN_SYNC:
        return "in sync"
    if state is SyncState.FORKED:
        return f"T3 moved to another session, resume it with: claude --resume {result.thread.resume_session_id}"
    if state is SyncState.MISSING:
        missing = [str(p) for p in (result.original, result.t3_copy) if not p.is_file()]
        return f"missing transcript: {', '.join(missing)}"
    if state is SyncState.BEHIND:
        return "original is ahead of T3's copy (continued in Claude Code), left alone"
    if state is SyncState.DIVERGED and not result.replaces:
        return "diverged: both sides changed. --force takes T3's version (the original is backed up)"
    action = "fast-forward" if state is SyncState.FAST_FORWARD else "overwrite diverged original"
    if result.blocked_by:
        return f"{action} blocked: claude (pid {result.blocked_by}) has the session open"
    if result.backup:
        return f"{action} done, backup: {result.backup.name}"
    return f"would {action}"


def _export_line(thread: Thread, result: exporter.ExportResult | None, dry_run: bool) -> str:
    head = f"{thread.id[:36]:<36}"
    if result is None:
        return f"{head}  skipped: no text messages"
    resume = f"cd {result.cwd} && claude --resume {result.session_id}   # {thread.title[:50]!r}"
    if result.kind is ExportKind.NATIVE:
        return f"{head}  already a Claude Code session: {resume}"
    if result.kind is ExportKind.COPIED:
        verb = "would copy" if dry_run else "copied"
        return f"{head}  {verb} to {result.path.parent.name}: {resume}"
    verb = "would write" if dry_run else "wrote"
    return f"{head}  {verb} {result.turns} turns: {resume}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="t3cc", description="Move conversation history between Claude Code and T3 Code."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list-claude", help="list Claude Code sessions and their T3 status")
    p.add_argument("-n", "--limit", type=int, default=30)
    p.set_defaults(func=cmd_list_claude)

    p = sub.add_parser("list-t3", help="list T3 Code threads")
    p.add_argument("-n", "--limit", type=int, default=30)
    p.add_argument("--project", help="workspace root to filter on")
    p.set_defaults(func=cmd_list_t3)

    p = sub.add_parser("import", help="Claude Code sessions -> T3 Code threads (T3 must be closed)")
    p.add_argument("sessions", nargs="*", help="session ids, id prefixes or .jsonl paths")
    p.add_argument("--all", action="store_true", help="every Claude Code session not already in T3")
    p.add_argument("--since", help="with --all: only sessions modified since this ISO date")
    p.add_argument("--project", help="target T3 project (id, title or workspace root) instead of matching by cwd")
    p.add_argument("--create-project", action="store_true", help="create the T3 project when none matches")
    p.add_argument("--no-worktree", action="store_true", help="run imported threads from the project root")
    p.add_argument("--allow-unknown-schema", action="store_true", help="write into an untested T3 schema version")
    p.add_argument("-n", "--dry-run", action="store_true")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("export", help="T3 Code threads -> resumable Claude Code sessions")
    p.add_argument("threads", nargs="*", help="thread ids or prefixes")
    p.add_argument("--all", action="store_true")
    p.add_argument("--project", help="only threads of this workspace root")
    p.add_argument("--to", help="directory to resume from (default: the thread's worktree or project root)")
    p.add_argument(
        "--flatten", action="store_true", help="rebuild from T3 messages even when a Claude transcript exists"
    )
    p.add_argument("-n", "--dry-run", action="store_true")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("sync", help="update imported sessions' original transcripts with what T3 added")
    p.add_argument("threads", nargs="*", help="imported thread ids, session ids or prefixes")
    p.add_argument("--all", action="store_true", help="every imported thread")
    p.add_argument("--force", action="store_true", help="overwrite a diverged original (it is backed up first)")
    p.add_argument("-n", "--dry-run", action="store_true")
    p.set_defaults(func=cmd_sync)
    return parser


def main(argv: Sequence[str] | None = None, paths: Paths | None = None, out: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args, paths or Paths.from_env(), out or sys.stdout)
    except T3ccError as error:
        print(f"t3cc: {error}", file=sys.stderr)
        return 1
