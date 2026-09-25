# t3cc

Move conversation history between [Claude Code](https://claude.com/claude-code) and
[T3 Code](https://github.com/pingdotgg/t3code).

- **import**: Claude Code sessions become T3 Code threads. The next message you send in T3 resumes the
  same Claude session, so the model keeps its full context.
- **export**: T3 Code threads become Claude Code sessions you can pick up with `claude --resume`.

T3 Code imports recent sessions on its own, but only during onboarding, only from the last 30 days, at most
100 sessions and 200 messages each, and only for sessions started in a project's root. It has no export.
t3cc writes the same records T3's importer writes, without those limits, so T3 treats the result as its own
import and never imports the same session twice.

## Install

```sh
mise run install-tool    # uv tool install --editable, so edits to the repo apply immediately
```

## Usage

```sh
t3cc list-claude              # Claude Code sessions, marked t3 / imported / -
t3cc list-t3                  # T3 Code threads

t3cc import --all --dry-run   # preview
t3cc import --all             # T3 Code must be closed
t3cc import 3f8fa3a6 --project ~/Repos/app

t3cc export <thread-id>       # prints the `claude --resume` command
t3cc export --all --project ~/Repos/app

t3cc sync --all --dry-run    # which originals T3 has moved past
t3cc sync --all
```

### Import

- Sessions are matched to a T3 project by their working directory. A subdirectory or a `<repo>.worktrees/...`
  checkout goes to the enclosing project. `--project` forces a target, `--create-project` creates a missing one.
- When a session ran outside the project root in a directory that still exists, the thread runs there, on the
  session's branch. `--no-worktree` runs it from the project root instead. If the directory is gone, the
  transcript is copied under the root so `claude --resume` still finds it.
- Sessions T3 started itself, sessions already imported and sessions without a user message are skipped.
- T3 Code must be closed: it only picks up new events when it starts. Each import first writes a backup next
  to the database (`state.sqlite.t3cc-<timestamp>.bak`).
- Writing is refused on a T3 schema version t3cc was not checked against (`--allow-unknown-schema` overrides).

### Export

- A T3 thread running on Claude already has a Claude Code transcript: t3cc prints the resume command, and with
  `--to DIR` copies the transcript under DIR.
- Other threads (Codex), or any thread with `--flatten`, are rebuilt from T3's messages as a new Claude Code
  session. Only text and attachment names carry over, not tool calls.

### Sync

Continuing an imported thread in T3 normally needs no sync: T3 resumes the same Claude session from the
directory it started in, Claude Code appends to the same transcript, and `claude --resume <session>` picks up
T3's turns.

The exception is a thread T3 runs from another directory (`--no-worktree`, or a session whose directory was
gone at import). Claude Code keeps one transcript per directory, so T3's turns go to a copy and the original
stops moving. `t3cc sync` compares the two line by line:

| State          | Meaning                                                   | Action                       |
| -------------- | --------------------------------------------------------- | ---------------------------- |
| `same-file`    | T3 writes to the original                                 | none                         |
| `in-sync`      | both files are identical                                  | none                         |
| `fast-forward` | the original is a strict prefix of T3's copy              | replace the original         |
| `behind`       | the original has more (continued in Claude Code)          | none                         |
| `diverged`     | both sides changed                                        | replace only with `--force`  |
| `forked`       | T3 moved the thread to another session id                 | none, prints the new id      |

The original is backed up to `<session>.jsonl.t3cc-<timestamp>.bak` and replaced atomically. Nothing is
written while a `claude` process has the session open. The command exits 1 while a session stays diverged or
blocked.

## Caveats

- Checked against T3 Code 0.0.42 (schema migration 52). T3 validates every event at startup, so an event it
  cannot read would stop it from booting: restore the backup in that case.
- An imported thread and `claude --resume` share one transcript. Use one of them at a time per session.
- Deleting an imported worktree thread in T3 may offer to remove its worktree.

## Environment

| Variable            | Default     |
| ------------------- | ----------- |
| `CLAUDE_CONFIG_DIR` | `~/.claude` |
| `T3CODE_HOME`       | `~/.t3`     |

## Development

```sh
mise run install     # uv sync
mise run test        # fails under 100% line and branch coverage
mise run check       # ty + ruff
mise run fmt
```

## License

MIT
