"""Build a Claude Code transcript from plain turns, resumable with `claude --resume`."""

import uuid
from dataclasses import dataclass

CONTINUATION_PROMPT = "(conversation continued from T3 Code)"


@dataclass
class Turn:
    role: str
    text: str
    timestamp: str


def merge_turns(turns: list[Turn]) -> list[Turn]:
    """Collapse consecutive same-role turns and make the conversation open on a user turn."""
    merged: list[Turn] = []
    for turn in turns:
        text = turn.text.strip()
        if not text:
            continue
        if merged and merged[-1].role == turn.role:
            merged[-1].text += "\n\n" + text
        else:
            merged.append(Turn(turn.role, text, turn.timestamp))
    if merged and merged[0].role != "user":
        merged.insert(0, Turn("user", CONTINUATION_PROMPT, merged[0].timestamp))
    return merged


def build_session(turns: list[Turn], *, cwd: str, branch: str | None, model: str, title: str) -> tuple[str, list[dict]]:
    session_id = str(uuid.uuid4())
    common = {
        "isSidechain": False,
        "userType": "external",
        "entrypoint": "cli",
        "cwd": cwd,
        "sessionId": session_id,
        "version": "t3cc",
        "gitBranch": branch or "",
    }
    records: list[dict] = []
    parent = None
    for turn in turns:
        record_id = str(uuid.uuid4())
        record = {"parentUuid": parent, **common, "type": turn.role, "uuid": record_id, "timestamp": turn.timestamp}
        if turn.role == "user":
            record["message"] = {"role": "user", "content": turn.text}
        else:
            record["message"] = {
                "id": f"msg_t3cc_{uuid.uuid4().hex[:24]}",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": turn.text}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
        records.append(record)
        parent = record_id
    records.append({"type": "ai-title", "aiTitle": title, "sessionId": session_id})
    return session_id, records
