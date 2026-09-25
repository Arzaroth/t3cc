"""Append events to T3 Code's event store in the exact shape its orchestration engine writes them."""

import json
import sqlite3
import uuid


def new_id() -> str:
    return str(uuid.uuid4())


class EventWriter:
    def __init__(self, con: sqlite3.Connection):
        self.con = con

    def append(
        self,
        *,
        aggregate_kind: str,
        stream_id: str,
        event_type: str,
        occurred_at: str,
        command_id: str,
        payload: dict,
        metadata: dict | None = None,
    ) -> int:
        version = self.con.execute(
            "SELECT COALESCE(MAX(stream_version) + 1, 0) FROM orchestration_events "
            "WHERE aggregate_kind = ? AND stream_id = ?",
            (aggregate_kind, stream_id),
        ).fetchone()[0]
        cursor = self.con.execute(
            """INSERT INTO orchestration_events (event_id, aggregate_kind, stream_id, stream_version, event_type,
                 occurred_at, command_id, causation_event_id, correlation_id, actor_kind, payload_json, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, 'client', ?, ?)""",
            (
                new_id(),
                aggregate_kind,
                stream_id,
                version,
                event_type,
                occurred_at,
                command_id,
                command_id,
                json.dumps(payload, ensure_ascii=False),
                json.dumps(metadata or {}),
            ),
        )
        return cursor.lastrowid

    def receipt(self, *, command_id: str, aggregate_kind: str, aggregate_id: str, accepted_at: str, sequence: int):
        self.con.execute(
            "INSERT INTO orchestration_command_receipts VALUES (?, ?, ?, ?, ?, 'accepted', NULL)",
            (command_id, aggregate_kind, aggregate_id, accepted_at, sequence),
        )
