"""Apply events to T3 Code's orchestration V2 projection tables the way its ProjectionStore and ProjectStore do."""

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

PROJECTION_SCHEMA_VERSION = 2


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _upsert(con: sqlite3.Connection, table: str, key: str, row: Mapping[str, Any]) -> None:
    columns = ", ".join(row)
    updates = ", ".join(f"{column} = excluded.{column}" for column in row if column != key)
    con.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({', '.join('?' * len(row))}) "
        f"ON CONFLICT({key}) DO UPDATE SET {updates}",
        tuple(row.values()),
    )


def _thread(con: sqlite3.Connection, thread: dict) -> None:
    _upsert(
        con,
        "orchestration_v2_projection_threads",
        "thread_id",
        {
            "thread_id": thread["id"],
            "project_id": thread["projectId"],
            "title": thread["title"],
            "default_provider": thread["providerInstanceId"],
            "provider_instance_id": thread["providerInstanceId"],
            "runtime_mode": thread["runtimeMode"],
            "interaction_mode": thread["interactionMode"],
            "active_provider_thread_id": thread["activeProviderThreadId"],
            "created_at": thread["createdAt"],
            "updated_at": thread["updatedAt"],
            "archived_at": thread["archivedAt"],
            "deleted_at": thread["deletedAt"],
            "payload_json": dumps(thread),
        },
    )


def _message(con: sqlite3.Connection, message: dict) -> None:
    _upsert(
        con,
        "orchestration_v2_projection_messages",
        "message_id",
        {
            "message_id": message["id"],
            "thread_id": message["threadId"],
            "run_id": message["runId"],
            "node_id": message["nodeId"],
            "role": message["role"],
            "streaming": int(message["streaming"]),
            "created_at": message["createdAt"],
            "updated_at": message["updatedAt"],
            "payload_json": dumps(message),
        },
    )


def _turn_item(con: sqlite3.Connection, item: dict) -> None:
    con.execute(
        "INSERT INTO orchestration_v2_turn_item_positions (thread_id, turn_item_id, ordinal) VALUES (?, ?, ?) "
        "ON CONFLICT(thread_id, turn_item_id) DO NOTHING",
        (item["threadId"], item["id"], item["ordinal"]),
    )
    _upsert(
        con,
        "orchestration_v2_projection_turn_items",
        "turn_item_id",
        {
            "turn_item_id": item["id"],
            "thread_id": item["threadId"],
            "run_id": item["runId"],
            "node_id": item["nodeId"],
            "provider_thread_id": item["providerThreadId"],
            "provider_turn_id": item["providerTurnId"],
            "parent_item_id": item["parentItemId"],
            "ordinal": item["ordinal"],
            "type": item["type"],
            "status": item["status"],
            "updated_at": item["updatedAt"],
            "payload_json": dumps(item),
        },
    )


def _provider_thread(con: sqlite3.Connection, provider_thread: dict) -> None:
    _upsert(
        con,
        "orchestration_v2_projection_provider_threads",
        "provider_thread_id",
        {
            "provider_thread_id": provider_thread["id"],
            "thread_id": provider_thread["appThreadId"],
            "owner_node_id": provider_thread["ownerNodeId"],
            "provider": provider_thread["providerInstanceId"],
            "driver": provider_thread["driver"],
            "provider_instance_id": provider_thread["providerInstanceId"],
            "provider_session_id": provider_thread["providerSessionId"],
            "status": provider_thread["status"],
            "first_run_ordinal": provider_thread["firstRunOrdinal"],
            "last_run_ordinal": provider_thread["lastRunOrdinal"],
            "updated_at": provider_thread["updatedAt"],
            "payload_json": dumps(provider_thread),
        },
    )
    row = con.execute(
        "SELECT payload_json FROM orchestration_v2_projection_threads WHERE thread_id = ?",
        (provider_thread["appThreadId"],),
    ).fetchone()
    if row is not None:
        thread = json.loads(row[0])
        _thread(
            con, {**thread, "activeProviderThreadId": provider_thread["id"], "updatedAt": provider_thread["updatedAt"]}
        )


THREAD_PROJECTORS = {
    "thread.created": _thread,
    "message.updated": _message,
    "turn-item.updated": _turn_item,
    "provider-thread.updated": _provider_thread,
}


def apply_thread_event(con: sqlite3.Connection, event_type: str, payload: dict) -> None:
    projector = THREAD_PROJECTORS.get(event_type)
    if projector is None:
        raise ValueError(f"no projection for {event_type}")
    projector(con, payload)


def mark_projected(con: sqlite3.Connection, sequence: int, now: str) -> None:
    con.execute(
        """INSERT INTO orchestration_v2_projection_metadata (projection_name, schema_version, last_sequence, updated_at)
           VALUES ('thread-projections', ?, ?, ?)
           ON CONFLICT(projection_name) DO UPDATE SET
             schema_version = excluded.schema_version,
             last_sequence = excluded.last_sequence,
             updated_at = excluded.updated_at""",
        (PROJECTION_SCHEMA_VERSION, sequence, now),
    )


def apply_project_created(con: sqlite3.Connection, project: dict) -> None:
    _upsert(
        con,
        "projection_projects",
        "project_id",
        {
            "project_id": project["projectId"],
            "title": project["title"],
            "workspace_root": project["workspaceRoot"],
            "default_model_selection_json": None,
            "default_thread_env_mode": None,
            "auto_pull": 0,
            "favicon_path": None,
            "project_icon_json": None,
            "scripts_json": "[]",
            "created_at": project["createdAt"],
            "updated_at": project["updatedAt"],
            "deleted_at": None,
        },
    )
