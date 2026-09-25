"""Read a Claude Code transcript the way T3 Code's own AgentSessionScanner does, minus its 200-message cap."""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from t3cc import timeutil

DEFAULT_MODEL = "claude-opus-5-5"
TEXT_BLOCK_TYPES = ("text", "input_text", "output_text")


@dataclass(frozen=True)
class Message:
    role: str
    text: str
    created_at: str


@dataclass(frozen=True)
class Transcript:
    path: Path
    stat: os.stat_result
    session_id: str
    title: str
    model: str | None
    cwd: str | None
    branch: str | None
    messages: tuple[Message, ...]
    updated_at: str

    @property
    def has_user_message(self) -> bool:
        return any(m.role == "user" for m in self.messages)


def extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = [
        (block.get("text") or "").strip()
        for block in content
        if isinstance(block, dict) and block.get("type") in TEXT_BLOCK_TYPES
    ]
    return "\n".join(part for part in parts if part)


def _records(path: Path):
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def _clean(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def parse(path: Path) -> Transcript:
    stat = path.stat()
    fallback = timeutil.from_epoch(stat.st_mtime)
    session_id = path.stem
    ai_title = custom_title = model = cwd = branch = None
    messages: list[Message] = []
    for record in _records(path):
        if record.get("isSidechain") or record.get("isMeta") or record.get("isCompactSummary"):
            continue
        session_id = _clean(record.get("sessionId")) or session_id
        ai_title = _clean(record.get("aiTitle")) or ai_title
        custom_title = _clean(record.get("customTitle")) or custom_title
        cwd = cwd or _clean(record.get("cwd")) or None
        branch = _clean(record.get("gitBranch")) or branch
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        record_model = _clean(message.get("model"))
        if record_model and record_model != "<synthetic>":
            model = record_model
        if record.get("type") not in ("user", "assistant"):
            continue
        text = extract_text(message.get("content"))
        if text:
            messages.append(Message(record["type"], text, timeutil.normalize(record.get("timestamp"), fallback)))
    first_user = next((m for m in messages if m.role == "user"), None)
    derived = first_user.text.split("\n")[0][:100].strip() if first_user else ""
    return Transcript(
        path=path,
        stat=stat,
        session_id=session_id,
        title=custom_title or ai_title or derived or "Imported thread",
        model=model,
        cwd=cwd,
        branch=branch,
        messages=tuple(messages),
        updated_at=fallback,
    )
