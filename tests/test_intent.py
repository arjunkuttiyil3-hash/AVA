"""Tests for deterministic intent classification."""

from typing import Iterator

from config.settings import AppSettings
from nlu.intent_classifier import IntentClassifier, detect_language
from orchestrator.router import ModelRouter


class JsonClassifierBackend:
    """Structured-output test backend that records the requested schema."""

    def __init__(self) -> None:
        self.schema = None
        self.model = ""
        self.prompt = ""

    def stream(
        self, model: str, messages: list[dict[str, str]], *, format_schema=None
    ) -> Iterator[str]:
        self.schema = format_schema
        self.model = model
        self.prompt = messages[-1]["content"] if messages else ""
        return iter(['{"intent":"smalltalk","confidence":0.84}'])


def test_rules_classify_story_and_schedule() -> None:
    classifier = IntentClassifier()
    assert classifier.classify("Create a story about a robot in Kerala").intent == "story_request"
    assert classifier.classify("Now make it meet a school student", active_story=True).intent == "story_followup"
    assert classifier.classify("What is the next item?").intent == "schedule_next"


def test_language_and_robot_command_detection() -> None:
    classifier = IntentClassifier()
    assert detect_language("നമസ്കാരം") == "ml"
    assert detect_language("Hello നമസ്കാരം") == "mixed"
    assert classifier.classify("Please wave").intent == "robot_command"
    assert classifier.classify("Dr. Nair has not arrived, remove him").intent == "event_command"


def test_ambiguous_intent_uses_json_schema_model_fallback(tmp_path) -> None:
    backend = JsonClassifierBackend()
    router = ModelRouter(
        AppSettings(
            config_path=tmp_path / "ava.yaml",
            llm={
                "routes": {
                    "intent_classifier": {
                        "backend": "gemini",
                        "model": "gemini-3.6-flash",
                    }
                }
            },
        ),
        backends={"gemini": backend},
    )

    result = IntentClassifier(router).classify("Perhaps something different")

    assert result.intent == "smalltalk"
    assert result.source == "llm"
    assert backend.schema["type"] == "object"
    assert backend.model == "gemini-3.6-flash"
    assert "Perhaps something different" in backend.prompt
    assert "story_followup" in backend.schema["properties"]["intent"]["enum"]
