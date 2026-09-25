# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- `t3cc list-t3` marks a thread `claude` only when T3 runs it on Claude and its
  transcript exists, the same test `t3cc export` uses.

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
  Claude sessions T3 already has.
- Imports refuse to run while T3 Code is open, take a backup of its database
  first, and refuse schema versions they were not checked against.
