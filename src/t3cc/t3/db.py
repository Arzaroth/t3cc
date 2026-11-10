import datetime as dt
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from t3cc import procs
from t3cc.errors import T3ccError
from t3cc.paths import Paths, backup_path

# Event and projection shapes were checked against T3 Code 0.1.5 to 0.1.7 (schema migrations 72, 76, 79).
# T3 decodes every projection row it reads, so writing into an unchecked schema could break the threads it lists.
SUPPORTED_SCHEMA_VERSIONS = frozenset({72, 76, 79})


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
) -> sqlite3.Connection:
    if not paths.t3_db.exists():
        if paths.t3_legacy_db.exists():
            raise T3ccError(
                f"{paths.t3_db} not found, only {paths.t3_legacy_db.name}: T3 Code older than 0.1 needs t3cc 0.1"
            )
        raise T3ccError(f"{paths.t3_db} not found")
    if write:
        con = sqlite3.connect(paths.t3_db, timeout=10)
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
