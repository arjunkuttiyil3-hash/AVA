"""Config-driven Gemini model selection and deterministic template fallback."""

from __future__ import annotations

import logging
import asyncio
import queue
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterator, Protocol

from config.settings import AppSettings, load_settings
from llm import llm_stream, model_name

logger = logging.getLogger(__name__)
STREAM_RESET = "\x00AVA_STREAM_RESET\x00"


@dataclass
class RouteDecision:
    """Observable route decision returned alongside each generated response."""

    task: str
    backend: str
    model: str
    reason: str
    latency_ms: float
    failure_detail: str | None = None
    failure_status: int | None = None


class ChatBackend(Protocol):
    """Minimal interface required for a streaming text backend."""

    def stream(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        format_schema: dict[str, Any] | None = None,
    ) -> Iterator[str]: ...


class GeminiBackend:
    """Synchronous iterator adapter over the centralized async Gemini client."""

    def stream(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        format_schema: dict[str, Any] | None = None,
    ) -> Iterator[str]:
        events: queue.Queue[tuple[str, Any]] = queue.Queue()

        def worker() -> None:
            async def consume() -> None:
                try:
                    async for token in llm_stream(
                        messages,
                        model=model,
                        response_schema=format_schema,
                    ):
                        events.put(("token", token))
                    events.put(("done", None))
                except Exception as exc:
                    events.put(("error", exc))

            asyncio.run(consume())

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        while True:
            kind, value = events.get()
            if kind == "done":
                break
            if kind == "error":
                raise value
            yield str(value)
        thread.join()


