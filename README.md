# t3cc

Move conversation history between [Claude Code](https://claude.com/claude-code) and
[T3 Code](https://github.com/pingdotgg/t3code).

- **import**: Claude Code sessions become T3 Code threads. The next message you send in T3 resumes the
  same Claude session, so the model keeps its full context.
- **export**: T3 Code threads become Claude Code sessions you can pick up with `claude --resume`.
- **send**: a local Claude Code session moves into a T3 Code environment the desktop app is connected to, on
  the same branch in a worktree there, and becomes a thread of the matching project.

T3 Code imports recent sessions on its own, but only during onboarding, only from the last 30 days, at most
100 sessions and 200 messages each, and only for sessions started in a project's root. It has no export.
t3cc writes the same records T3's importer writes, without those limits, so T3 treats the result as its own
import and never imports the same session twice.

## Install

```bash
uv tool install git+https://github.com/Arzaroth/t3cc@v0.3.0
```

Or, from a clone:

```bash
mise run install-tool    # uv tool install --editable, so edits to the repo apply immediately
```

## Usage

```sh
t3cc list-claude              # Claude Code sessions, marked t3 / imported / -
t3cc list-t3                  # T3 Code threads

t3cc import --all --dry-run   # preview
t3cc import --all
t3cc import 3f8fa3a6 --project ~/Repos/app

t3cc export <thread-id>       # prints the `claude --resume` command
t3cc export --all --project ~/Repos/app

t3cc sync --all --dry-run    # which originals T3 has moved past
t3cc sync --all

t3cc link-prs --all --dry-run # PRs imported sessions opened that their threads miss
t3cc link-prs --all

t3cc capabilities            # what this t3cc supports, as JSON
t3cc envs                    # T3 Code environments the desktop app is connected to
t3cc send 2d59f854 --env sunkeeper --dry-run
t3cc send 2d59f854 --env sunkeeper
```

### Import

- Sessions are matched to a T3 project by their working directory. A subdirectory or a `<repo>.worktrees/...`
  checkout goes to the enclosing project. `--project` forces a target, `--create-project` creates a missing one.
- When a session ran outside the project root in a directory that still exists, the thread runs there, on the
  session's branch. `--no-worktree` runs it from the project root instead. If the directory is gone, the
  transcript is copied under the root so `claude --resume` still finds it.
- Sessions T3 started itself, sessions already imported and sessions without a user message are skipped.
- A PR the session opened with `gh pr create` or `glab mr create` is linked to the thread, by the rule T3
  applies to its own runs: the command printed the PR's URL on a line of its own. So is one opened with a
  `gh api` or `glab api` POST to the pulls or merge requests endpoint that printed its URL (with `--jq` or a
  pipe, since their default JSON output does not), which T3 itself does not link.
- A thread that opened PRs is imported active, and T3's auto-settle rules take it from there: by default it
  settles once its PRs merge or close. T3 only settles it after it has read the PRs' state, so a thread whose
  PRs it cannot reach (deleted repository, host without credentials) stays active until settled by hand. Any
  other thread is imported settled, as T3's own importer does.
- T3 Code can stay open. t3cc writes its database directly, one short transaction per thread, and T3 lists the
  new threads at once; a window already open shows them after a reload, which t3cc reminds you of when it sees
  the server running. Each import first writes a backup next to the database
  (`statev2.sqlite.t3cc-<timestamp>.bak`).
- Writing is refused on a T3 schema version t3cc was not checked against (`--allow-unknown-schema` overrides).

### Export

- A T3 thread running on Claude already has a Claude Code transcript: t3cc prints the resume command, and with
  `--to DIR` copies the transcript under DIR.
- Other threads (Codex), or any thread with `--flatten`, are rebuilt from T3's messages as a new Claude Code
  session. Only text and attachment names carry over, not tool calls.

### Link PRs

`t3cc link-prs` adds those links to threads imported before t3cc found them. It reads each thread's original
transcript, links the PRs the thread does not have yet, and leaves the thread's settled state alone. A PR
t3cc linked once and you unlinked in T3 is not linked again. Threads whose original transcript is gone are
listed as skipped. Like an import, it backs the database up first and works while T3 Code runs.

### Remote environments

`t3cc envs` lists the T3 Code environments the desktop app is connected to, and the t3cc installed on each. It
reads the desktop's own list of connections (`connection-catalog.json`, decrypted with the key T3 Code keeps in
the keyring, through `secret-tool`) and reaches each environment over T3 Code's own connection, running commands
in one of its terminals: no ssh, nothing else to configure. Linux only for now.

### Send

`t3cc send <session> --env <label>` moves a session into a connected environment, through T3 Code's own
connection (see Remote environments). The environment needs a t3cc with `receive`; `t3cc envs` says when it
does not, and how to install one.

1. The session's checkout is read: its remotes, the branch the session last ran on and that branch's commit.
   A checkout with unpushed commits or uncommitted changes is refused; `--dirty` carries them as a git bundle,
   a binary diff and a tar of the untracked files. Ignored files never travel.
2. The transcript, its companion directory and that payload go out as one archive, over `wyrm` when both sides
   have it, then `wormhole`, then the environment's terminal (slow, but needs nothing installed);
   `--transport` forces one. Every delivery is checked against the archive's sha256.
3. On the environment, `t3cc receive` finds the T3 project whose checkout shares a remote with the session's.
   The session lands in the project root when that is on the session's branch (fast-forwarded to its commit
   when behind and clean), otherwise in the branch's worktree: an existing one at that commit, or a new one
   from `wt switch` (`git worktree add` under `<repo>.worktrees/<branch>` without wt). The transcript's working
   directory and companion paths are rewritten for its new place, and it is imported while T3 Code runs.
