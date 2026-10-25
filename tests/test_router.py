"""Tests for model routing and fallback behavior."""

from pathlib import Path
from typing import Iterator

from config.settings import AppSettings
from orchestrator.router import ModelRouter


class BrokenBackend:
    """Backend that models a local service outage."""

    def stream(self, model: str, messages: list[dict[str, str]]) -> Iterator[str]:
        if messages and model:
            raise TimeoutError("offline")
        return iter(())


class InterruptedBackend:
    """Backend that fails after emitting one partial response chunk."""

    def stream(self, model: str, messages: list[dict[str, str]]) -> Iterator[str]:
        if not messages or not model:
            return iter(())
        yield "Partial response."
        raise TimeoutError("connection interrupted")


def test_router_falls_back_to_template(tmp_path: Path) -> None:
    settings = AppSettings(
        config_path=tmp_path / "ava.yaml",
        llm={
            "default_model": "gemma3:4b",
            "retries": 1,
            "timeout_seconds": 0.1,
            "routes": {
                "chat_question": {"backend": "ollama", "model": "gemma3:4b"}
            },
        },
    )
    router = ModelRouter(
        settings, backends={"ollama": BrokenBackend()}, sleeper=lambda _: None
    )

    chunks, decision = router.stream(
        "chat_question",
        "en",
        [{"role": "user", "content": "How are you?"}],
    )

    response = "".join(chunks)
    assert "local language model is currently unavailable" in response
    assert decision.backend == "template"
    assert decision.reason.startswith("Template fallback")


def test_router_marks_reset_after_midstream_failure(tmp_path: Path) -> None:
    settings = AppSettings(
        config_path=tmp_path / "ava.yaml",
        llm={
            "retries": 0,
            "routes": {
                "chat_question": {"backend": "ollama", "model": "gemma3:4b"}
            },
        },
    )
    router = ModelRouter(
        settings, backends={"ollama": InterruptedBackend()}, sleeper=lambda _: None
    )
    chunks, decision = router.stream(
        "chat_question", "en", [{"role": "user", "content": "Question"}]
    )

    output = list(chunks)

    assert output[0] == "Partial response."
    assert output[1] == "\x00AVA_STREAM_RESET\x00"
    assert decision.backend == "template"


def test_router_selects_candidate_within_latency_and_memory_limits(
    tmp_path: Path,
) -> None:
    settings = AppSettings(
        config_path=tmp_path / "ava.yaml",
        llm={
            "latency_budget_ms": 9000,
            "routes": {
                "chat_question": {
                    "candidates": [
                        {
                            "backend": "ollama",
                            "model": "gemma3:4b",
                            "estimated_latency_ms": 10000,
                            "min_available_memory_mb": 2048,
                        },
                        {
                            "backend": "ollama",
                            "model": "qwen2.5:1.5b",
                            "estimated_latency_ms": 5000,
                            "min_available_memory_mb": 512,
                        },
                        {
                            "backend": "template",
                            "model": "template",
                            "estimated_latency_ms": 20,
                            "min_available_memory_mb": 0,
                        },
                    ]
                }
            },
        },
    )
    router = ModelRouter(settings)

    selected = router._configured_route(
        "chat_question", "en", latency_budget_ms=6000, available_memory_mb=1024
    )

    assert selected["model"] == "qwen2.5:1.5b"
    assert "6000 ms" in selected["_selection_reason"]
