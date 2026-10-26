"""Tests for script generation gap analysis and clarification prompt construction."""

from modules.script_gen import (
    analyze_event_gaps,
    build_clarification_prompt,
    build_structured_script_prompt,
    validate_script_segments,
)
import pytest


def test_analyze_event_gaps_identifies_missing_chief_guest_title():
    event = {
        "name": "TechFest 2026",
        "chief_guest": "Dr. Ramesh",
        "chief_guest_title": "",
    }
    brief = {"audience": "Students"}
    schedule = [{"title": "Welcome Speech", "participant": "Dean"}]

    result = analyze_event_gaps(event, brief, schedule, language="en")
    assert result["has_questions"] is True
    assert any("Chief guest designation" in aspect for aspect in result["missing_aspects"])
    q_fields = [q["field"] for q in result["questions"]]
    assert "chief_guest_title" in q_fields


def test_analyze_event_gaps_identifies_missing_performers():
    event = {"name": "Arts Night"}
    brief = {"audience": "Students"}
    schedule = [
        {"title": "Classical Dance", "participant": ""},
        {"title": "Keynote Address", "participant": ""},
    ]

    result = analyze_event_gaps(event, brief, schedule, language="en")
    assert result["has_questions"] is True
    assert len(result["questions"]) >= 2


def test_analyze_event_gaps_malayalam_support():
    event = {
        "name": "കലോത്സവം 2026",
        "chief_guest": "ശ്രീ മോഹൻലാൽ",
        "chief_guest_title": "",
    }
    result = analyze_event_gaps(event, {}, [], language="ml")
    assert result["has_questions"] is True
    assert "മോഹൻലാൽ" in result["questions"][0]["question"]


def test_analyze_event_gaps_complete_event_has_no_questions():
    event = {
        "name": "Annual Day",
        "venue": "Main Hall",
        "event_start_time": "10:00",
        "chief_guest": "Dr. Sharma",
        "chief_guest_title": "Director",
    }
    brief = {
        "audience": "Parents and Students",
        "topic": "Excellence in Education",
        "key_points": "Awards and cultural programs",
    }
    schedule = [
        {"title": "Welcome Speech", "participant": "Principal"},
        {"title": "Vote of Thanks", "participant": "Vice Principal"},
    ]

    result = analyze_event_gaps(event, brief, schedule, language="en")
    assert result["has_questions"] is False
    assert len(result["questions"]) == 0


def test_build_clarification_prompt_structure():
    messages, schema = build_clarification_prompt(
        {"name": "Seminar"}, {"audience": "Engineers"}, [], language="en"
    )
    assert len(messages) == 2
    assert "system" in messages[0]["role"]
    assert "schema" != ""
    assert "properties" in schema
    assert "questions" in schema["properties"]


def test_structured_prompt_excludes_absentees_and_uses_requested_targets():
    prompt = build_structured_script_prompt(
        {"name": "Awards night"},
        [
            {"id": 1, "type": "speech", "person_name": "Anu", "status": "pending"},
            {"id": 2, "type": "performance", "person_name": "Binu", "status": "absent"},
        ],
        targets=[{"id": "intro-1", "type": "intro", "itemId": 1}],
    )
    assert "intro-1" in prompt
    assert "Binu" in prompt
    assert "Eligible running order:" in prompt
    assert "recovery-" in prompt


def test_validate_script_segments_rejects_missing_or_mislinked_segments():
    expected = [{"id": "intro-7", "type": "intro", "itemId": 7}]
    valid = [{
        "id": "intro-7",
        "type": "intro",
        "itemId": 7,
        "text": "Please welcome our speaker.",
        "estimatedSeconds": 8,
    }]
    assert validate_script_segments(valid, expected)[0]["edited"] is False
    with pytest.raises(ValueError, match="wrong event item"):
        validate_script_segments([{**valid[0], "itemId": 8}], expected)
    with pytest.raises(ValueError, match="non-negative integer"):
        validate_script_segments([{**valid[0], "estimatedSeconds": -1}], expected)
