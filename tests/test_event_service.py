"""Regression tests for versioned event operations and announcement safety."""

from pathlib import Path

import pytest

from memory.repository import MemoryRepository
from services.event_service import (
    EventService,
    blocked_names,
    contains_blocked_name,
    drop_blocked_name_sentences,
)
from modules.tts import SpeechSynthesizer


def make_service(tmp_path: Path) -> tuple[EventService, int]:
    repository = MemoryRepository(tmp_path / "events.sqlite3")
    session_id = repository.create_session("event-control-session")
    service = EventService(repository.database_path)
    event = service.create_event(
        session_id,
        name="Arts day",
        schedule=[
            {"title": "Opening", "type": "welcome"},
            {"title": "Guest address", "type": "speech", "person_name": "Dr. Nair"},
            {"title": "Music", "type": "performance"},
        ],
    )
    return service, event["id"]


def test_absent_removed_reorder_and_undo_are_versioned(tmp_path: Path) -> None:
    service, event_id = make_service(tmp_path)
    initial = service.get_event(event_id)
    opening, speaker, performance = initial["items"]

    marked_absent = service.update_item(event_id, speaker["id"], {"status": "absent"})
    assert marked_absent["state_version"] == initial["state_version"] + 1
    assert "Dr. Nair" in blocked_names(marked_absent)
    assert contains_blocked_name("Please welcome Dr. Nair", blocked_names(marked_absent)) == "Dr. Nair"

    restored = service.undo(event_id)
    assert restored["items"][1]["status"] == "pending"
    assert restored["state_version"] == marked_absent["state_version"] + 1

    removed = service.delete_item(event_id, speaker["id"])
    assert removed["items"][1]["status"] == "removed"
    reordered = service.reorder(
        event_id, [performance["id"], speaker["id"], opening["id"]]
    )
    assert [item["id"] for item in reordered["items"]] == [
        performance["id"], speaker["id"], opening["id"]
    ]
    undone = service.undo(event_id)
    assert [item["id"] for item in undone["items"]] == [
        opening["id"], speaker["id"], performance["id"]
    ]
    assert undone["items"][1]["status"] == "removed"
    previous = service.undo(event_id)
    assert [item["id"] for item in previous["items"]] == [
        opening["id"], speaker["id"], performance["id"]
    ]
    assert previous["items"][1]["status"] == "pending"
    assert any(row["action"] == "undo" for row in previous["log"])


def test_current_item_removal_stops_cursor_and_advances(tmp_path: Path) -> None:
    service, event_id = make_service(tmp_path)
    event = service.start(event_id)
    _, second = event["items"][:2]
    event = service.advance(event_id, item_id=second["id"])
    assert event["current_item_id"] == second["id"]
    updated = service.update_item(event_id, second["id"], {"status": "absent"})
    assert updated["current_item_id"] == updated["items"][2]["id"]
    assert updated["items"][2]["status"] == "on_stage"


def test_late_arrival_can_be_inserted_at_requested_position(tmp_path: Path) -> None:
    service, event_id = make_service(tmp_path)
    event = service.add_item(
        event_id,
        {"title": "Late guest", "type": "speech", "person_name": "Dr. Rao"},
        position=1,
    )
    assert [item["title"] for item in event["items"]] == [
        "Opening", "Late guest", "Guest address", "Music"
    ]


def test_cache_key_changes_with_event_state_version(tmp_path: Path) -> None:
    synthesizer = SpeechSynthesizer(cache_dir=tmp_path / "audio")
    first = synthesizer._cache_path("Welcome", "en", "en-IN-NeerjaNeural", "+0%", 1)
    second = synthesizer._cache_path("Welcome", "en", "en-IN-NeerjaNeural", "+0%", 2)
    assert first != second


def test_command_parser_requires_one_matching_speaker(tmp_path: Path) -> None:
    service, event_id = make_service(tmp_path)
    event = service.get_event(event_id)
    from services.event_service import parse_operator_command

    proposal = parse_operator_command("Dr. Nair has not arrived, remove him", event)
    assert proposal == {
        "action": "set_absent",
        "item_id": event["items"][1]["id"],
        "person_name": "Dr. Nair",
    }
    assert parse_operator_command("Nobody has arrived", event) is None


def test_absence_applies_to_all_future_items_for_same_person(tmp_path: Path) -> None:
    service, event_id = make_service(tmp_path)
    event = service.add_item(
        event_id,
        {"title": "Second address", "type": "speech", "person_name": "Dr. Nair"},
    )
    first_speech = next(item for item in event["items"] if item["person_name"] == "Dr. Nair")
    updated = service.update_item(event_id, first_speech["id"], {"status": "absent"})
    assert [
        item["status"] for item in updated["items"] if item["person_name"] == "Dr. Nair"
    ] == ["absent", "absent"]


def test_blocked_names_drop_only_sentences_that_mention_them() -> None:
    output = drop_blocked_name_sentences(
        "Welcome, everyone. Please welcome Dr. Nair. We will begin with music!",
        ["Dr. Nair"],
    )
    assert output == "Welcome, everyone. We will begin with music!"


def test_safe_fallback_removes_absent_name_even_if_in_another_title() -> None:
    from orchestrator.pipeline import ConversationPipeline

    event = {
        "current_item_id": 2,
        "items": [
            {"id": 2, "title": "Dr. Nair's address", "status": "pending"},
            {"id": 3, "title": "Guest address", "person_name": "Dr. Nair", "status": "absent"},
        ],
    }
    output = ConversationPipeline._safe_event_fallback(event, "en")
    assert "Dr. Nair" not in output
    assert "address" in output


def test_invalid_reorder_is_rejected(tmp_path: Path) -> None:
    service, event_id = make_service(tmp_path)
    with pytest.raises(ValueError, match="every schedule item exactly once"):
        service.reorder(event_id, [service.get_event(event_id)["items"][0]["id"]])