class ModelRouter:
    """Select and execute configured model routes, with bounded backoff retries."""

    def __init__(
        self,
        settings: AppSettings | None = None,
        *,
        backends: dict[str, ChatBackend] | None = None,
        sleeper: Any = time.sleep,
    ) -> None:
        self.settings = settings or load_settings()
        self.backends = backends or {}
        self.sleeper = sleeper

    def _configured_route(
        self,
        task: str,
        language: str,
        *,
        latency_budget_ms: int | None = None,
        available_memory_mb: int | None = None,
    ) -> dict[str, Any]:
        routes = self.settings.llm.get("routes", {})
        route = routes.get(task, routes.get("default", {}))
        language_route = route.get("languages", {}).get(language, {})
        route = {**route, **language_route}
        candidates = route.get("candidates")
        if not candidates:
            return route

        budget = latency_budget_ms or int(
            self.settings.llm.get("latency_budget_ms", 30000)
        )
        memory = available_memory_mb
        if memory is None:
            try:
                import psutil

                memory = int(psutil.virtual_memory().available // (1024 * 1024))
            except (ImportError, OSError):
                logger.warning("Available memory could not be measured for model routing")

        for candidate in candidates:
            estimated_latency = int(candidate.get("estimated_latency_ms", 0))
            minimum_memory = int(candidate.get("min_available_memory_mb", 0))
            if estimated_latency > budget:
                continue
            if memory is not None and minimum_memory > memory:
                continue
            if memory is None and minimum_memory > 0:
                continue
            selected = {
                **route,
                **candidate,
                "_selection_reason": (
                    f"Selected {candidate.get('backend')}:{candidate.get('model')} "
                    f"within {budget} ms latency and {memory or 'unknown'} MB available memory"
                ),
            }
            if selected.get("backend") == "gemini":
                selected["model"] = model_name()
            return selected

        fallback = route.get("resource_fallback", {"backend": "template", "model": "template"})
        return {
            **route,
            **fallback,
            "fallback": "template",
            "_selection_reason": (
                f"No candidate met latency budget ({budget} ms) and memory limits"
            ),
        }

    def select_route(
        self,
        task: str,
        language: str,
        *,
        latency_budget_ms: int | None = None,
    ) -> dict[str, Any]:
        """Expose the selected configuration route for UI observability."""
        return self._configured_route(
            task, language, latency_budget_ms=latency_budget_ms
        )

    def _backend(self, name: str) -> ChatBackend:
        if name in self.backends:
            return self.backends[name]
        if name == "gemini":
            backend = GeminiBackend()
        else:
            raise RuntimeError(f"Model backend '{name}' is not configured.")
        self.backends[name] = backend
        return backend

    @staticmethod
    def _template(task: str, messages: list[dict[str, str]]) -> str:
        latest = next(
            (message["content"] for message in reversed(messages) if message["role"] == "user"),
            "",
        )
        if task == "anchoring_script":
            return f"Welcome, everyone. We now move on to {latest}. Please join me in welcoming our next speaker."
        if task in {"story_request", "story_followup"}:
            return (
                f"Here is the next part of our story: {latest}. "
                "The characters continue their journey together."
            )
        if task == "schedule_next":
            return f"Up next: {latest}."
        if task == "conversation_summary":
            prior = next(
                (message["content"] for message in messages if message["role"] == "system"),
                "",
            )
            return f"{prior} Earlier conversation: {latest}"[-1800:]
        return (
            "language service is currently unavailable. "
            "I’m here to help. Please try again shortly."
        )

    def stream(
        self,
        task: str,
        language: str,
        messages: list[dict[str, str]],
        *,
        response_schema: dict[str, Any] | None = None,
        latency_budget_ms: int | None = None,
        available_memory_mb: int | None = None,
    ) -> tuple[Iterator[str], RouteDecision]:
        """Return a token iterator and initial route metadata.

        Backends are attempted lazily while consuming the iterator, keeping HTTP
        response streaming available even when a backend fails before its first token.
        """
        route = self._configured_route(
            task,
            language,
            latency_budget_ms=latency_budget_ms,
            available_memory_mb=available_memory_mb,
        )
        primary = str(route.get("backend", "gemini"))
        model = str(
            route.get("model", self.settings.llm.get("default_model", model_name()))
        )
        fallback = str(route.get("fallback", "template"))
        retries = 0
        started = time.monotonic()
        decision = RouteDecision(
            task=task,
            backend=primary,
            model=model if primary != "template" else "template",
            reason=str(
                route.get(
                    "_selection_reason",
                    f"Configured route for {task} ({language})",
                )
            ),
            latency_ms=0.0,
        )

        def generate() -> Iterator[str]:
            errors: list[str] = []
            if primary != "template":
                for attempt in range(retries + 1):
                    attempt_yielded = False
                    try:
                        backend = self._backend(primary)
                        stream = (
                            backend.stream(model, messages)
                            if response_schema is None
                            else backend.stream(
                                model, messages, format_schema=response_schema
                            )
                        )
                        for token in stream:
                            attempt_yielded = True
                            yield token
                        elapsed = (time.monotonic() - started) * 1000
                        logger.info(
                            "Model route completed",
                            extra={"task": task, "backend": primary, "latency_ms": elapsed},
                        )
                        decision.latency_ms = elapsed
                        return
                    except Exception as exc:
                        failure_detail = str(exc)
                        failure_status = getattr(exc, "status_code", None)
                        decision.failure_detail = failure_detail
                        decision.failure_status = failure_status
                        errors.append(
                            f"{primary}: {type(exc).__name__}: {failure_detail}"
                        )
                        logger.warning(
                            "Model route attempt failed",
                            exc_info=True,
                            extra={
                                "task": task,
                                "backend": primary,
                                "attempt": attempt + 1,
                                "failure_status": failure_status,
                                "failure_detail": failure_detail,
                            },
                        )
                        if attempt_yielded:
                            yield STREAM_RESET
                            break
                        if attempt < retries:
                            self.sleeper(min(0.5 * (2**attempt) + random.random() * 0.1, 4))

            try:
                if fallback != "template" and fallback != primary:
                    fallback_backend = self._backend(fallback)
                    yield from fallback_backend.stream(model, messages)
                    decision.backend = fallback
                    decision.model = model
                    decision.reason = f"Fallback to {fallback} after {', '.join(errors)}"
                    decision.latency_ms = (time.monotonic() - started) * 1000
                    return
            except Exception as exc:
                failure_detail = str(exc)
                decision.failure_detail = failure_detail
                decision.failure_status = getattr(exc, "status_code", None)
                errors.append(f"{fallback}: {type(exc).__name__}: {failure_detail}")
                logger.exception(
                    "Fallback model route failed",
                    extra={
                        "task": task,
                        "failure_status": decision.failure_status,
                        "failure_detail": failure_detail,
                    },
                )

            reason = "Template fallback after " + (
                ", ".join(errors) if errors else "configured template route"
            )
            logger.info("Using template fallback", extra={"task": task, "reason": reason})
            decision.backend = "template"
            decision.model = "template"
            decision.reason = reason
            decision.latency_ms = (time.monotonic() - started) * 1000
            yield self._template(task, messages)

        return generate(), decision
