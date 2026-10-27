"""SQLite schema management for AVA's persistent and legacy event data."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from config.settings import AppSettings, load_settings


def get_connection(database_path: str | Path | None = None) -> sqlite3.Connection:
    """Open a configured SQLite connection with foreign keys enabled."""
    selected_path = Path(database_path) if database_path else load_settings().database_path
    selected_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(selected_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def connection_scope(
    database_path: str | Path | None = None,
) -> Generator[sqlite3.Connection, None, None]:
    """Yield a connection, commit on success, roll back on failure, and always close."""
    connection = get_connection(database_path)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database(
    settings: AppSettings | None = None,
    database_path: str | Path | None = None,
) -> None:
    """Create AVA's tables and add memory columns to the original event schema."""
    config = settings or load_settings()
    target = Path(database_path) if database_path else config.database_path
    with connection_scope(target) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                active_event_id INTEGER,
                active_story_id TEXT,
                rolling_summary TEXT NOT NULL DEFAULT '',
                summary_through_id INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS messages_session_recent
                ON messages(session_id, id DESC);

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                venue TEXT,
                event_date TEXT,
                event_start_time TEXT NOT NULL DEFAULT '',
                event_end_time TEXT NOT NULL DEFAULT '',
                event_type TEXT NOT NULL DEFAULT 'event',
                host TEXT NOT NULL DEFAULT '',
                sponsors_json TEXT NOT NULL DEFAULT '[]',
                audience TEXT NOT NULL DEFAULT '',
                style TEXT NOT NULL DEFAULT '',
                special_instructions TEXT NOT NULL DEFAULT '',
                chief_guest TEXT NOT NULL DEFAULT '',
                chief_guest_title TEXT NOT NULL DEFAULT '',
                speakers_json TEXT NOT NULL DEFAULT '[]',
                schedule_json TEXT NOT NULL DEFAULT '[]',
                tone TEXT NOT NULL DEFAULT 'Professional',
                current_item_index INTEGER NOT NULL DEFAULT 0,
                session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                event_time TEXT NOT NULL,
                title TEXT NOT NULL,
                event_type TEXT,
                participant TEXT,
                designation TEXT NOT NULL DEFAULT '',
                participant_details TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                duration_minutes INTEGER NOT NULL DEFAULT 0,
                performance_style TEXT NOT NULL DEFAULT 'event_style'
            );

            CREATE TABLE IF NOT EXISTS stories (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                characters_json TEXT NOT NULL DEFAULT '[]',
                setting TEXT NOT NULL DEFAULT '',
                plot_summary TEXT NOT NULL DEFAULT '',
                last_paragraph TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS stories_session_recent
                ON stories(session_id, updated_at DESC);

            CREATE TABLE IF NOT EXISTS preferences (
                session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
                language TEXT NOT NULL DEFAULT 'en',
                style TEXT NOT NULL DEFAULT 'Professional',
                voice TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS event_control (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'event',
                date TEXT NOT NULL DEFAULT '',
                event_start_time TEXT NOT NULL DEFAULT '',
                event_end_time TEXT NOT NULL DEFAULT '',
                venue TEXT NOT NULL DEFAULT '',
                host TEXT NOT NULL DEFAULT '',
                sponsors_json TEXT NOT NULL DEFAULT '[]',
                audience TEXT NOT NULL DEFAULT '',
                style TEXT NOT NULL DEFAULT '',
                special_instructions TEXT NOT NULL DEFAULT '',
                tone TEXT NOT NULL DEFAULT 'Professional',
                language TEXT NOT NULL DEFAULT 'en',
                status TEXT NOT NULL DEFAULT 'not_started'
                    CHECK (status IN ('not_started', 'running', 'paused', 'ended')),
                chief_guest TEXT NOT NULL DEFAULT '',
                chief_guest_title TEXT NOT NULL DEFAULT '',
                current_item_id INTEGER,
                transcript TEXT NOT NULL DEFAULT '',
                event_summary TEXT NOT NULL DEFAULT '',
                summary_played INTEGER NOT NULL DEFAULT 0
                    CHECK (summary_played IN (0, 1)),
                state_version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS schedule_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL REFERENCES event_control(id) ON DELETE CASCADE,
                order_index INTEGER NOT NULL,
                title TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'other',
                person_name TEXT NOT NULL DEFAULT '',
                designation TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'arrived', 'absent', 'on_stage',
                                      'done', 'skipped', 'removed')),
                planned_duration INTEGER NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '',
                scheduled_time TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                participant_details TEXT NOT NULL DEFAULT '',
                performance_style TEXT NOT NULL DEFAULT 'event_style',
                transcript TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                transcript_language TEXT NOT NULL DEFAULT 'auto',
                UNIQUE(event_id, order_index)
            );
            CREATE INDEX IF NOT EXISTS schedule_items_event_order
                ON schedule_items(event_id, order_index);

            CREATE TABLE IF NOT EXISTS event_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL REFERENCES event_control(id) ON DELETE CASCADE,
                timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                action TEXT NOT NULL,
                old_value TEXT NOT NULL DEFAULT '{}',
                new_value TEXT NOT NULL DEFAULT '{}',
                operator TEXT NOT NULL DEFAULT 'operator'
            );
            CREATE INDEX IF NOT EXISTS event_log_recent
                ON event_log(event_id, id DESC);

            CREATE TABLE IF NOT EXISTS event_undo (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL REFERENCES event_control(id) ON DELETE CASCADE,
                snapshot TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS event_undo_recent
                ON event_undo(event_id, id DESC);

            CREATE TABLE IF NOT EXISTS event_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL REFERENCES event_control(id) ON DELETE CASCADE,
                item_id INTEGER NOT NULL REFERENCES schedule_items(id) ON DELETE CASCADE,
                summary TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(event_id, item_id)
            );

            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS event_settings (
                session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
                auto_speak_summary INTEGER NOT NULL DEFAULT 1
                    CHECK (auto_speak_summary IN (0, 1))
            );

            CREATE TABLE IF NOT EXISTS event_scripts (
                event_id INTEGER PRIMARY KEY REFERENCES event_control(id) ON DELETE CASCADE,
                segments_json TEXT NOT NULL DEFAULT '[]',
                version INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS event_script_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL REFERENCES event_control(id) ON DELETE CASCADE,
                segments_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS event_script_history_recent
                ON event_script_history(event_id, id DESC);

            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                message_id UNINDEXED,
                session_id UNINDEXED,
                content,
                tokenize='unicode61'
            );

            CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(message_id, session_id, content)
                VALUES (new.id, new.session_id, new.content);
            END;
            CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
                DELETE FROM messages_fts WHERE message_id = old.id;
            END;
            CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE OF content ON messages BEGIN
                DELETE FROM messages_fts WHERE message_id = old.id;
                INSERT INTO messages_fts(message_id, session_id, content)
                VALUES (new.id, new.session_id, new.content);
            END;
            """
        )

        existing_event_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(events)")
        }
        migrations = {
            "event_type": "TEXT NOT NULL DEFAULT 'event'",
            "event_start_time": "TEXT NOT NULL DEFAULT ''",
            "event_end_time": "TEXT NOT NULL DEFAULT ''",
            "host": "TEXT NOT NULL DEFAULT ''",
            "sponsors_json": "TEXT NOT NULL DEFAULT '[]'",
            "audience": "TEXT NOT NULL DEFAULT ''",
            "style": "TEXT NOT NULL DEFAULT ''",
            "special_instructions": "TEXT NOT NULL DEFAULT ''",
            "chief_guest": "TEXT NOT NULL DEFAULT ''",
            "chief_guest_title": "TEXT NOT NULL DEFAULT ''",
            "speakers_json": "TEXT NOT NULL DEFAULT '[]'",
            "schedule_json": "TEXT NOT NULL DEFAULT '[]'",
            "tone": "TEXT NOT NULL DEFAULT 'Professional'",
            "current_item_index": "INTEGER NOT NULL DEFAULT 0",
            "session_id": "TEXT REFERENCES sessions(id) ON DELETE SET NULL",
            "created_at": "TEXT NOT NULL DEFAULT ''",
        }
        for column, definition in migrations.items():
            if column not in existing_event_columns:
                connection.execute(f"ALTER TABLE events ADD COLUMN {column} {definition}")
        if "created_at" not in existing_event_columns:
            connection.execute(
                "UPDATE events SET created_at = CURRENT_TIMESTAMP WHERE created_at = ''"
            )

        schedule_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(schedule)")
        }
        for column, definition in {
            "designation": "TEXT NOT NULL DEFAULT ''",
            "participant_details": "TEXT NOT NULL DEFAULT ''",
            "location": "TEXT NOT NULL DEFAULT ''",
            "duration_minutes": "INTEGER NOT NULL DEFAULT 0",
            "performance_style": "TEXT NOT NULL DEFAULT 'event_style'",
        }.items():
            if column not in schedule_columns:
                connection.execute(f"ALTER TABLE schedule ADD COLUMN {column} {definition}")

        control_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(event_control)")
        }
        for column, definition in {
            "event_start_time": "TEXT NOT NULL DEFAULT ''",
            "event_end_time": "TEXT NOT NULL DEFAULT ''",
            "chief_guest_title": "TEXT NOT NULL DEFAULT ''",
            "host": "TEXT NOT NULL DEFAULT ''",
            "sponsors_json": "TEXT NOT NULL DEFAULT '[]'",
            "audience": "TEXT NOT NULL DEFAULT ''",
            "style": "TEXT NOT NULL DEFAULT ''",
            "special_instructions": "TEXT NOT NULL DEFAULT ''",
            "transcript": "TEXT NOT NULL DEFAULT ''",
            "event_summary": "TEXT NOT NULL DEFAULT ''",
            "summary_played": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if column not in control_columns:
                connection.execute(f"ALTER TABLE event_control ADD COLUMN {column} {definition}")

        schedule_item_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(schedule_items)")
        }
        for column, definition in {
            "scheduled_time": "TEXT NOT NULL DEFAULT ''",
            "location": "TEXT NOT NULL DEFAULT ''",
            "participant_details": "TEXT NOT NULL DEFAULT ''",
            "performance_style": "TEXT NOT NULL DEFAULT 'event_style'",
        }.items():
            if column not in schedule_item_columns:
                connection.execute(
                    f"ALTER TABLE schedule_items ADD COLUMN {column} {definition}"
                )

        session_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(sessions)")
        }
        if "summary_through_id" not in session_columns:
            connection.execute(
                "ALTER TABLE sessions ADD COLUMN summary_through_id INTEGER NOT NULL DEFAULT 0"
            )

        connection.execute(
            """
            INSERT INTO messages_fts(message_id, session_id, content)
            SELECT m.id, m.session_id, m.content FROM messages m
            WHERE NOT EXISTS (
                SELECT 1 FROM messages_fts f WHERE f.message_id = CAST(m.id AS TEXT)
            )
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO schema_meta(key, value)
            VALUES ('event_control_version', '1')
            """
        )
