"""Conversation orchestration from user input through memory, models, and speech."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

from config.settings import AppSettings
from context.memory_manager import MemoryManager
from memory.repository import MemoryRepository
from modules.script_gen import build_anchoring_prompt, detect_script_task
from modules.story_gen import build_story_prompt, update_story_state
from modules.robot import RobotController, SimulatedRobot, gesture_for_response
from services.event_service import EventService, blocked_names, contains_blocked_name
from modules.tts import SentenceBuffer, SpeechSynthesizer, clean_tts_text
from nlu.intent_classifier import IntentClassifier
from orchestrator.router import ModelRouter, STREAM_RESET
from validation.output_validator import OutputValidator

logger = logging.getLogger(__name__)


def _next_chunk(iterator):
    """Read one item from a sync model stream without leaking StopIteration to asyncio."""
    try:
        return next(iterator)
    except StopIteration:
        return None


def _stream_to_queue(iterator, loop, queue, stop_event) -> None:
    """Move a blocking model iterator to a worker thread and enqueue its tokens."""
    try:
        while not stop_event.is_set():
            token = _next_chunk(iterator)
            if token is None:
                break
            future = asyncio.run_coroutine_threadsafe(queue.put(("token", token)), loop)
            while not stop_event.is_set():
                try:
                    future.result(timeout=0.25)
                    break
                except TimeoutError:
                    continue
                except Exception:
                    stop_event.set()
                    break
    except Exception as exc:
        if not stop_event.is_set():
            future = asyncio.run_coroutine_threadsafe(queue.put(("error", exc)), loop)
            try:
                future.result(timeout=1)
            except Exception:
                stop_event.set()
    finally:
        if not stop_event.is_set():
            future = asyncio.run_coroutine_threadsafe(queue.put(None), loop)
            try:
                future.result(timeout=1)
            except Exception:
                stop_event.set()


class ConversationPipeline:
    """Run one conversation turn and emit typed token, metadata, and audio events."""

    def __init__(
        self,
        settings: AppSettings,
        repository: MemoryRepository,
        memory: MemoryManager,
        router: ModelRouter,
        classifier: IntentClassifier,
        synthesizer: SpeechSynthesizer,
        validator: OutputValidator | None = None,
        robot: RobotController | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.memory = memory
        self.router = router
        self.classifier = classifier
        self.synthesizer = synthesizer
        self.validator = validator or OutputValidator()
        self.robot = robot or SimulatedRobot()
        self.event_service = EventService(repository.database_path)
        self._active_stream_stops: dict[str, threading.Event] = {}

    def stop_speaking(self, session_id: str) -> None:
        """Signal the active model/audio stream to stop at the next safe boundary."""
        stop_event = self._active_stream_stops.get(session_id)
        if stop_event is not None:
            stop_event.set()

    async def process(
        self,
        session_id: str,
        message: str,
        *,
        mode: str = "chat",
        language: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield WebSocket-friendly dictionaries for a single user turn."""
        if not message.strip():
            yield {"type": "error", "message": "Please enter a message or speak first."}
            return
        if len(message) > 8000:
            yield {"type": "error", "message": "That message is too long. Please shorten it."}
            return
        if mode not in {"chat", "story", "anchoring"}:
            yield {"type": "error", "message": "Choose Chat, Story, or Anchoring mode."}
            return

        await asyncio.to_thread(self.repository.create_session, session_id)
        await asyncio.to_thread(self.memory.compact_history, session_id)
        context = await asyncio.to_thread(
            self.memory.resolve_followup, session_id, message
        )
        story_active = context.active_story is not None
        intent = await asyncio.to_thread(
            self.classifier.classify, message, active_story=story_active
        )
        active_state = await asyncio.to_thread(
            self.repository.get_active_state, session_id
        )
        live_event = await asyncio.to_thread(
            self.event_service.get_for_session, session_id
        )
        if (
            (live_event or active_state["event"])
            and (
                mode == "anchoring"
                or intent.intent in {"anchoring_script", "schedule_next"}
            )
        ):
            context = replace(
                context,
                active_event=live_event or active_state["event"],
                continues_event=True,
            )
        if (
            mode == "story"
            and intent.intent != "story_request"
            and active_state["story"]
        ):
            context = replace(
                context,
                active_story=active_state["story"],
                continues_story=True,
            )
        selected_language = language or (
            context.preferences.get("language")
            if intent.language == "en" and context.preferences.get("language") == "ml"
            else intent.language
        )
        if selected_language == "mixed":
            selected_language = "ml"
        if selected_language not in {"en", "ml"}:
            selected_language = "en"
        if intent.intent == "language_switch":
            selected_language = "ml" if "malay" in message.casefold() or "മലയാളം" in message else "en"
            await asyncio.to_thread(
                self.repository.set_preferences,
                session_id,
                language=selected_language,
                style=str(context.preferences.get("style", "Professional")),
                voice=str(context.preferences.get("voice", "")),
            )

        await asyncio.to_thread(
            self.repository.add_message, session_id, "user", message
        )
        task, prompt, response_language = self._select_task(
            intent.intent,
            message,
            mode,
            selected_language,
            context.active_event,
            (
                context.active_story
                if context.continues_story and intent.intent != "story_request"
                else None
            ),
            context.preferences,
        )
        system_prompt = (
            "You are AVA, a friendly AI virtual anchoring assistant. "
            f"Reply in natural spoken {response_language} and respect user intent. "
            "Use provided memory only when relevant; do not claim to control unavailable hardware."
        )
        messages = self.memory.build_prompt(context, system_prompt, prompt)
        await asyncio.to_thread(
            self.repository.set_preferences,
            session_id,
            language=selected_language,
            style=str(context.preferences.get("style", "Professional")),
            voice=str(context.preferences.get("voice", "")),
        )

        yield {
            "type": "decision",
            "intent": intent.intent,
            "intent_source": intent.source,
            "language": selected_language,
            "task": task,
            "model": await asyncio.to_thread(
                self._route_model, task, selected_language
            ),
            "reason": f"{intent.source} intent; configured {task} route",
            "retrieved_context": [
                item["content"][:220] for item in context.retrieved[:3]
            ],
        }

        started = time.monotonic()
        full_text = ""
        decision = None
        voice = str(context.preferences.get("voice") or "") or None
        audio_count = 0
        defer_anchoring_output = task in {"anchoring_script", "schedule_next"}
        stop_speaking = threading.Event()
        self._active_stream_stops[session_id] = stop_speaking

        async def stream_generated(
            generation_messages: list[dict[str, str]],
        ) -> AsyncIterator[dict[str, Any]]:
            nonlocal full_text, decision, audio_count
            full_text = ""
            chunks, route_decision = self.router.stream(
                task, selected_language, generation_messages
            )
            decision = route_decision
            yield {
                "type": "model",
                "backend": route_decision.backend,
                "model": route_decision.model,
            }
            sentence_buffer = SentenceBuffer()
            loop = asyncio.get_running_loop()
            token_queue: asyncio.Queue[tuple[str, Any] | None] = asyncio.Queue(maxsize=8)
            stop_stream = stop_speaking
            producer = asyncio.create_task(
                asyncio.to_thread(
                    _stream_to_queue, chunks, loop, token_queue, stop_stream
                )
            )
            try:
                while True:
                    try:
                        queued = await asyncio.wait_for(token_queue.get(), timeout=0.2)
                    except TimeoutError:
                        if stop_stream.is_set():
                            break
                        continue
                    if queued is None:
                        break
                    kind, value = queued
                    if kind == "error":
                        raise value
                    token = str(value)
                    if token == STREAM_RESET:
                        full_text = ""
                        sentence_buffer = SentenceBuffer()
                        audio_count = 0
                        yield {"type": "reset"}
                        continue
                    full_text += token
                    if not defer_anchoring_output:
                        yield {"type": "token", "text": token}
                    for sentence in sentence_buffer.push(token):
                        if defer_anchoring_output or stop_stream.is_set():
                            continue
                        result = self.validator.validate(sentence, selected_language)
                        if result.valid:
                            try:
                                audio = await self.synthesizer.synthesize(
                                    result.text, selected_language, voice=voice
                                )
                                if stop_stream.is_set():
                                    break
                                audio_count += 1
                                yield {
                                    "type": "audio",
                                    "text": clean_tts_text(result.text),
                                    "url": f"/api/audio/{audio.name}",
                                }
                                notice = self.synthesizer.get_status(audio)
                                if notice:
                                    yield {"type": "audio_notice", "message": notice}
                            except Exception as exc:
                                logger.warning(
                                    "Speech synthesis unavailable: %s", type(exc).__name__
                                )
                                yield {
                                    "type": "audio_unavailable",
                                    "message": (
                                        "Text response is ready; speech is unavailable. "
                                        "Check Edge TTS, offline TTS dependencies, and ffmpeg."
                                    ),
                                }
                    await asyncio.sleep(0)
                await producer
            finally:
                if not producer.done():
                    stop_stream.set()
                    producer.cancel()
            remaining = sentence_buffer.flush()
            if remaining and not defer_anchoring_output:
                result = self.validator.validate(remaining, selected_language)
                if result.valid:
                    try:
                        audio = await self.synthesizer.synthesize(
                            result.text, selected_language, voice=voice
                        )
                        if stop_stream.is_set():
                            return
                        audio_count += 1
                        yield {
                            "type": "audio",
                            "text": clean_tts_text(result.text),
                            "url": f"/api/audio/{audio.name}",
                        }
                    except Exception as exc:
                        logger.warning(
                            "Speech synthesis unavailable: %s", type(exc).__name__
                        )
                        yield {
                            "type": "audio_unavailable",
                            "message": "Text response is ready; speech is unavailable.",
                        }
                    else:
                        notice = self.synthesizer.get_status(audio)
                        if notice:
                            yield {"type": "audio_notice", "message": notice}

        async for event in stream_generated(messages):
            yield event

        if defer_anchoring_output and context.active_event:
            latest_event = await asyncio.to_thread(
                self.event_service.get_for_session, session_id
            )
            if (
                latest_event
                and latest_event["state_version"]
                != context.active_event.get("state_version")
            ):
                context = replace(context, active_event=latest_event)
                task, prompt, response_language = self._select_task(
                    intent.intent,
                    message,
                    mode,
                    selected_language,
                    latest_event,
                    context.active_story if context.continues_story else None,
                    context.preferences,
                )
                messages = self.memory.build_prompt(context, system_prompt, prompt)
                yield {"type": "reset"}
                async for event in stream_generated(messages):
                    yield event

        if defer_anchoring_output and stop_speaking.is_set():
            self._active_stream_stops.pop(session_id, None)
            yield {
                "type": "error",
                "message": "The announcement stopped because the live schedule changed.",
            }
            return

        validation = self.validator.validate(full_text, selected_language)
        event_blocked_name = (
            contains_blocked_name(full_text, blocked_names(context.active_event))
            if defer_anchoring_output and context.active_event
            else None
        )
        if not validation.valid or event_blocked_name:
            correction_messages = [
                *messages,
                {"role": "assistant", "content": full_text},
                {
                    "role": "user",
                    "content": (
                        "Rewrite the response. "
                        + (
                            f"Never mention {event_blocked_name}; that person is absent, removed, or skipped. "
                            if event_blocked_name
                            else f"Fix this issue: {validation.reason}. "
                        )
                        + "Follow the requested language and spoken length requirements."
                    ),
                },
            ]
            yield {"type": "reset"}
            audio_count = 0
            async for event in stream_generated(correction_messages):
                yield event
            validation = self.validator.validate(full_text, selected_language)
            event_blocked_name = (
                contains_blocked_name(full_text, blocked_names(context.active_event))
                if defer_anchoring_output and context.active_event
                else None
            )

        if not validation.valid or event_blocked_name:
            if defer_anchoring_output and context.active_event:
                full_text = self._safe_event_fallback(context.active_event, selected_language)
            else:
                full_text = self._safe_fallback(task, selected_language, message)
            validation = self.validator.validate(full_text, selected_language)
            yield {"type": "reset"}
            yield {"type": "token", "text": full_text}

        full_text = validation.text if validation.valid else full_text
        if defer_anchoring_output and not event_blocked_name and full_text:
            yield {"type": "token", "text": full_text}
        await asyncio.to_thread(
            self.repository.add_message, session_id, "assistant", full_text
        )
        if task in {"story_request", "story_followup"}:
            new_state = update_story_state(context.active_story, full_text, message)
            await asyncio.to_thread(
                self.repository.save_story,
                session_id,
                title=str(new_state["title"]),
                characters=list(new_state["characters"]),
                setting=str(new_state["setting"]),
                plot_summary=str(new_state["plot_summary"]),
                last_paragraph=str(new_state["last_paragraph"]),
                status=str(new_state["status"]),
                story_id=(str(context.active_story["id"]) if context.active_story else None),
            )

        gesture = gesture_for_response(task, intent.intent, message)
        if stop_speaking.is_set():
            robot_ok = False
            gesture = "stop"
        else:
            robot_results = await asyncio.gather(
                self.robot.speak(full_text),
                self.robot.gesture(gesture),
                return_exceptions=True,
            )
            robot_ok = not any(isinstance(result, Exception) for result in robot_results)
        if not robot_ok:
            logger.warning("Robot action failed; text and speech continue")
        yield {
            "type": "robot",
            "gesture": gesture,
            "connected": self.robot.is_connected(),
            "ok": robot_ok,
        }

        if audio_count == 0 and not stop_speaking.is_set():
            sentence_buffer = SentenceBuffer()
            sentences = sentence_buffer.push(full_text)
            remainder = sentence_buffer.flush()
            if remainder:
                sentences.append(remainder)
            for sentence in sentences:
                if stop_speaking.is_set():
                    break
                try:
                    cache_version = (
                        context.active_event.get("state_version")
                        if defer_anchoring_output and context.active_event
                        else None
                    )
                    options: dict[str, Any] = {"voice": voice}
                    if cache_version is not None:
                        options["cache_version"] = cache_version
                    audio = await self.synthesizer.synthesize(
                        sentence, selected_language, **options
                    )
                    yield {
                        "type": "audio",
                        "text": clean_tts_text(sentence),
                        "url": f"/api/audio/{audio.name}",
                    }
                    notice = self.synthesizer.get_status(audio)
                    if notice:
                        yield {"type": "audio_notice", "message": notice}
                except Exception as exc:
                    logger.warning(
                        "Speech synthesis unavailable: %s", type(exc).__name__
                    )
                    yield {
                        "type": "audio_unavailable",
                        "message": "Text response is ready; speech is unavailable.",
                    }

        if decision is None:
            raise RuntimeError("Model router did not produce a route decision.")
        self._active_stream_stops.pop(session_id, None)
        yield {
            "type": "complete",
            "text": full_text,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "model": decision.model,
            "backend": decision.backend,
            "route_reason": decision.reason,
        }

    def _select_task(
        self,
        intent: str,
        message: str,
        mode: str,
        language: str,
        event: dict[str, Any] | None,
        story: dict[str, Any] | None,
        preferences: dict[str, Any],
    ) -> tuple[str, str, str]:
        """Route the user request to a specialized prompt and generation task."""
        if mode == "story" or intent in {"story_request", "story_followup"}:
            task = "story_followup" if story else "story_request"
            return (
                task,
                build_story_prompt(message, language=language, story=story),
                "Malayalam" if language == "ml" else "English",
            )
        if (
            mode == "anchoring"
            or intent == "anchoring_script"
            or (intent == "schedule_next" and event is not None)
            or (intent == "command" and event is not None)
        ):
            current = None
            next_item = None
            schedule = event.get("items")
            if isinstance(schedule, list):
                ordered = sorted(schedule, key=lambda item: (item.get("order_index", 0), item.get("id", 0)))
                eligible = [
                    item for item in ordered
                    if item.get("status") in {"pending", "arrived", "on_stage"}
                ]
                current = next(
                    (item for item in eligible if item.get("id") == event.get("current_item_id")),
                    eligible[0] if eligible else None,
                )
                if current:
                    current_position = eligible.index(current)
                    next_item = eligible[current_position + 1] if current_position + 1 < len(eligible) else None
                safe_schedule = [
                    {
                        "title": item["title"],
                        "type": item["type"],
                        "person_name": item["person_name"],
                        "designation": item["designation"],
                        "status": item["status"],
                    }
                    for item in eligible
                ]
                event = {
                    key: event.get(key, "")
                    for key in ("name", "type", "date", "venue", "chief_guest", "tone", "language", "state_version")
                } | {"items": safe_schedule}
            else:
                try:
                    legacy_schedule = json.loads(event.get("schedule_json", "[]"))
                    current_index = int(event.get("current_item_index", 0))
                    if legacy_schedule and 0 <= current_index < len(legacy_schedule):
                        current = legacy_schedule[current_index]
                        next_item = legacy_schedule[current_index + 1] if current_index + 1 < len(legacy_schedule) else None
                except (TypeError, ValueError, json.JSONDecodeError):
                    logger.exception("Invalid persisted event schedule")
            task = "schedule_next" if intent == "schedule_next" else "anchoring_script"
            script_task = (
                "schedule_next"
                if intent == "schedule_next"
                else detect_script_task(message)
            )
            return (
                task,
                build_anchoring_prompt(
                    script_task,
                    language,
                    event=event,
                    current=current,
                    next_item=next_item,
                    tone=str(preferences.get("style", event.get("tone", "Professional"))),
                    user_prompt=message,
                ),
                "Malayalam" if language == "ml" else "English",
            )
        return "chat_question", message, "Malayalam" if language == "ml" else "English"

    def _route_model(self, task: str, language: str) -> str:
        route = self.router.select_route(task, language)
        return str(route.get("model", route.get("backend", "template")))

    @staticmethod
    def _safe_fallback(task: str, language: str, message: str) -> str:
        """Produce a short safe response when both model attempts fail validation."""
        if language == "ml":
            return "ക്ഷമിക്കണം, ഇപ്പോൾ മറുപടി തയ്യാറാക്കാൻ കഴിഞ്ഞില്ല. ദയവായി വീണ്ടും ശ്രമിക്കുക."
        if task.startswith("story"):
            return f"The story will continue from where we left off. {message[:100]}"
        return "Sorry, I could not prepare a suitable response. Please try again."

    @staticmethod
    def _safe_event_fallback(event: dict[str, Any], language: str) -> str:
        """Build an announcement only from currently eligible live schedule items."""
        items = [
            item for item in event.get("items", [])
            if item.get("status") in {"pending", "arrived", "on_stage"}
        ]
        current_id = event.get("current_item_id")
        current = next((item for item in items if item.get("id") == current_id), None)
        if current is None and items:
            current = items[0]
        if current is None:
            return (
                "ഇപ്പോൾ പ്രഖ്യാപിക്കാനുള്ള അടുത്ത പരിപാടിയില്ല."
                if language == "ml"
                else "There is no eligible schedule item to announce right now."
            )
        title = str(current["title"])
        for name in blocked_names(event):
            title = re.sub(re.escape(name), "", title, flags=re.IGNORECASE)
        title = re.sub(r"\s+", " ", title).strip(" ,.-")
        if not title:
            title = "അടുത്ത പരിപാടി" if language == "ml" else "the next scheduled item"
        if language == "ml":
            return f"ഇപ്പോൾ നമുക്ക് {title}ലേക്ക് കടക്കാം."
        return f"We now move to {title}."
