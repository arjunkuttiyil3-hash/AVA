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


def test_talks_support_transcripts_and_summaries_only_while_talk_type(tmp_path: Path) -> None:
    service, event_id = make_service(tmp_path)
    talk_event = service.add_item(
        event_id, {"title": "Community talk", "type": "talk", "person_name": "Asha"}
    )
    talk = next(item for item in talk_event["items"] if item["type"] == "talk")
    service.append_transcript(event_id, talk["id"], "The speaker discussed local gardens.")
    service.save_summary(event_id, talk["id"], "Asha discussed local gardens.")

    saved = service.get_event(event_id)
    assert next(item for item in saved["items"] if item["id"] == talk["id"])["summary"]
    assert any(summary["item_id"] == talk["id"] for summary in saved["summaries"])

    performance = next(item for item in saved["items"] if item["type"] == "performance")
    with pytest.raises(ValueError, match="Only speech or talk"):
        service.append_transcript(event_id, performance["id"], "A song was performed.")
    with pytest.raises(ValueError, match="Only speech or talk"):
        service.save_summary(event_id, performance["id"], "A short summary.")

    service.update_item(event_id, talk["id"], {"type": "performance"})
    changed = service.get_event(event_id)
    changed_talk = next(item for item in changed["items"] if item["id"] == talk["id"])
    assert changed_talk["summary"] == ""
    assert all(summary["item_id"] != talk["id"] for summary in changed["summaries"])

    announcement_event = service.add_item(
        event_id, {"title": "Safety announcement", "type": "announcement"}
    )
    announcement = next(
        item for item in announcement_event["items"] if item["type"] == "announcement"
    )
    service.append_transcript(event_id, announcement["id"], "Please keep the exits clear.")
    service.save_summary(event_id, announcement["id"], "Guests were reminded to keep exits clear.")
    assert next(
        item for item in service.get_event(event_id)["items"]
        if item["id"] == announcement["id"]
    )["summary"] == "Guests were reminded to keep exits clear."


def test_skip_current_advances_and_can_move_item_to_end(tmp_path: Path) -> None:
    service, event_id = make_service(tmp_path)
    initial = service.get_event(event_id)
    opening = initial["items"][0]

    skipped = service.skip_item(event_id, opening["id"], move_to_end=True)

    assert [item["title"] for item in skipped["items"]] == [
        "Guest address", "Music", "Opening"
    ]
    assert skipped["items"][-1]["status"] == "skipped"
    assert skipped["current_item_id"] == skipped["items"][0]["id"]
    assert skipped["items"][0]["status"] == "on_stage"


def test_restore_moves_a_late_item_to_requested_position(tmp_path: Path) -> None:
    service, event_id = make_service(tmp_path)
    initial = service.get_event(event_id)
    opening = initial["items"][0]
    service.skip_item(event_id, opening["id"])

    restored = service.restore_item(event_id, opening["id"], position=1)

    assert [item["title"] for item in restored["items"]] == [
        "Guest address", "Opening", "Music"
    ]
    assert restored["items"][1]["status"] == "pending"


def test_ended_event_can_be_edited_and_restarted(tmp_path: Path) -> None:
    service, event_id = make_service(tmp_path)
    ended = service.update_event(event_id, {"status": "ended"})
    first_item = ended["items"][0]

    updated = service.update_event(event_id, {"name": "Revised event"})
    updated = service.update_item(event_id, first_item["id"], {"title": "Revised opening"})
    updated = service.add_item(event_id, {"title": "Added after event"})
    updated = service.reorder(
        event_id, [item["id"] for item in reversed(updated["items"])]
    )
    updated = service.delete_item(event_id, first_item["id"])

    assert updated["status"] == "ended"
    assert updated["name"] == "Revised event"
    assert next(item for item in updated["items"] if item["id"] == first_item["id"])[
        "status"
    ] == "removed"
    with pytest.raises(ValueError, match="restarted through the start action"):
        service.update_event(event_id, {"status": "running"})
    restarted = service.start(event_id)
    first_eligible = next(
        item for item in updated["items"]
        if item["status"] not in {"absent", "removed", "skipped"}
    )
    assert restarted["status"] == "running"
    assert restarted["current_item_id"] == first_eligible["id"]


def test_restarting_ended_event_replays_schedule_and_preserves_exclusions(
    tmp_path: Path,
) -> None:
    service, event_id = make_service(tmp_path)
    initial = service.start(event_id)
    first, second, third = initial["items"]
    service.advance(event_id)
    service.skip_item(event_id, third["id"])
    service.update_event(event_id, {"status": "ended"})

    restarted = service.start(event_id)

    assert restarted["status"] == "running"
    assert restarted["current_item_id"] == first["id"]
    statuses = {item["id"]: item["status"] for item in restarted["items"]}
    assert statuses[first["id"]] == "on_stage"
    assert statuses[second["id"]] == "pending"
    assert statuses[third["id"]] == "skipped"


def test_session_event_lookup_prefers_the_active_event(tmp_path: Path) -> None:
    service, first_event_id = make_service(tmp_path)
    first_event = service.get_event(first_event_id)
    second_event = service.create_event(
        first_event["session_id"],
        name="Second event",
        schedule=[{"title": "Second event opening"}],
    )
    repository = MemoryRepository(service.database_path)
    repository.set_active_state(first_event["session_id"], event_id=first_event_id)

    selected = service.get_for_session(first_event["session_id"])

    assert second_event["id"] > first_event_id
    assert selected is not None
    assert selected["id"] == first_event_id


def test_deferred_start_waits_for_program_time_to_put_item_on_stage(tmp_path: Path) -> None:
    service, event_id = make_service(tmp_path)

    started = service.start(event_id, defer_first_item=True)

    assert started["status"] == "running"
    assert started["current_item_id"] is None
    assert all(item["status"] == "pending" for item in started["items"])

    first_program = service.advance(event_id, item_id=started["items"][0]["id"])
    assert first_program["current_item_id"] == started["items"][0]["id"]
    assert first_program["items"][0]["status"] == "on_stage"


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
