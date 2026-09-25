import json
import shutil
from collections.abc import Iterable
from pathlib import Path

from t3cc.errors import T3ccError
from t3cc.paths import claude_project_dir


class ClaudeStore:
    def __init__(self, projects: Path):
        self.projects = projects

    def project_dir(self, cwd: str | Path) -> Path:
        return claude_project_dir(self.projects, cwd)

    def all_transcripts(self) -> list[Path]:
        return sorted(self.projects.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)

    def by_session_id(self, session_id: str) -> Path | None:
        hits = sorted(self.projects.glob(f"*/{session_id}.jsonl"), key=lambda p: p.stat().st_size, reverse=True)
        return hits[0] if hits else None

    def find(self, ref: str) -> Path:
        candidate = Path(ref).expanduser()
        if candidate.suffix == ".jsonl" and candidate.is_file():
            return candidate
        hits = list(self.projects.glob(f"*/{ref}*.jsonl"))
        ids = sorted({hit.stem for hit in hits})
        if not ids:
            raise T3ccError(f"no Claude Code session matches '{ref}'")
        if len(ids) > 1:
            raise T3ccError(f"'{ref}' is ambiguous: {', '.join(ids[:5])}")
        return max(hits, key=lambda p: p.stat().st_size)

    def copy_into(self, transcript: Path, cwd: str) -> Path | None:
        """Copy a transcript under cwd's project dir so `claude --resume` finds it from there."""
        dest = self.project_dir(cwd) / transcript.name
        if dest.exists():
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(transcript, dest)
        return dest

    def write_session(self, cwd: str, session_id: str, records: Iterable[dict]) -> Path:
        dest = self.project_dir(cwd) / f"{session_id}.jsonl"
        if dest.exists():
            raise T3ccError(f"refusing to overwrite {dest}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return dest
