"""End-to-end regression for continuity across a two-turn story request."""

import asyncio
from pathlib import Path
from typing import Any, Iterator

from config.settings import AppSettings
from context.memory_manager import MemoryManager
from memory.repository import MemoryRepository
from modules.robot import SimulatedRobot
from nlu.intent_classifier import IntentClassifier
from orchestrator.pipeline import ConversationPipeline
from orchestrator.router import ModelRouter
from services.event_service import EventService


class StoryBackend:
    """Deterministic local model substitute that records story prompts."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.requests: list[list[dict[str, str]]] = []

    def stream(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        format_schema: dict[str, Any] | None = None,
    ) -> Iterator[str]:
        if model != "gemini-3.5-flash" or format_schema is not None:
            return iter(())
        self.requests.append([dict(message) for message in messages])
        prompt = "\n".join(message["content"] for message in messages)
        self.prompts.append(prompt)
        if "Continue the same story" in prompt:
            return iter(
                [
                    "Kuttan met Anu, a school student, beside the paddy fields. "
                    "Together, they planned a small garden."
                ]
            )
        return iter(
            [
                "Kuttan, a curious robot, explored Kerala's green paddy fields. "
                "He hoped to meet new friends."
            ]
        )


class SilentSynthesizer:
    """Avoid external TTS services while exercising sentence output events."""

    async def synthesize(self, text: str, language: str, *, voice=None) -> Path:
        _ = (text, language, voice)
        return Path("cached.mp3")

    @staticmethod
    def get_status(audio_path: Path) -> None:
        _ = audio_path
        return None


class BrokenSynthesizer(SilentSynthesizer):
    """Model a failure of both Edge TTS and the configured offline synthesizer."""

    async def synthesize(self, text: str, language: str, *, voice=None) -> Path:
        _ = (text, language, voice)
        raise RuntimeError("speech synthesis unavailable")


class MalayalamBackend:
    """Return Malayalam text and retain the prompt for language assertions."""

    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []

    def stream(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        format_schema: dict[str, Any] | None = None,
    ) -> Iterator[str]:
        _ = (model, format_schema)
        self.messages = [dict(message) for message in messages]
        return iter(["നമസ്കാരം."])


class RecordingSynthesizer(SilentSynthesizer):
    """Capture the selected voice and provide a small valid audio file."""

    def __init__(self, output_path: Path) -> None:
        self.output_path = output_path
        self.voices = {
            "en": "en-IN-NeerjaNeural",
            "ml": "ml-IN-SobhanaNeural",
        }
        self.selected_voice: str | None = None

    async def synthesize(self, text: str, language: str, *, voice=None) -> Path:
        _ = (text, language)
        self.selected_voice = voice
        self.output_path.write_bytes(b"audio")
        return self.output_path


class ScheduleSafetyBackend:
    """Record the schedule prompt and return a deliberately unsafe sentence."""

    def __init__(self) -> None:
        self.prompt = ""
        self.prompts: list[str] = []

    def stream(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        format_schema: dict[str, Any] | None = None,
    ) -> Iterator[str]:
        _ = (model, format_schema)
        self.prompt = "\n".join(message["content"] for message in messages)
        self.prompts.append(self.prompt)
        return iter(
            [
                "Welcome, everyone. Please welcome Dr. Nair. "
                "We will now enjoy the music performance."
            ]
        )


def test_robot_kerala_story_continues_when_user_adds_student(tmp_path: Path) -> None:
    async def exercise() -> None:
        repository = MemoryRepository(tmp_path / "pipeline.sqlite3")
        memory = MemoryManager(repository)
        backend = StoryBackend()
        settings = AppSettings(
            config_path=tmp_path / "ava.yaml",
            llm={
                "default_model": "gemini-3.5-flash",
                "routes": {
                    "story_request": {"backend": "gemini", "model": "gemini-3.5-flash"},
                    "story_followup": {"backend": "gemini", "model": "gemini-3.5-flash"},
                    "default": {"backend": "template"},
                },
            },
        )
        router = ModelRouter(settings, backends={"gemini": backend})
        pipeline = ConversationPipeline(
            settings,
            repository,
            memory,
            router,
            IntentClassifier(router),
            SilentSynthesizer(),
            robot=SimulatedRobot(),
        )
        session_id = repository.create_session("story-pipeline")

        first_turn = [
            event
            async for event in pipeline.process(
                session_id,
                "Create a story about a robot in Kerala",
                language="en",
            )
        ]
        second_turn = [
            event
            async for event in pipeline.process(
                session_id,
                "Now make it meet a school student",
                language="en",
            )
        ]
        state = repository.get_active_state(session_id)["story"]

        assert any(event.get("intent") == "story_request" for event in first_turn)
        assert any(event.get("intent") == "story_followup" for event in second_turn)
        assert state is not None
        assert state["setting"] == "Kerala"
        assert "Anu" in state["last_paragraph"]
        assert "Kuttan, a curious robot, explored Kerala" in backend.prompts[-1]
        assert state["id"]
        conversation = [
            item for item in backend.requests[-1] if item["role"] != "system"
        ]
        assert len(conversation) == 3
        assert conversation[0]["content"] == "Create a story about a robot in Kerala"
        assert "Kuttan, a curious robot, explored Kerala" in conversation[1]["content"]
        assert "Now make it meet a school student" in conversation[2]["content"]
        stored_messages = repository.get_recent_messages(session_id, limit=12)
        assert [item["role"] for item in stored_messages] == [
            "user", "assistant", "user", "assistant"
        ]
        assert stored_messages[0]["content"] == "Create a story about a robot in Kerala"
        assert stored_messages[2]["content"] == "Now make it meet a school student"
        assert "Kuttan" in stored_messages[1]["content"]
        assert "Anu" in stored_messages[3]["content"]

    asyncio.run(exercise())


def test_anchoring_prompt_uses_live_schedule_and_drops_absent_person_sentence(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        database_path = tmp_path / "schedule-safety.sqlite3"
        repository = MemoryRepository(database_path)
        memory = MemoryManager(repository)
        session_id = repository.create_session("schedule-safety")
        event_service = EventService(database_path)
        event = event_service.create_event(
            session_id,
            name="Arts day",
            schedule=[
                {"title": "Opening", "type": "welcome"},
                {
                    "title": "Guest address",
                    "type": "speech",
                    "person_name": "Dr. Nair",
                },
                {"title": "Music performance", "type": "performance"},
            ],
        )
        absent_item = next(
            item for item in event["items"] if item["person_name"] == "Dr. Nair"
        )
        event_service.update_item(event["id"], absent_item["id"], {"status": "absent"})
        backend = ScheduleSafetyBackend()
        settings = AppSettings(
            config_path=tmp_path / "ava.yaml",
            memory={"database_path": str(database_path)},
            llm={
                "routes": {
                    "anchoring_script": {
                        "backend": "gemini",
                        "model": "gemini-3.5-flash",
                    },
                    "schedule_next": {
                        "backend": "gemini",
                        "model": "gemini-3.5-flash",
                    },
                    "default": {"backend": "template"},
                }
            },
        )
        router = ModelRouter(settings, backends={"gemini": backend})
        pipeline = ConversationPipeline(
            settings,
            repository,
            memory,
            router,
            IntentClassifier(router),
            SilentSynthesizer(),
            robot=SimulatedRobot(),
        )

        events = [
            item
            async for item in pipeline.process(
                session_id,
                "Prepare an announcement for the next item",
                mode="anchoring",
                language="en",
            )
        ]
        response_text = "".join(
            item["text"] for item in events if item["type"] == "token"
        )

        assert "Music performance" in backend.prompt
        assert "Dr. Nair" not in backend.prompts[0]
        assert "Dr. Nair" not in response_text
        assert "Welcome, everyone." in response_text
        assert "We will now enjoy the music performance." in response_text

    asyncio.run(exercise())


def test_malayalam_mode_uses_script_prompt_and_matching_voice(tmp_path: Path) -> None:
    async def exercise() -> None:
        repository = MemoryRepository(tmp_path / "malayalam-mode.sqlite3")
        memory = MemoryManager(repository)
        backend = MalayalamBackend()
        settings = AppSettings(
            config_path=tmp_path / "ava.yaml",
            llm={
                "default_model": "gemini-3.5-flash",
                "routes": {
                    "default": {"backend": "gemini", "model": "gemini-3.5-flash"}
                },
            },
        )
        router = ModelRouter(settings, backends={"gemini": backend})
        synthesizer = RecordingSynthesizer(tmp_path / "reply.mp3")
        pipeline = ConversationPipeline(
            settings,
            repository,
            memory,
            router,
            IntentClassifier(router),
            synthesizer,
            robot=SimulatedRobot(),
        )
        session_id = repository.create_session("malayalam-mode")
        repository.set_preferences(
            session_id,
            language="ml",
            style="Professional",
            voice="en-IN-NeerjaNeural",
        )

        events = [
            event
            async for event in pipeline.process(
                session_id, "നമസ്കാരം", language="ml"
            )
        ]

        system_prompt = backend.messages[0]["content"]
        assert "natural spoken Malayalam" in system_prompt
        assert "written in Malayalam script" in system_prompt
        assert "short, clear sentences" in system_prompt
        assert synthesizer.selected_voice == "ml-IN-SobhanaNeural"
        assert any(event.get("language") == "ml" for event in events)

    asyncio.run(exercise())


def test_synthesis_failure_emits_browser_speech_fallback_text(tmp_path: Path) -> None:
    async def exercise() -> None:
        repository = MemoryRepository(tmp_path / "speech-fallback.sqlite3")
        memory = MemoryManager(repository)
        settings = AppSettings(
            config_path=tmp_path / "ava.yaml",
            llm={"routes": {"default": {"backend": "template", "model": "template"}}},
        )
        router = ModelRouter(settings)
        pipeline = ConversationPipeline(
            settings,
            repository,
            memory,
            router,
            IntentClassifier(router),
            BrokenSynthesizer(),
            robot=SimulatedRobot(),
        )
        session_id = repository.create_session("speech-fallback")
        events = [
            event
            async for event in pipeline.process(
                session_id, "Hello AVA", language="en"
            )
        ]

        unavailable = [
            event for event in events if event["type"] == "audio_unavailable"
        ]
        assert unavailable
        assert unavailable[0]["text"]
        assert unavailable[0]["language"] == "en"

    asyncio.run(exercise())


def test_prompt_includes_at_most_twelve_conversation_messages(tmp_path: Path) -> None:
    repository = MemoryRepository(tmp_path / "prompt-window.sqlite3")
    memory = MemoryManager(repository, short_term_turns=12)
    session_id = repository.create_session("prompt-window")
    for index in range(15):
        role = "user" if index % 2 == 0 else "assistant"
        repository.add_message(session_id, role, f"stored message {index}")

    from context.memory_manager import ConversationContext

    context = ConversationContext(
        session_id=session_id,
        preferences={},
        summary="",
        retrieved=[],
        recent=repository.get_recent_messages(session_id, limit=15),
        active_event=None,
        active_story=None,
        continues_story=False,
        continues_event=False,
    )
    prompt = memory.build_prompt(context, "System instructions.", "Latest request.")
    conversation = [item for item in prompt if item["role"] != "system"]

    assert len(conversation) == 12
    assert conversation[0]["content"] == "stored message 4"
    assert conversation[-2]["content"] == "stored message 14"
    assert conversation[-1]["content"] == "Latest request."
