"""Append events to T3 Code's event store in the exact shape its orchestration engine writes them."""

import json
import sqlite3
import uuid
from dataclasses import dataclass


def new_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class Event:
    type: str
    occurred_at: str
    payload: dict


class EventWriter:
    def __init__(self, con: sqlite3.Connection):
        self.con = con

    def command(
        self, aggregate_kind: str, stream_id: str, events: list[Event], *, metadata: dict | None = None
    ) -> None:
        """Append the events of one command, accepted when its last event occurred."""
        command_id = new_id()
        sequences = [self._append(aggregate_kind, stream_id, event, command_id, metadata or {}) for event in events]
        self.con.execute(
            "INSERT INTO orchestration_command_receipts VALUES (?, ?, ?, ?, ?, 'accepted', NULL)",
            (command_id, aggregate_kind, stream_id, events[-1].occurred_at, sequences[-1]),
        )

    def _append(self, aggregate_kind: str, stream_id: str, event: Event, command_id: str, metadata: dict) -> int:
        version = self.con.execute(
            "SELECT COALESCE(MAX(stream_version) + 1, 0) FROM orchestration_events "
            "WHERE aggregate_kind = ? AND stream_id = ?",
            (aggregate_kind, stream_id),
        ).fetchone()[0]
        row = self.con.execute(
            """INSERT INTO orchestration_events (event_id, aggregate_kind, stream_id, stream_version, event_type,
                 occurred_at, command_id, causation_event_id, correlation_id, actor_kind, payload_json, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, 'client', ?, ?)
               RETURNING sequence""",
            (
                new_id(),
                aggregate_kind,
                stream_id,
                version,
                event.type,
                event.occurred_at,
                command_id,
                command_id,
                json.dumps(event.payload, ensure_ascii=False),
                json.dumps(metadata),
            ),
        ).fetchone()
        return row[0]
