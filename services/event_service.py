"""Versioned live event state, schedule edits, and an operator audit trail."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from memory.db import connection_scope, initialize_database

EVENT_FIELDS = {"name", "type", "date", "venue", "tone", "language", "status", "chief_guest"}
ITEM_FIELDS = {
    "title", "type", "person_name", "designation", "status",
    "planned_duration", "notes", "transcript_language", "summary",
}
ITEM_STATUSES = {"pending", "arrived", "absent", "on_stage", "done", "skipped", "removed"}
EVENT_STATUSES = {"not_started", "running", "paused", "ended"}


class EventNotFoundError(LookupError):
    """Raised when an event or schedule item does not exist."""


class EventService:
    """Perform every live event mutation transactionally and version its state."""

    def __init__(self, database_path: str | Path | None = None) -> None:
        self.database_path = Path(database_path) if database_path else None
        initialize_database(database_path=self.database_path)
        self._migrate_legacy_events()

    def _migrate_legacy_events(self) -> None:
        """Import existing AVA events and schedules without changing their legacy rows."""
        with connection_scope(self.database_path) as connection:
            legacy_events = connection.execute(
                """SELECT e.* FROM events e LEFT JOIN event_control c ON c.id = e.id
                   WHERE c.id IS NULL ORDER BY e.id"""
            ).fetchall()
            for legacy in legacy_events:
                session_id = legacy["session_id"] or f"legacy-event-{legacy['id']}-{uuid.uuid4().hex[:8]}"
                connection.execute("INSERT OR IGNORE INTO sessions(id) VALUES (?)", (session_id,))
                connection.execute(
                    """UPDATE events SET session_id = ? WHERE id = ? AND session_id IS NULL""",
                    (session_id, legacy["id"]),
                )
                connection.execute(
                    """INSERT OR IGNORE INTO event_control(
                        id, session_id, name, type, date, venue, tone, language,
                        status, chief_guest, state_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'en', 'not_started', ?, 1)""",
                    (
                        legacy["id"], session_id, legacy["name"], legacy["event_type"],
                        legacy["event_date"] or "", legacy["venue"] or "",
                        legacy["tone"] or "Professional", legacy["chief_guest"] or "",
                    ),
                )
                rows = connection.execute(
                    "SELECT * FROM schedule WHERE event_id = ? ORDER BY event_time, id",
                    (legacy["id"],),
                ).fetchall()
                if not rows:
                    try:
                        saved = json.loads(legacy["schedule_json"] or "[]")
                    except json.JSONDecodeError:
                        saved = []
                    rows = [
                        {
                            "event_time": item.get("time", ""),
                            "title": item.get("title", ""),
                            "event_type": item.get("type", "other"),
                            "participant": item.get("participant", item.get("person_name", "")),
                        }
                        for item in saved
                    ]
                item_ids: list[int] = []
                for index, item in enumerate(rows):
                    item = dict(item)
                    if not str(item.get("title", "")).strip():
                        continue
                    item_ids.append(
                        self._insert_item(
                            connection,
                            int(legacy["id"]),
                            {
                                "title": item["title"],
                                "type": item.get("event_type") or "other",
                                "person_name": item.get("participant") or "",
                            },
                            index,
                        )
                    )
                current_index = int(legacy["current_item_index"] or 0)
                if item_ids and 0 <= current_index < len(item_ids):
                    connection.execute(
                        "UPDATE schedule_items SET status = 'on_stage' WHERE id = ?",
                        (item_ids[current_index],),
                    )
                    connection.execute(
                        "UPDATE event_control SET current_item_id = ? WHERE id = ?",
                        (item_ids[current_index], legacy["id"]),
                    )

    def create_event(
        self,
        session_id: str,
        *,
        name: str,
        event_type: str = "event",
        date: str = "",
        venue: str = "",
        tone: str = "Professional",
        language: str = "en",
        chief_guest: str = "",
        schedule: list[dict[str, Any]] | None = None,
        event_id: int | None = None,
    ) -> dict[str, Any]:
        """Create a live event and initial schedule, preserving an optional legacy ID."""
        if not name.strip():
            raise ValueError("Event name cannot be empty.")
        self._validate_language(language)
        with connection_scope(self.database_path) as connection:
            if event_id is None:
                cursor = connection.execute(
                    """INSERT INTO event_control(
                        session_id, name, type, date, venue, tone, language, chief_guest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (session_id, name.strip(), event_type, date, venue, tone, language, chief_guest),
                )
                identifier = int(cursor.lastrowid)
            else:
                connection.execute(
                    """INSERT INTO event_control(
                        id, session_id, name, type, date, venue, tone, language, chief_guest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (event_id, session_id, name.strip(), event_type, date, venue, tone, language, chief_guest),
                )
                identifier = event_id
            item_ids = [
                self._insert_item(connection, identifier, item, index)
                for index, item in enumerate(schedule or [])
            ]
            if item_ids:
                connection.execute(
                    "UPDATE event_control SET current_item_id = ? WHERE id = ?",
                    (item_ids[0], identifier),
                )
            self._audit(connection, identifier, "event_created", {}, {"name": name.strip()}, "operator")
        return self.get_event(identifier)

    def get_for_session(self, session_id: str) -> dict[str, Any] | None:
        """Return the live event associated with a conversation session."""
        with connection_scope(self.database_path) as connection:
            row = connection.execute(
                "SELECT id FROM event_control WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return self.get_event(int(row["id"])) if row else None

    def get_event(self, event_id: int) -> dict[str, Any]:
        """Load an event with its current ordered schedule, audit log, and summaries."""
        with connection_scope(self.database_path) as connection:
            event = connection.execute(
                "SELECT * FROM event_control WHERE id = ?", (event_id,)
            ).fetchone()
            if not event:
                raise EventNotFoundError(f"Event {event_id} was not found.")
            items = connection.execute(
                "SELECT * FROM schedule_items WHERE event_id = ? ORDER BY order_index, id",
                (event_id,),
            ).fetchall()
            logs = connection.execute(
                "SELECT * FROM event_log WHERE event_id = ? ORDER BY id DESC LIMIT 100",
                (event_id,),
            ).fetchall()
            summaries = connection.execute(
                """SELECT s.item_id, s.summary, s.created_at, i.person_name, i.title
                   FROM event_summaries s JOIN schedule_items i ON i.id = s.item_id
                   WHERE s.event_id = ? ORDER BY s.id DESC""",
                (event_id,),
            ).fetchall()
        result = dict(event)
        result["items"] = [dict(item) for item in items]
        result["log"] = [dict(row) for row in logs]
        result["summaries"] = [dict(row) for row in summaries]
        return result

    def update_event(
        self, event_id: int, changes: dict[str, Any], operator: str = "operator"
    ) -> dict[str, Any]:
        """Validate and update event metadata and state."""
        unknown = set(changes) - EVENT_FIELDS
        if unknown:
            raise ValueError(f"Unsupported event fields: {', '.join(sorted(unknown))}")
        if not changes:
            raise ValueError("No event changes were supplied.")
        if "language" in changes:
            self._validate_language(str(changes["language"]))
        if "status" in changes and changes["status"] not in EVENT_STATUSES:
            raise ValueError("Invalid event status.")
        if "name" in changes and not str(changes["name"]).strip():
            raise ValueError("Event name cannot be empty.")
        with connection_scope(self.database_path) as connection:
            event = self._require_event(connection, event_id)
            old = {key: event[key] for key in changes}
            self._record_undo(connection, event_id)
            columns = ", ".join(f"{key} = ?" for key in changes)
            connection.execute(
                f"UPDATE event_control SET {columns}, state_version = state_version + 1 WHERE id = ?",
                (*changes.values(), event_id),
            )
            self._audit(connection, event_id, "event_updated", old, changes, operator)
        return self.get_event(event_id)

    def add_item(
        self,
        event_id: int,
        item: dict[str, Any],
        operator: str = "operator",
        position: int | None = None,
    ) -> dict[str, Any]:
        """Insert a validated schedule item at a position in the live schedule."""
        self._validate_item(item)
        with connection_scope(self.database_path) as connection:
            self._require_event(connection, event_id)
            self._record_undo(connection, event_id)
            row = connection.execute(
                "SELECT COALESCE(MAX(order_index), -1) + 1 AS next FROM schedule_items WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            next_index = int(row["next"])
            insert_at = next_index if position is None else position
            if insert_at < 0 or insert_at > next_index:
                raise ValueError(f"Position must be between 0 and {next_index}.")
            existing = connection.execute(
                "SELECT id, order_index FROM schedule_items WHERE event_id = ?",
                (event_id,),
            ).fetchall()
            connection.execute(
                "UPDATE schedule_items SET order_index = -order_index - 1 WHERE event_id = ?",
                (event_id,),
            )
            for row_item in existing:
                old_index = int(row_item["order_index"])
                new_index = old_index + 1 if old_index >= insert_at else old_index
                connection.execute(
                    "UPDATE schedule_items SET order_index = ? WHERE id = ?",
                    (1_000_000 + new_index, row_item["id"]),
                )
            connection.execute(
                "UPDATE schedule_items SET order_index = order_index - 1000000 WHERE event_id = ?",
                (event_id,),
            )
            item_id = self._insert_item(connection, event_id, item, insert_at)
            self._increment_version(connection, event_id)
            self._audit(connection, event_id, "item_added", {}, {"item_id": item_id, **item}, operator)
        return self.get_event(event_id)

    def update_item(
        self, event_id: int, item_id: int, changes: dict[str, Any], operator: str = "operator"
    ) -> dict[str, Any]:
        """Edit a schedule item or its attendance state."""
        if not changes or set(changes) - ITEM_FIELDS:
            raise ValueError("Supply supported schedule item fields only.")
        self._validate_item(changes, partial=True)
        with connection_scope(self.database_path) as connection:
            self._require_item(connection, event_id, item_id)
            self._record_undo(connection, event_id)
            item = connection.execute(
                "SELECT * FROM schedule_items WHERE id = ?", (item_id,)
            ).fetchone()
            old = {key: item[key] for key in changes}
            current_event = self._require_event(connection, event_id)
            connection.execute(
                f"UPDATE schedule_items SET {', '.join(f'{key} = ?' for key in changes)} WHERE id = ?",
                (*changes.values(), item_id),
            )
            if "summary" in changes and str(changes["summary"]).strip():
                connection.execute(
                    """INSERT INTO event_summaries(event_id, item_id, summary)
                       VALUES (?, ?, ?) ON CONFLICT(event_id, item_id)
                       DO UPDATE SET summary = excluded.summary, created_at = CURRENT_TIMESTAMP""",
                    (event_id, item_id, str(changes["summary"])),
                )
            elif "summary" in changes:
                connection.execute(
                    "DELETE FROM event_summaries WHERE event_id = ? AND item_id = ?",
                    (event_id, item_id),
                )
            affected_ids = [item_id]
            if (
                changes.get("status") in {"absent", "arrived"}
                and str(item["person_name"]).strip()
            ):
                related_statuses = (
                    ("pending", "arrived", "on_stage")
                    if changes["status"] == "absent"
                    else ("pending", "absent")
                )
                status_placeholders = ",".join("?" for _ in related_statuses)
                related = connection.execute(
                    f"""SELECT id FROM schedule_items WHERE event_id = ?
                        AND lower(trim(person_name)) = lower(trim(?)) AND order_index >= ?
                        AND (status IN ({status_placeholders}) OR id = ?)""",
                    (
                        event_id, item["person_name"], item["order_index"],
                        *related_statuses, item_id,
                    ),
                ).fetchall()
                affected_ids = [int(row["id"]) for row in related]
                if affected_ids:
                    placeholders = ",".join("?" for _ in affected_ids)
                    connection.execute(
                        f"UPDATE schedule_items SET status = ? WHERE id IN ({placeholders})",
                        (changes["status"], *affected_ids),
                    )
            if (
                changes.get("status") in {"absent", "removed", "skipped"}
                and current_event["current_item_id"] in affected_ids
            ):
                order_row = connection.execute(
                    "SELECT order_index FROM schedule_items WHERE id = ?", (item_id,)
                ).fetchone()
                next_item = connection.execute(
                    """SELECT id FROM schedule_items WHERE event_id = ? AND order_index > ?
                       AND status IN ('pending', 'arrived') ORDER BY order_index, id LIMIT 1""",
                    (event_id, order_row["order_index"]),
                ).fetchone()
                if next_item:
                    connection.execute(
                        "UPDATE schedule_items SET status = 'on_stage' WHERE id = ?",
                        (next_item["id"],),
                    )
                connection.execute(
                    "UPDATE event_control SET current_item_id = ? WHERE id = ?",
                    (int(next_item["id"]) if next_item else None, event_id),
                )
            self._increment_version(connection, event_id)
            self._audit(
                connection,
                event_id,
                "item_updated",
                old,
                {"item_id": item_id, **changes, "affected_item_ids": affected_ids},
                operator,
            )
        return self.get_event(event_id)

    def delete_item(
        self, event_id: int, item_id: int, operator: str = "operator"
    ) -> dict[str, Any]:
        """Mark an item removed rather than erasing its audit/transcript history."""
        return self.update_item(event_id, item_id, {"status": "removed"}, operator)

    def reorder(
        self, event_id: int, item_ids: list[int], operator: str = "operator"
    ) -> dict[str, Any]:
        """Apply an exact permutation of the event's schedule items."""
        with connection_scope(self.database_path) as connection:
            self._require_event(connection, event_id)
            rows = connection.execute(
                "SELECT id FROM schedule_items WHERE event_id = ? ORDER BY order_index, id",
                (event_id,),
            ).fetchall()
            expected = {int(row["id"]) for row in rows}
            if len(item_ids) != len(expected) or set(item_ids) != expected:
                raise ValueError("Reorder must include every schedule item exactly once.")
            self._record_undo(connection, event_id)
            for item_id in item_ids:
                connection.execute(
                    "UPDATE schedule_items SET order_index = -order_index - 1 WHERE id = ?",
                    (item_id,),
                )
            for order, item_id in enumerate(item_ids):
                connection.execute(
                    "UPDATE schedule_items SET order_index = ? WHERE id = ? AND event_id = ?",
                    (order, item_id, event_id),
                )
            self._increment_version(connection, event_id)
            self._audit(connection, event_id, "schedule_reordered", {"ids": sorted(expected)}, {"ids": item_ids}, operator)
        return self.get_event(event_id)

    def advance(self, event_id: int, direction: int = 1, item_id: int | None = None) -> dict[str, Any]:
        """Move the current item, skipping entries that cannot be announced."""
        if direction not in {-1, 1}:
            raise ValueError("Direction must be -1 or 1.")
        with connection_scope(self.database_path) as connection:
            event = self._require_event(connection, event_id)
            rows = connection.execute(
                "SELECT * FROM schedule_items WHERE event_id = ? ORDER BY order_index, id",
                (event_id,),
            ).fetchall()
            eligible = [
                row for row in rows
                if row["status"] not in {"absent", "removed", "skipped"}
            ]
            if item_id is not None:
                indexes = [index for index, row in enumerate(eligible) if int(row["id"]) == item_id]
                if not indexes:
                    raise ValueError("Selected item is not available to announce.")
                next_index = indexes[0]
                candidates = eligible
            else:
                current_order = next(
                    (index for index, row in enumerate(rows)
                     if int(row["id"]) == event["current_item_id"]),
                    -1 if direction > 0 else len(rows),
                )
                candidates = [
                    row for index, row in enumerate(rows)
                    if row["status"] not in {"absent", "removed", "skipped"}
                    and (direction < 0 or row["status"] != "done")
                    and (index > current_order if direction > 0 else index < current_order)
                ]
                if direction < 0:
                    candidates.reverse()
                next_index = 0
            if next_index < 0 or next_index >= len(candidates):
                return self.get_event(event_id)
            next_row = candidates[next_index]
            self._record_undo(connection, event_id)
            current_id = event["current_item_id"]
            if current_id is not None and int(current_id) != int(next_row["id"]):
                connection.execute(
                    """UPDATE schedule_items SET status = 'done' WHERE id = ?
                       AND status IN ('on_stage', 'pending', 'arrived')""",
                    (current_id,),
                )
            connection.execute(
                "UPDATE schedule_items SET status = 'on_stage' WHERE id = ?", (next_row["id"],)
            )
            connection.execute(
                """UPDATE event_control SET current_item_id = ?, status = 'running',
                   state_version = state_version + 1 WHERE id = ?""",
                (next_row["id"], event_id),
            )
            self._audit(
                connection, event_id, "item_advanced",
                {"current_item_id": current_id}, {"current_item_id": int(next_row["id"])}, "operator",
            )
        return self.get_event(event_id)

    def start(self, event_id: int) -> dict[str, Any]:
        """Start an event and move to its first eligible schedule item."""
        event = self.get_event(event_id)
        if event["current_item_id"] is None:
            if not event["items"]:
                return self.update_event(event_id, {"status": "running"})
            return self.advance(event_id, 1)
        with connection_scope(self.database_path) as connection:
            current = self._require_event(connection, event_id)
            item = self._require_item(
                connection, event_id, int(current["current_item_id"])
            )
            self._record_undo(connection, event_id)
            if item["status"] in {"pending", "arrived"}:
                connection.execute(
                    "UPDATE schedule_items SET status = 'on_stage' WHERE id = ?",
                    (item["id"],),
                )
            connection.execute(
                "UPDATE event_control SET status = 'running', state_version = state_version + 1 WHERE id = ?",
                (event_id,),
            )
            self._audit(
                connection, event_id, "started",
                {"status": current["status"]}, {"status": "running"}, "operator",
            )
        return self.get_event(event_id)

    def previous(self, event_id: int) -> dict[str, Any]:
        """Move the current event cursor to the previous eligible item."""
        return self.advance(event_id, -1)

    def undo(self, event_id: int, operator: str = "operator") -> dict[str, Any]:
        """Restore the most recent operator-edit snapshot as a new version."""
        with connection_scope(self.database_path) as connection:
            event = self._require_event(connection, event_id)
            row = connection.execute(
                "SELECT id, snapshot FROM event_undo WHERE event_id = ? ORDER BY id DESC LIMIT 1",
                (event_id,),
            ).fetchone()
            if not row:
                raise ValueError("There is no event change to undo.")
            snapshot = json.loads(row["snapshot"])
            connection.execute("DELETE FROM event_undo WHERE id = ?", (row["id"],))
            connection.execute(
                "UPDATE schedule_items SET order_index = -order_index - 1 WHERE event_id = ?",
                (event_id,),
            )
            for key, value in snapshot["event"].items():
                if key not in {"id", "session_id", "state_version", "created_at"}:
                    connection.execute(f"UPDATE event_control SET {key} = ? WHERE id = ?", (value, event_id))
            snapshot_ids = set()
            for item in snapshot["items"]:
                item_id = int(item["id"])
                snapshot_ids.add(item_id)
                columns = [key for key in item if key != "event_id"]
                connection.execute(
                    f"UPDATE schedule_items SET {', '.join(f'{key} = ?' for key in columns)} WHERE id = ?",
                    (*(item[key] for key in columns), item_id),
                )
            if snapshot_ids:
                placeholders = ",".join("?" for _ in snapshot_ids)
                connection.execute(
                    f"UPDATE schedule_items SET status = 'removed' WHERE event_id = ? AND id NOT IN ({placeholders})",
                    (event_id, *snapshot_ids),
                )
                connection.execute(
                    f"DELETE FROM event_summaries WHERE event_id = ? AND item_id NOT IN ({placeholders})",
                    (event_id, *snapshot_ids),
                )
            else:
                connection.execute(
                    "UPDATE schedule_items SET status = 'removed' WHERE event_id = ?", (event_id,)
                )
                connection.execute(
                    "DELETE FROM event_summaries WHERE event_id = ?", (event_id,)
                )
            for item in snapshot["items"]:
                if item["summary"]:
                    connection.execute(
                        """INSERT INTO event_summaries(event_id, item_id, summary)
                           VALUES (?, ?, ?) ON CONFLICT(event_id, item_id)
                           DO UPDATE SET summary = excluded.summary""",
                        (event_id, item["id"], item["summary"]),
                    )
                else:
                    connection.execute(
                        "DELETE FROM event_summaries WHERE event_id = ? AND item_id = ?",
                        (event_id, item["id"]),
                    )
            connection.execute(
                "UPDATE event_control SET state_version = ? WHERE id = ?",
                (int(event["state_version"]) + 1, event_id),
            )
            self._audit(connection, event_id, "undo", {}, {"restored": True}, operator)
        return self.get_event(event_id)

    def append_transcript(self, event_id: int, item_id: int, text: str) -> dict[str, Any]:
        """Append a transcribed or operator-provided transcript segment."""
        segment = text.strip()
        if not segment:
            raise ValueError("Transcript text cannot be empty.")
        with connection_scope(self.database_path) as connection:
            self._require_item(connection, event_id, item_id)
            self._record_undo(connection, event_id)
            connection.execute(
                "UPDATE schedule_items SET transcript = trim(transcript || ' ' || ?) WHERE id = ?",
                (segment, item_id),
            )
            self._increment_version(connection, event_id)
            self._audit(connection, event_id, "transcript_appended", {}, {"item_id": item_id, "characters": len(segment)}, "operator")
        return self.get_event(event_id)

    def save_summary(self, event_id: int, item_id: int, summary: str) -> dict[str, Any]:
        """Store a reviewed speaker summary with the schedule item."""
        with connection_scope(self.database_path) as connection:
            self._require_item(connection, event_id, item_id)
            self._record_undo(connection, event_id)
            connection.execute("UPDATE schedule_items SET summary = ? WHERE id = ?", (summary.strip(), item_id))
            connection.execute(
                """INSERT INTO event_summaries(event_id, item_id, summary) VALUES (?, ?, ?)
                   ON CONFLICT(event_id, item_id) DO UPDATE SET summary = excluded.summary,
                   created_at = CURRENT_TIMESTAMP""",
                (event_id, item_id, summary.strip()),
            )
            self._increment_version(connection, event_id)
            self._audit(connection, event_id, "summary_saved", {}, {"item_id": item_id}, "operator")
        return self.get_event(event_id)

    def set_transcript(self, event_id: int, item_id: int, transcript: str) -> dict[str, Any]:
        """Replace a transcript after operator correction."""
        with connection_scope(self.database_path) as connection:
            self._require_item(connection, event_id, item_id)
            self._record_undo(connection, event_id)
            connection.execute(
                "UPDATE schedule_items SET transcript = ? WHERE id = ?",
                (transcript.strip(), item_id),
            )
            self._increment_version(connection, event_id)
            self._audit(connection, event_id, "transcript_edited", {}, {"item_id": item_id}, "operator")
        return self.get_event(event_id)

    def set_auto_speak(self, session_id: str, enabled: bool) -> None:
        """Persist the explicit operator preference for auto-speaking summaries."""
        with connection_scope(self.database_path) as connection:
            connection.execute(
                """INSERT INTO event_settings(session_id, auto_speak_summary) VALUES (?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET auto_speak_summary = excluded.auto_speak_summary""",
                (session_id, int(enabled)),
            )

    def get_auto_speak(self, session_id: str) -> bool:
        """Read summary auto-speak preference; operator approval is the default."""
        with connection_scope(self.database_path) as connection:
            row = connection.execute(
                "SELECT auto_speak_summary FROM event_settings WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return bool(row["auto_speak_summary"]) if row else False

    def replan_upcoming(self, event_id: int) -> str:
        """Construct a deterministic transition from live current/next schedule facts."""
        event = self.get_event(event_id)
        next_item = next(
            (item for item in event["items"]
             if item["order_index"] > self._current_order(event) and item["status"] in {"arrived", "pending"}),
            None,
        )
        if next_item is None:
            return "There are no further scheduled items."
        current = next(
            (item for item in event["items"] if item["id"] == event["current_item_id"]),
            None,
        )
        name = next_item["person_name"].strip()
        presentation = f" Please welcome {name}" if name else ""
        if next_item["designation"]:
            presentation += f", {next_item['designation']}"
        gratitude = ""
        if current and current["type"] == "speech" and current["summary"]:
            speaker = current["person_name"].strip() or "our speaker"
            topic = current["summary"].split(".")[0].strip()
            gratitude = f"Thank you, {speaker}, for those insights on {topic}. "
        return f"{gratitude}Let us now move to {next_item['title']}.{presentation}."

    def export_markdown(self, event_id: int) -> str:
        """Build a full Markdown event report from the authoritative live state."""
        event = self.get_event(event_id)
        lines = [
            f"# {event['name']}",
            "",
            f"- Type: {event['type']}",
            f"- Date: {event['date'] or 'Not set'}",
            f"- Venue: {event['venue'] or 'Not set'}",
            f"- Status: {event['status']}",
            f"- State version: {event['state_version']}",
            "",
            "## Schedule and attendance",
            "",
            "| Order | Item | Type | Person | Designation | Status | Duration (min) |",
            "|---:|---|---|---|---|---|---:|",
        ]
        for item in event["items"]:
            lines.append(
                f"| {item['order_index'] + 1} | {item['title']} | {item['type']} | "
                f"{item['person_name']} | {item['designation']} | {item['status']} | "
                f"{item['planned_duration']} |"
            )
            if item["summary"]:
                lines.extend(["", f"### {item['person_name'] or item['title']} — summary", "", item["summary"]])
        lines.extend(["", "## Change log", ""])
        for item in reversed(event["log"]):
            lines.append(f"- {item['timestamp']} — {item['operator']}: {item['action']} ({item['new_value']})")
        return "\n".join(lines)

    @staticmethod
    def _current_order(event: dict[str, Any]) -> int:
        current = next((item for item in event["items"] if item["id"] == event["current_item_id"]), None)
        return current["order_index"] if current else -1

    @staticmethod
    def _validate_language(language: str) -> None:
        if language not in {"en", "ml"}:
            raise ValueError("Event language must be 'en' or 'ml'.")

    @staticmethod
    def _validate_item(item: dict[str, Any], partial: bool = False) -> None:
        if not partial and not str(item.get("title", "")).strip():
            raise ValueError("Schedule item title cannot be empty.")
        if "title" in item and not str(item["title"]).strip():
            raise ValueError("Schedule item title cannot be empty.")
        if "status" in item and item["status"] not in ITEM_STATUSES:
            raise ValueError("Invalid schedule item status.")
        if "planned_duration" in item and int(item["planned_duration"]) < 0:
            raise ValueError("Planned duration cannot be negative.")

    @staticmethod
    def _insert_item(
        connection: sqlite3.Connection, event_id: int, item: dict[str, Any], order_index: int
    ) -> int:
        EventService._validate_item(item)
        cursor = connection.execute(
            """INSERT INTO schedule_items(
                event_id, order_index, title, type, person_name, designation, status,
                planned_duration, notes, transcript_language
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id, order_index, str(item["title"]).strip(), item.get("type", "other"),
                item.get("person_name", item.get("participant", "")), item.get("designation", ""),
                item.get("status", "pending"), int(item.get("planned_duration", 0)),
                item.get("notes", ""), item.get("transcript_language", "auto"),
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _require_event(connection: sqlite3.Connection, event_id: int) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM event_control WHERE id = ?", (event_id,)).fetchone()
        if not row:
            raise EventNotFoundError(f"Event {event_id} was not found.")
        return row

    @staticmethod
    def _require_item(connection: sqlite3.Connection, event_id: int, item_id: int) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM schedule_items WHERE id = ? AND event_id = ?", (item_id, event_id)
        ).fetchone()
        if not row:
            raise EventNotFoundError(f"Schedule item {item_id} was not found.")
        return row

    @staticmethod
    def _is_current(connection: sqlite3.Connection, event_id: int, item_id: int) -> bool:
        row = connection.execute(
            "SELECT current_item_id FROM event_control WHERE id = ?", (event_id,)
        ).fetchone()
        return bool(row and row["current_item_id"] == item_id)

    @staticmethod
    def _increment_version(connection: sqlite3.Connection, event_id: int) -> None:
        connection.execute(
            "UPDATE event_control SET state_version = state_version + 1 WHERE id = ?", (event_id,)
        )

    @staticmethod
    def _snapshot(connection: sqlite3.Connection, event_id: int) -> str:
        event = dict(EventService._require_event(connection, event_id))
        items = [
            dict(row) for row in connection.execute(
                "SELECT * FROM schedule_items WHERE event_id = ? ORDER BY order_index, id",
                (event_id,),
            ).fetchall()
        ]
        return json.dumps({"event": event, "items": items}, ensure_ascii=False)

    @staticmethod
    def _record_undo(connection: sqlite3.Connection, event_id: int) -> None:
        connection.execute(
            "INSERT INTO event_undo(event_id, snapshot) VALUES (?, ?)",
            (event_id, EventService._snapshot(connection, event_id)),
        )

    @staticmethod
    def _audit(
        connection: sqlite3.Connection, event_id: int, action: str,
        old: dict[str, Any], new: dict[str, Any], operator: str,
    ) -> None:
        connection.execute(
            """INSERT INTO event_log(event_id, action, old_value, new_value, operator)
               VALUES (?, ?, ?, ?, ?)""",
            (
                event_id, action, json.dumps(old, ensure_ascii=False, default=str),
                json.dumps(new, ensure_ascii=False, default=str), operator[:120],
            ),
        )


def blocked_names(event: dict[str, Any]) -> list[str]:
    """Return names that must never be included in an upcoming announcement."""
    names = {
        str(item["person_name"]).strip()
        for item in event.get("items", [])
        if item["status"] in {"absent", "removed", "skipped"} and str(item["person_name"]).strip()
    }
    return sorted(names, key=len, reverse=True)


def contains_blocked_name(text: str, names: list[str]) -> str | None:
    """Find a removed/absent name as a whole token sequence, case-insensitively."""
    for name in names:
        pattern = rf"(?<!\w){re.escape(name)}(?!\w)"
        if re.search(pattern, text, re.IGNORECASE):
            return name
    return None


def parse_operator_command(message: str, event: dict[str, Any]) -> dict[str, Any] | None:
    """Parse an explicit absent/remove command only when it uniquely matches a person."""
    normalized = message.casefold()
    if not any(cue in normalized for cue in ("not arrived", "hasn't arrived", "has not arrived", "remove him", "remove her", "not coming", "absent")):
        return None
    candidates = [
        item for item in event.get("items", [])
        if item["status"] in {"pending", "arrived", "on_stage"} and item["person_name"]
        and str(item["person_name"]).casefold() in normalized
    ]
    unique_names = {str(item["person_name"]).casefold() for item in candidates}
    if len(unique_names) != 1:
        return None
    item = min(candidates, key=lambda candidate: candidate["order_index"])
    return {"action": "set_absent", "item_id": item["id"], "person_name": item["person_name"]}
