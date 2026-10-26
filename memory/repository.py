"""Typed persistence operations for conversations, events, stories, and preferences."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from memory.db import connection_scope, initialize_database

RowData = dict[str, Any]


class MemoryRepository:
    """Repository for durable AVA conversation and event state."""

    def __init__(self, database_path: str | Path | None = None) -> None:
        self.database_path = Path(database_path) if database_path else None
        initialize_database(database_path=self.database_path)

    def _connection(self) -> AbstractContextManager[sqlite3.Connection]:
        return connection_scope(self.database_path)

    def create_session(self, session_id: str | None = None) -> str:
        """Create a session or return the supplied session identifier."""
        identifier = session_id or str(uuid.uuid4())
        with self._connection() as connection:
            connection.execute("INSERT OR IGNORE INTO sessions(id) VALUES (?)", (identifier,))
        return identifier

    def session_exists(self, session_id: str) -> bool:
        """Check whether a session has been persisted."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return row is not None

    def add_message(self, session_id: str, role: str, content: str) -> int:
        """Persist one message and return its row identifier."""
        if role not in {"system", "user", "assistant"}:
            raise ValueError(f"Unsupported conversation role: {role}")
        if not content.strip():
            raise ValueError("Message content cannot be empty.")
        self.create_session(session_id)
        with self._connection() as connection:
            cursor = connection.execute(
                "INSERT INTO messages(session_id, role, content) VALUES (?, ?, ?)",
                (session_id, role, content.strip()),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (session_id,),
            )
            return int(cursor.lastrowid)

    def get_recent_messages(self, session_id: str, limit: int = 12) -> list[RowData]:
        """Return the newest conversation messages in chronological order."""
        if limit < 1:
            raise ValueError("Message limit must be positive.")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, role, content, created_at FROM messages
                WHERE session_id = ? ORDER BY id DESC LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def search_messages(
        self, session_id: str, query: str, limit: int = 5
    ) -> list[RowData]:
        """Search older messages by FTS5 relevance and recency."""
        terms = [term.replace('"', '""') for term in query.split() if term.isalnum()]
        if not terms:
            return []
        match_expression = " OR ".join(f'"{term}"' for term in terms)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT m.id, m.role, m.content, m.created_at
                FROM messages_fts f
                JOIN messages m ON m.id = CAST(f.message_id AS INTEGER)
                WHERE f.session_id = ? AND messages_fts MATCH ?
                ORDER BY bm25(messages_fts), m.id DESC
                LIMIT ?
                """,
                (session_id, match_expression, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_summary(self, session_id: str) -> str:
        """Return a session's rolling summary, if one exists."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT rolling_summary FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return str(row["rolling_summary"]) if row else ""

    def set_summary(
        self, session_id: str, summary: str, through_message_id: int = 0
    ) -> None:
        """Persist the latest rolling summary."""
        self.create_session(session_id)
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE sessions SET rolling_summary = ?, summary_through_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (summary.strip(), through_message_id, session_id),
            )

    def get_summary_cursor(self, session_id: str) -> int:
        """Return the last message ID represented by the rolling summary."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT summary_through_id FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return int(row["summary_through_id"]) if row else 0

    def get_messages_after(
        self, session_id: str, after_id: int, limit: int = 100
    ) -> list[RowData]:
        """Load a bounded range of conversation messages after the summary cursor."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, role, content, created_at FROM messages
                WHERE session_id = ? AND id > ? ORDER BY id LIMIT ?
                """,
                (session_id, after_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_message_count(self, session_id: str) -> int:
        """Return the total number of persisted messages in a session."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return int(row["count"]) if row else 0

    def get_message_character_count(self, session_id: str) -> int:
        """Return persisted conversation size for approximate token-budget checks."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(LENGTH(content)), 0) AS characters "
                "FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return int(row["characters"]) if row else 0

    def set_active_state(
        self,
        session_id: str,
        *,
        story_id: str | None = None,
        event_id: int | None = None,
    ) -> None:
        """Record the active story or event selected in this session."""
        self.create_session(session_id)
        with self._connection() as connection:
            updates: list[str] = []
            values: list[Any] = []
            if story_id is not None:
                updates.append("active_story_id = ?")
                values.append(story_id)
            if event_id is not None:
                updates.append("active_event_id = ?")
                values.append(event_id)
            if updates:
                updates.append("updated_at = CURRENT_TIMESTAMP")
                values.append(session_id)
                connection.execute(
                    f"UPDATE sessions SET {', '.join(updates)} WHERE id = ?", values
                )

    def get_active_state(self, session_id: str) -> RowData:
        """Load the active event and story for a session."""
        with self._connection() as connection:
            session = connection.execute(
                "SELECT active_event_id, active_story_id FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not session:
                return {"event": None, "story": None}
            event = (
                connection.execute(
                    "SELECT * FROM events WHERE id = ?", (session["active_event_id"],)
                ).fetchone()
                if session["active_event_id"] is not None
                else None
            )
            story = (
                connection.execute(
                    "SELECT * FROM stories WHERE id = ?", (session["active_story_id"],)
                ).fetchone()
                if session["active_story_id"]
                else None
            )
        return {
            "event": dict(event) if event else None,
            "story": dict(story) if story else None,
        }

    def save_story(
        self,
        session_id: str,
        *,
        title: str,
        characters: list[str],
        setting: str,
        plot_summary: str,
        last_paragraph: str,
        status: str = "active",
        story_id: str | None = None,
    ) -> str:
        """Create or update structured story continuity state."""
        identifier = story_id or str(uuid.uuid4())
        self.create_session(session_id)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO stories(
                    id, session_id, title, characters_json, setting, plot_summary,
                    last_paragraph, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    characters_json=excluded.characters_json,
                    setting=excluded.setting,
                    plot_summary=excluded.plot_summary,
                    last_paragraph=excluded.last_paragraph,
                    status=excluded.status,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    identifier,
                    session_id,
                    title,
                    json.dumps(characters, ensure_ascii=False),
                    setting,
                    plot_summary,
                    last_paragraph,
                    status,
                ),
            )
        self.set_active_state(session_id, story_id=identifier)
        return identifier

    def get_preferences(self, session_id: str) -> RowData:
        """Read session preferences, supplying stable defaults when unset."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT language, style, voice FROM preferences WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return dict(row) if row else {"language": "en", "style": "Professional", "voice": ""}

    def set_preferences(
        self, session_id: str, *, language: str, style: str, voice: str
    ) -> None:
        """Create or replace the selected language, style, and voice."""
        self.create_session(session_id)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO preferences(session_id, language, style, voice)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    language=excluded.language, style=excluded.style,
                    voice=excluded.voice, updated_at=CURRENT_TIMESTAMP
                """,
                (session_id, language, style, voice),
            )
