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
from google.genai import types

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
    return os.getenv("GEMINI_MODEL", "gemini-3.5-flash").strip() or "gemini-3.5-flash"


def fallback_model_name() -> str:
    """Return the optional fallback model selected in the environment."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    return os.getenv("GEMINI_FALLBACK_MODEL", "").strip()


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
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or "timeout" in name:
        return GeminiRequestError("Gemini request timed out after 60 seconds.")
    if status == 429:
        return GeminiRequestError("Rate limit reached, wait a moment and try again.")
    if status == 404:
        return GeminiRequestError(
            "Gemini model was not found. Check GEMINI_MODEL and GEMINI_FALLBACK_MODEL."
        )
    if status is not None and status >= 500:
        return GeminiRequestError("Gemini is temporarily unavailable. Please try again.")
    if (
        status == 0
        or any(token in name for token in ("connecterror", "networkerror", "connectionerror"))
        or any(token in message for token in ("network is unreachable", "name resolution", "connection refused"))
    ):
        return GeminiRequestError("No internet connection or Gemini service is unreachable.")
    return GeminiRequestError(f"Gemini request failed ({type(exc).__name__}).")


def _retryable(exc: Exception) -> bool:
    status = _status_code(exc)
    return status == 429 or (status is not None and 500 <= status <= 599)


def _should_use_fallback(exc: Exception) -> bool:
    return _status_code(exc) in {404, 429} or _retryable(exc) or isinstance(
        exc, (TimeoutError, asyncio.TimeoutError)
    ) or "timeout" in type(exc).__name__.lower()


async def _backoff(attempt: int) -> None:
    await asyncio.sleep(min(0.5 * (2**attempt) + random.random() * 0.1, 4))


async def _stream_model(
    selected_model: str,
    contents: list[dict[str, Any]],
    config: types.GenerateContentConfig,
) -> AsyncIterator[str]:
    """Stream one model with a total timeout and bounded transient retries."""
    emitted = False
    for attempt in range(MAX_RETRIES + 1):
        try:
            client = _client()
            stream = await client.aio.models.generate_content_stream(
                model=selected_model,
                contents=contents,
                config=config,
            )
            iterator = stream.__aiter__()
            deadline = asyncio.get_running_loop().time() + REQUEST_TIMEOUT_SECONDS
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError
                try:
                    response = await asyncio.wait_for(
                        iterator.__anext__(), timeout=remaining
                    )
                except StopAsyncIteration:
                    return
                text = response.text
                if text:
                    emitted = True
                    yield text
        except GeminiRequestError:
            raise
        except Exception as exc:
            if _retryable(exc) and attempt < MAX_RETRIES and not emitted:
                logger.warning(
                    "Retrying Gemini stream after transient response",
                    extra={"attempt": attempt + 1, "status": _status_code(exc)},
                )
                await _backoff(attempt)
                continue
            raise


async def _full_model(
    selected_model: str,
    contents: list[dict[str, Any]],
    config: types.GenerateContentConfig,
) -> str:
    """Generate a complete response with bounded transient retries."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            client = _client()
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=selected_model,
                    contents=contents,
                    config=config,
                ),
                timeout=REQUEST_TIMEOUT_SECONDS,
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
            raise


async def llm_stream(
    messages: Sequence[dict[str, str]],
    *,
    model: str | None = None,
    response_schema: dict[str, Any] | None = None,
) -> AsyncIterator[str]:
    """Yield Gemini chunks and use the configured fallback after selected failures."""
    selected_model = model or model_name()
    fallback = fallback_model_name()
    contents, config = convert_messages(messages, response_schema)
    yielded = False
    try:
        async for text in _stream_model(selected_model, contents, config):
            yielded = True
            yield text
    except Exception as exc:
        if not yielded and fallback and fallback != selected_model and _should_use_fallback(exc):
            logger.warning(
                "Gemini primary model failed; trying configured fallback",
                extra={"primary_model": selected_model, "fallback_model": fallback},
            )
            try:
                async for text in _stream_model(fallback, contents, config):
                    yield text
                return
            except Exception as fallback_exc:
                raise _friendly_error(fallback_exc) from fallback_exc
        raise _friendly_error(exc) from exc


async def llm_full(
    messages: Sequence[dict[str, str]],
    *,
    model: str | None = None,
    response_schema: dict[str, Any] | None = None,
) -> str:
    """Generate a complete response, using the configured fallback if needed."""
    selected_model = model or model_name()
    fallback = fallback_model_name()
    contents, config = convert_messages(messages, response_schema)
    try:
        return await _full_model(selected_model, contents, config)
    except Exception as exc:
        if fallback and fallback != selected_model and _should_use_fallback(exc):
            logger.warning(
                "Gemini primary model failed; trying configured fallback",
                extra={"primary_model": selected_model, "fallback_model": fallback},
            )
            try:
                return await _full_model(fallback, contents, config)
            except Exception as fallback_exc:
                raise _friendly_error(fallback_exc) from fallback_exc
        raise _friendly_error(exc) from exc


async def health_check() -> dict[str, Any]:
    """Verify key configuration and perform a tiny Gemini generation."""
    fallback = fallback_model_name() or None
    try:
        api_key()
    except GeminiRequestError as exc:
        return {
            "available": False,
            "key_set": False,
            "model": model_name(),
            "fallback_model": fallback,
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
            "fallback_model": fallback,
            "message": "Gemini test generation succeeded." if text.strip() else "Gemini returned an empty response.",
        }
    except GeminiRequestError as exc:
        return {
            "available": False,
            "key_set": True,
            "model": model_name(),
            "fallback_model": fallback,
            "message": str(exc),
        }
