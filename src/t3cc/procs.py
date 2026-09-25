import os
from collections.abc import Iterator
from pathlib import Path

PROC = Path("/proc")


def cmdline(pid: int, proc_root: Path = PROC) -> list[bytes] | None:
    try:
        return (proc_root / str(pid) / "cmdline").read_bytes().split(b"\0")
    except OSError:
        return None


def others(proc_root: Path = PROC) -> Iterator[tuple[int, list[bytes]]]:
    """Every process but this one, with its command line, when it can be read."""
    for entry in proc_root.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        args = cmdline(int(entry.name), proc_root)
        if args is not None:
            yield int(entry.name), args
