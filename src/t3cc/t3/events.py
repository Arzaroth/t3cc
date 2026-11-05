"""Append to T3 Code's orchestration V2 event store, projecting each event in the same transaction as its EventSink."""

import sqlite3
import uuid
from dataclasses import dataclass

from t3cc import timeutil
from t3cc.t3 import projections
from t3cc.t3.projections import dumps


def new_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class ThreadEvent:
    id: str
    type: str
    occurred_at: str
    payload: dict
    driver: str | None = None
    provider_instance_id: str | None = None

    @property
    def metadata(self) -> dict:
        metadata = {}
        if self.driver is not None:
            metadata["driver"] = self.driver
        if self.provider_instance_id is not None:
            metadata["providerInstanceId"] = self.provider_instance_id
        return metadata


INSERT_EVENT = """INSERT INTO orchestration_events (event_id, aggregate_kind, stream_id, stream_version, event_type,
     occurred_at, command_id, causation_event_id, correlation_id, actor_kind, payload_json, metadata_json,
     application_event_version)
   VALUES (?, ?, ?,
     (SELECT COALESCE(MAX(stream_version) + 1, 0) FROM orchestration_events WHERE aggregate_kind = ? AND stream_id = ?),
     ?, ?, ?, NULL, ?, ?, ?, ?, 2)
   RETURNING sequence"""


class EventWriter:
    def __init__(self, con: sqlite3.Connection):
        self.con = con

    def thread_events(self, thread_id: str, events: list[ThreadEvent], now: str | None = None) -> None:
        sequence = 0
        for event in events:
            sequence = self._insert(
                event.id,
                "thread",
                thread_id,
                event.type,
                event.occurred_at,
                None,
                "server",
                event.payload,
                event.metadata,
            )
            projections.apply_thread_event(self.con, event.type, event.payload)
        projections.mark_projected(self.con, sequence, now or timeutil.now_iso())

    def project_created(self, project: dict) -> None:
        command_id = new_id()
        project_id, at = project["projectId"], project["createdAt"]
        event_id = f"event:command:{command_id}:{new_id()}"
        sequence = self._insert(
            event_id, "project", project_id, "project.created", at, command_id, "client", project, {}
        )
        projections.apply_project_created(self.con, project)
        self.con.execute(
            """INSERT INTO orchestration_command_receipts (command_id, aggregate_kind, aggregate_id, accepted_at,
                 result_sequence, status, error, command_type)
               VALUES (?, 'project', ?, ?, ?, 'accepted', NULL, 'project.create')""",
            (command_id, project_id, at, sequence),
        )

    def _insert(
        self,
        event_id: str,
        aggregate_kind: str,
        stream_id: str,
        event_type: str,
        occurred_at: str,
        command_id: str | None,
        actor_kind: str,
        payload: dict,
        metadata: dict,
    ) -> int:
        row = self.con.execute(
            INSERT_EVENT,
            (
                event_id,
                aggregate_kind,
                stream_id,
                aggregate_kind,
                stream_id,
                event_type,
                occurred_at,
                command_id,
                command_id,
                actor_kind,
                dumps(payload),
                dumps(metadata),
            ),
        ).fetchone()
        return row[0]
