import datetime as dt
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from t3cc import procs
from t3cc.errors import T3ccError
from t3cc.paths import Paths, backup_path

# Event payload shapes were checked against T3 Code 0.0.42, whose last schema migration is 52.
# T3 validates every event when it starts, so writing into an unchecked schema could stop it from booting.
SUPPORTED_SCHEMA_VERSIONS = frozenset({52})


def running_server_pid(runtime_file: Path, proc_root: Path = procs.PROC) -> int | None:
    try:
        pid = json.loads(runtime_file.read_text())["pid"]
    except (OSError, KeyError, TypeError, ValueError):
        return None
    args = procs.cmdline(pid, proc_root) if isinstance(pid, int) else None
    return pid if args and any(b"t3" in arg for arg in args) else None


def schema_version(con: sqlite3.Connection) -> int | None:
    return con.execute("SELECT MAX(migration_id) FROM effect_sql_migrations").fetchone()[0]


def connect(
    paths: Paths,
    *,
    write: bool,
    allow_unknown_schema: bool = False,
    proc_root: Path = procs.PROC,
) -> sqlite3.Connection:
    if not paths.t3_db.exists():
        raise T3ccError(f"{paths.t3_db} not found")
    if write:
        pid = running_server_pid(paths.t3_runtime, proc_root)
        if pid:
            raise T3ccError(
                f"T3 Code is running (server pid {pid}). Quit it first: it only picks up new events at startup."
            )
        con = sqlite3.connect(paths.t3_db)
    else:
        con = sqlite3.connect(f"file:{paths.t3_db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    version = schema_version(con)
    if write and not allow_unknown_schema and version not in SUPPORTED_SCHEMA_VERSIONS:
        con.close()
        supported = ", ".join(str(v) for v in sorted(SUPPORTED_SCHEMA_VERSIONS))
        raise T3ccError(
            f"T3 Code schema version {version} is untested (supported: {supported}). "
            "Pass --allow-unknown-schema to write anyway; a backup is still taken."
        )
    return con


def backup(con: sqlite3.Connection, db_path: Path, now: dt.datetime | None = None) -> Path:
    dest = backup_path(db_path, now)
    with closing(sqlite3.connect(dest)) as out:
        con.backup(out)
    return dest
