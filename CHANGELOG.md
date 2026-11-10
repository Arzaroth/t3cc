# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-11-10

### Added

- `t3cc capabilities` prints the version, a protocol number and the features
  this t3cc supports, as JSON, so two installs can check each other.
- `t3cc envs` lists the T3 Code environments the desktop app is connected to,
  and the t3cc each one has. It reaches them through T3 Code's own connection
  and terminals, with the desktop's saved credentials. Linux only for now.
- `t3cc send` moves a Claude Code session into one of those environments: the
  branch it last ran on is checked out there in a worktree (with `wt` when
  installed), the transcript is rewritten for its new directory and imported
  as a thread of the project sharing a remote with it, and only then is the
  local copy removed (`--copy` keeps it, `--archive` keeps a tarball).
  `--dirty` carries unpushed commits and uncommitted files, `--clone` creates
  a missing project, `--map` places a checkout git cannot identify. Archives
  travel over wyrm, magic-wormhole or T3 Code's terminal, checked by sha256.
- Hooks around a send (`pre-send`, `post-checkout`, `post-import`,
  `post-send`), read from `~/.config/t3cc/config.toml` per environment and
  per repository.
- `t3cc receive`, the environment's side of a send.

### Changed

- t3cc now depends on websockets, cryptography and resumove (from its git tag).
- `t3cc import` and `link-prs` work while T3 Code is running. Each thread is
  written in its own short transaction, and a window already open shows the
  new threads after a reload.

### Fixed

- `t3cc sync` no longer replaces an original transcript that a `claude`
  started without `--resume` still has open: it reads Claude Code's own list
  of open sessions, not only process command lines.

## [0.3.0] - 2026-11-05

### Added

- Imports link the PRs a session opened with `gh pr create` or `glab mr create`
  to its T3 Code thread, by the rule T3 Code applies to its own runs, and the
  ones it opened with a `gh api` or `glab api` POST. A thread that opened PRs
  is imported active, and T3 Code's auto-settle rules take it from there;
  other threads are still imported settled.
- `t3cc link-prs` adds those links to threads imported before. It does not
  relink a PR you unlinked, and lists the threads whose transcript is gone.

## [0.2.0] - 2026-11-05

### Changed

- t3cc now works with T3 Code 0.1 (checked against 0.1.5 to 0.1.7), which
  moved its state to a new database, `statev2.sqlite`. Imports write the events
  T3 Code 0.1's own importer writes, with the thread, message and provider rows
  it builds from them, in one transaction. T3 Code 0.0.x is no longer
  supported: use t3cc 0.1.0 with it.
- `t3cc list-t3`, `export` and `sync` leave out the subagent threads and side
  chats T3 Code hides from its own thread list. Exports leave out the
  notifications and prompts agents post as user messages.

## [0.1.0] - 2026-09-25

### Added

- `t3cc import` turns Claude Code sessions into T3 Code threads that resume the
  same Claude session. It writes what T3 Code's own importer writes, so T3 treats
  the threads as its own, but without that importer's limits: any age, any
  number of sessions, the full history, and sessions started outside a project's
  root. Sessions in a subdirectory or a `<repo>.worktrees/...` checkout go to the
  enclosing project and run from their own directory, on their own branch.
- `t3cc export` hands T3 Code threads back to Claude Code. A thread running on
  Claude already has a transcript, and export prints its `claude --resume`
  command. Other threads are rebuilt from T3's messages as a new session.
- `t3cc sync` catches an imported session's original transcript up with the
  copy T3 Code writes to when it runs the thread from another directory. It
  fast-forwards only when the original is a strict prefix of the copy, reports
  a session continued in Claude Code as behind, and overwrites a diverged one
  only with `--force`, after a backup.
- `t3cc list-claude` and `t3cc list-t3` show what each side holds and which
  Claude sessions T3 already has. `list-t3` marks a thread `claude` when T3 runs
  it on Claude and its transcript exists, the same test `t3cc export` uses.
- Imports refuse to run while T3 Code is open, take a backup of its database
  first, and refuse schema versions they were not checked against.
