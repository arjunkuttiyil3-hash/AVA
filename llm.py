"""Central asynchronous Gemini client for every AVA language-model request."""

from __future__ import annotations

import asyncio
import logging
import os
import random
from collections.abc import AsyncIterator, Sequence
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

from config.settings import PROJECT_ROOT

load_dotenv(PROJECT_ROOT / ".env")
logger = logging.getLogger("ava.llm")

REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 2
_clients: dict[str, genai.Client] = {}


class GeminiRequestError(RuntimeError):
    """Friendly failure safe to show in the AVA interface."""


def api_key() -> str:
    """Return the configured API key or raise an actionable setup error."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise GeminiRequestError(
            "Gemini API key missing. Add GEMINI_API_KEY to your .env file."
        )
    return key


def model_name() -> str:
    """Return the Gemini model selected in the environment."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    return os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"


def _client() -> genai.Client:
    key = api_key()
    if key not in _clients:
        _clients[key] = genai.Client(
            api_key=key,
            http_options=types.HttpOptions(
                timeout=REQUEST_TIMEOUT_SECONDS * 1000,
            ),
        )
    return _clients[key]


def convert_messages(
    messages: Sequence[dict[str, str]],
    response_schema: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], types.GenerateContentConfig]:
    """Separate system instructions and map chat roles to Gemini content roles."""
    instructions: list[str] = []
    contents: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role", "user"))
        text = str(message.get("content", ""))
        if role == "system":
            instructions.append(text)
            continue
        gemini_role = "model" if role in {"assistant", "model"} else "user"
        contents.append({"role": gemini_role, "parts": [{"text": text}]})
    if not contents:
        contents.append({"role": "user", "parts": [{"text": "Continue."}]})
    config_values: dict[str, Any] = {
        "system_instruction": "\n\n".join(instructions) or None,
    }
    if response_schema is not None:
        config_values["response_mime_type"] = "application/json"
        config_values["response_schema"] = response_schema
    return contents, types.GenerateContentConfig(**config_values)


def _status_code(exc: Exception) -> int | None:
    for value in (
        getattr(exc, "code", None),
        getattr(exc, "status_code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ):
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _friendly_error(exc: Exception) -> GeminiRequestError:
    status = _status_code(exc)
    if status == 429:
        return GeminiRequestError("Rate limit reached, wait a moment and try again.")
    if status is not None and status >= 500:
        return GeminiRequestError("Gemini is temporarily unavailable. Please try again.")
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return GeminiRequestError("Gemini request timed out after 60 seconds.")
    return GeminiRequestError(f"Gemini request failed ({type(exc).__name__}).")


def _retryable(exc: Exception) -> bool:
    status = _status_code(exc)
    return status == 429 or (status is not None and 500 <= status <= 599)


async def _backoff(attempt: int) -> None:
    await asyncio.sleep(min(0.5 * (2**attempt) + random.random() * 0.1, 4))


async def llm_stream(
    messages: Sequence[dict[str, str]],
    *,
    model: str | None = None,
    response_schema: dict[str, Any] | None = None,
) -> AsyncIterator[str]:
    """Yield Gemini text chunks, retrying transient rate-limit/server failures."""
    selected_model = model or model_name()
    contents, config = convert_messages(messages, response_schema)
    yielded = False
    for attempt in range(MAX_RETRIES + 1):
        try:
            client = _client()
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                async for response in client.aio.models.generate_content_stream(
                    model=selected_model,
                    contents=contents,
                    config=config,
                ):
                    text = response.text
                    if text:
                        yielded = True
                        yield text
            return
        except GeminiRequestError:
            raise
        except Exception as exc:
            if _retryable(exc) and attempt < MAX_RETRIES and not yielded:
                logger.warning(
                    "Retrying Gemini stream after transient response",
                    extra={"attempt": attempt + 1, "status": _status_code(exc)},
                )
                await _backoff(attempt)
                continue
            raise _friendly_error(exc) from exc


async def llm_full(
    messages: Sequence[dict[str, str]],
    *,
    model: str | None = None,
    response_schema: dict[str, Any] | None = None,
) -> str:
    """Generate one complete Gemini response with bounded retry and timeout."""
    selected_model = model or model_name()
    contents, config = convert_messages(messages, response_schema)
    for attempt in range(MAX_RETRIES + 1):
        try:
            client = _client()
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                response = await client.aio.models.generate_content(
                    model=selected_model,
                    contents=contents,
                    config=config,
                )
            return response.text or ""
        except GeminiRequestError:
            raise
        except Exception as exc:
            if _retryable(exc) and attempt < MAX_RETRIES:
                logger.warning(
                    "Retrying Gemini request after transient response",
                    extra={"attempt": attempt + 1, "status": _status_code(exc)},
                )
                await _backoff(attempt)
                continue
            raise _friendly_error(exc) from exc
    raise GeminiRequestError("Gemini request failed after retries.")


async def health_check() -> dict[str, Any]:
    """Verify key configuration and perform a tiny Gemini generation."""
    try:
        api_key()
    except GeminiRequestError as exc:
        return {
            "available": False,
            "key_set": False,
            "model": model_name(),
            "message": str(exc),
        }
    try:
        text = await llm_full(
            [{"role": "user", "content": "Reply with the single word OK."}],
            model=model_name(),
        )
        return {
            "available": bool(text.strip()),
            "key_set": True,
            "model": model_name(),
            "message": "Gemini test generation succeeded." if text.strip() else "Gemini returned an empty response.",
        }
    except GeminiRequestError as exc:
        return {
            "available": False,
            "key_set": True,
            "model": model_name(),
            "message": str(exc),
        }
