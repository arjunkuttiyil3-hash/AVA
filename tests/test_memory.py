"""Tests for SQLite-backed AVA memory and follow-up resolution."""

from pathlib import Path
import sqlite3

from context.memory_manager import MemoryManager
from memory.db import initialize_database
from memory.repository import MemoryRepository


def test_story_state_resolves_followup_and_persists(tmp_path: Path) -> None:
    database_path = tmp_path / "ava.sqlite3"
    repository = MemoryRepository(database_path)
    manager = MemoryManager(repository)
    session_id = repository.create_session("story-test")
    repository.add_message(
        session_id, "user", "Create a story about a robot in Kerala"
    )
    repository.save_story(
        session_id,
        title="Kuttan the Robot",
        characters=["Kuttan"],
        setting="Kerala",
        plot_summary="A robot explores Kerala.",
        last_paragraph="Kuttan rolled down a path beside the paddy fields.",
    )
    repository.add_message(
        session_id,
        "assistant",
        "Kuttan rolled down a path beside the paddy fields.",
    )

    followup = manager.resolve_followup(
        session_id, "Now make it meet a school student"
    )

    assert followup.continues_story is True
    assert followup.active_story is not None
    assert followup.active_story["setting"] == "Kerala"
    assert "Kuttan rolled" in followup.active_story["last_paragraph"]

    restarted_repository = MemoryRepository(database_path)
    restarted = MemoryManager(restarted_repository).resolve_followup(
        session_id, "Now make it meet a school student"
    )
    assert restarted.active_story is not None
    assert restarted.active_story["title"] == "Kuttan the Robot"


def test_recent_history_and_fts_retrieval(tmp_path: Path) -> None:
    repository = MemoryRepository(tmp_path / "ava.sqlite3")
    session_id = repository.create_session("retrieval-test")
    repository.add_message(session_id, "user", "The chief guest is Dr. Menon.")
    repository.add_message(session_id, "assistant", "I will remember the guest.")
    repository.add_message(session_id, "user", "Please introduce the chief guest.")

    results = repository.search_messages(session_id, "chief guest", limit=3)

    assert len(repository.get_recent_messages(session_id, limit=2)) == 2
    assert results
    assert any("Dr. Menon" in item["content"] for item in results)


def test_migration_preserves_original_event_rows(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                venue TEXT,
                event_date TEXT
            );
            CREATE TABLE schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                event_time TEXT NOT NULL,
                title TEXT NOT NULL,
                event_type TEXT,
                participant TEXT
            );
            INSERT INTO events(name, venue, event_date)
            VALUES ('Legacy event', 'Main hall', '2026-10-25');
            """
        )

    initialize_database(database_path=database_path)

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        event = connection.execute("SELECT * FROM events WHERE id = 1").fetchone()
        schedule_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(schedule)")
        }
    assert event["name"] == "Legacy event"
    assert event["created_at"]
    assert {"event_time", "title", "participant"}.issubset(schedule_columns)


def test_history_summary_advances_cursor_only_for_older_messages(tmp_path: Path) -> None:
    repository = MemoryRepository(tmp_path / "summary.sqlite3")
    session_id = repository.create_session("summary-test")
    for index in range(4):
        repository.add_message(
            session_id, "user", f"Important detail {index}: " + ("Kerala " * 30)
        )

    manager = MemoryManager(
        repository,
        short_term_turns=1,
        token_budget=128,
        summarizer=lambda previous, messages: (
            f"{previous} {messages[-1]['content'][:60]}".strip()
        ),
    )

    summary = manager.compact_history(session_id)

    assert "Important detail" in summary
    assert repository.get_summary_cursor(session_id) == 2
