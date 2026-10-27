"""Tests for Gemini message conversion, streaming, and model fallback."""

import asyncio
from types import SimpleNamespace

import pytest

import llm


class StatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class FakeModels:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.models: list[str] = []

    async def generate_content(self, *, model, contents, config):
        _ = (contents, config)
        self.models.append(model)
        if model == "primary" and self.failure is not None:
            raise self.failure
        return SimpleNamespace(text="fallback response")

    async def generate_content_stream(self, *, model, contents, config):
        _ = (contents, config)
        self.models.append(model)
        if model == "primary" and self.failure is not None:
            raise self.failure

        async def chunks():
            yield SimpleNamespace(text="fallback ")
            yield SimpleNamespace(text="response")

        return chunks()


@pytest.mark.parametrize(
    "failure",
    [
        StatusError(404),
        StatusError(429),
        StatusError(503),
        TimeoutError("request timed out"),
    ],
)
def test_full_generation_uses_configured_fallback(
    monkeypatch, failure: Exception
) -> None:
    models = FakeModels(failure)

    async def no_backoff(attempt: int) -> None:
        _ = attempt

    monkeypatch.setenv("GEMINI_FALLBACK_MODEL", "fallback")
    monkeypatch.setattr(llm, "_client", lambda: SimpleNamespace(aio=SimpleNamespace(models=models)))
    monkeypatch.setattr(llm, "_backoff", no_backoff)

    result = asyncio.run(
        llm.llm_full([{"role": "user", "content": "Hello"}], model="primary")
    )

    assert result == "fallback response"
    assert models.models[-1] == "fallback"
    if isinstance(failure, StatusError) and failure.status_code in {429, 503}:
        assert models.models == ["primary", "primary", "primary", "fallback"]


def test_stream_generation_awaits_sdk_stream_and_uses_fallback(monkeypatch) -> None:
    models = FakeModels(StatusError(404))
    monkeypatch.setenv("GEMINI_FALLBACK_MODEL", "fallback")
    monkeypatch.setattr(llm, "_client", lambda: SimpleNamespace(aio=SimpleNamespace(models=models)))

    chunks = asyncio.run(
        collect_stream(llm.llm_stream(
            [{"role": "user", "content": "Hello"}], model="primary"
        ))
    )

    assert chunks == ["fallback ", "response"]
    assert models.models == ["primary", "fallback"]


def test_stream_does_not_switch_models_after_emitting_text(monkeypatch) -> None:
    class PartialModels(FakeModels):
        async def generate_content_stream(self, *, model, contents, config):
            _ = (contents, config)
            self.models.append(model)

            async def chunks():
                yield SimpleNamespace(text="partial")
                raise StatusError(429)

            return chunks()

    models = PartialModels()
    monkeypatch.setenv("GEMINI_FALLBACK_MODEL", "fallback")
    monkeypatch.setattr(llm, "MAX_RETRIES", 0)
    monkeypatch.setattr(llm, "_client", lambda: SimpleNamespace(aio=SimpleNamespace(models=models)))

    with pytest.raises(
        llm.GeminiRequestError, match="rate limit or quota exceeded \\(HTTP 429\\)"
    ) as error:
        asyncio.run(collect_stream(llm.llm_stream(
            [{"role": "user", "content": "Hello"}], model="primary"
        )))

    assert error.value.status_code == 429
    assert "Provider detail: HTTP 429" in str(error.value)
    assert models.models == ["primary"]


async def collect_stream(stream) -> list[str]:
    return [chunk async for chunk in stream]


def test_message_conversion_uses_system_instruction_and_model_role() -> None:
    contents, config = llm.convert_messages(
        [
            {"role": "system", "content": "Be concise."},
            {"role": "assistant", "content": "Prior answer."},
            {"role": "user", "content": "Continue."},
        ]
    )

    assert contents[0]["role"] == "model"
    assert contents[1]["role"] == "user"
    assert config.system_instruction == "Be concise."


def test_health_check_reports_missing_key_without_calling_sdk(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "")
    result = asyncio.run(llm.health_check())

    assert result["available"] is False
    assert result["key_set"] is False
    assert "API key missing" in result["message"]