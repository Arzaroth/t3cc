# Roadmap

## Next

- **Fetch**: the reverse of `send`, moving a thread's session from a connected environment back here.
- **macOS**: read the desktop's connections from the Keychain (`t3code Safe Storage`, `v10`, PBKDF2 with 1003
  iterations). Needs a Mac to test on; the shell side already uses only POSIX tools.
- **Windows**: DPAPI-protected connections, and a PowerShell remote for environments running on Windows.
- **`--rewrite-paths`**: rewrite every path into the old checkout inside a moved transcript, not only its
  working directory and companion paths, through resumove.

## Later

- Resumable uploads over the terminal channel, for large transcripts on slow links.
- Confirm the imported thread through the environment's RPC instead of `t3cc receive`'s own read-back.