4. Then, and only then, the local transcript and companion directory are removed. `--copy` keeps them,
   `--archive [DIR]` keeps a tarball of what was moved (default `~/.local/state/t3cc/moved`).

When no project there shares a remote, `--clone PATH` clones the origin at PATH on the environment and creates
the project, and `--map LOCAL=REMOTE` names the remote directory of a checkout outside git or of one whose
remotes do not identify it. A local worktree left behind, or a local T3 thread of the session, is pointed out
and left alone. A local `claude` with the session open stops the send.

Hooks run around a send when `~/.config/t3cc/config.toml` defines them, the most specific table winning:

```toml
[hooks]
pre-send = "git push --quiet"

[env.sunkeeper.hooks]
post-checkout = "mise install"

[repo."github.com/arzaroth/gliff".hooks]
post-send = "notify-send \"$T3CC_SESSION is on $T3CC_ENV\""
```

`pre-send` runs here first, in the session's directory, before the checkout is read, and stops the send when
it fails. `post-checkout` and `post-import` run on the environment in the session's directory: the first stops
the receive, the second only warns. `post-send` runs here once the session has moved, and only warns. Each
gets `T3CC_SESSION`, `T3CC_ENV`, `T3CC_BRANCH` and `T3CC_REMOTE_DIR` (empty before the receive). `--no-hooks`
ignores the file.

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

- Works with T3 Code 0.1, which keeps its state in `statev2.sqlite`. Checked against 0.1.5 to 0.1.7 (schema
  migrations 72, 76 and 79); imports into any other schema need `--allow-unknown-schema`. T3 decodes every
  thread it lists, so a row it cannot read would break that list: restore the backup in that case. For T3 Code
  0.0.x, use t3cc 0.1.0.
- Imported threads run in full access. T3's own importer uses the project's default mode instead when an
  administrator policy caps the runtime mode; t3cc does not read that policy.
- An imported thread and `claude --resume` share one transcript. Use one of them at a time per session.
- Deleting an imported worktree thread in T3 may offer to remove its worktree.
- `send` keeps the session id. Usage trackers that dedupe per machine count a moved session's past turns
  again on the machine it moved to.

## Environment

| Variable            | Default     |
| ------------------- | ----------- |
| `CLAUDE_CONFIG_DIR` | `~/.claude` |
| `T3CODE_HOME`       | `~/.t3`     |
| `XDG_CONFIG_HOME`   | `~/.config` (send hooks: `t3cc/config.toml`) |
| `XDG_STATE_HOME`    | `~/.local/state` (send archives: `t3cc/moved`) |

## Development

```sh
mise run install     # uv sync
mise run test        # fails under 100% line and branch coverage
mise run check       # ty + ruff
mise run fmt
```

### Releasing

Bump the version with `uv version --bump minor` (or `patch`), move the `[Unreleased]` notes in `CHANGELOG.md`
under the new version, commit, then tag and push:

```sh
git tag v0.2.0 && git push origin master v0.2.0
```

The release workflow checks the tag against `pyproject.toml`, reruns the checks, and publishes a GitHub
release with the wheel, the sdist and the changelog section as notes.

## License

MIT
