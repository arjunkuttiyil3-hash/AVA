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


class StoryBackend:
    """Deterministic local model substitute that records story prompts."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def stream(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        format_schema: dict[str, Any] | None = None,
    ) -> Iterator[str]:
        if model != "gemma3:4b" or format_schema is not None:
            return iter(())
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


def test_robot_kerala_story_continues_when_user_adds_student(tmp_path: Path) -> None:
    async def exercise() -> None:
        repository = MemoryRepository(tmp_path / "pipeline.sqlite3")
        memory = MemoryManager(repository)
        backend = StoryBackend()
        settings = AppSettings(
            config_path=tmp_path / "ava.yaml",
            llm={
                "default_model": "gemma3:4b",
                "routes": {
                    "story_request": {"backend": "ollama", "model": "gemma3:4b"},
                    "story_followup": {"backend": "ollama", "model": "gemma3:4b"},
                    "default": {"backend": "template"},
                },
            },
        )
        router = ModelRouter(settings, backends={"ollama": backend})
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

    asyncio.run(exercise())
