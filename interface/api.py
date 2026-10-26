"""FastAPI API and WebSocket application for AVA."""

from __future__ import annotations

import logging
import asyncio
import json
import os
import re
import tempfile
import time
from datetime import time as datetime_time
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.staticfiles import StaticFiles

from config.settings import AppSettings, load_settings
from context.memory_manager import MemoryManager
from memory.db import connection_scope, initialize_database
from memory.repository import MemoryRepository
from modules.tts import SpeechSynthesizer
from modules.stt import SpeechRecognizer
from modules.script_gen import (
    SCRIPT_SEGMENT_TYPES,
    build_structured_script_prompt,
    generate_clarification_questions,
    validate_script_segments,
)
from modules.robot import SerialRobot, SimulatedRobot
from modules.health import collect_health, log_health
from nlu.intent_classifier import IntentClassifier
from orchestrator.pipeline import ConversationPipeline
from orchestrator.router import ModelRouter
from services.event_service import (
    EventNotFoundError,
    EventService,
    blocked_names,
    contains_blocked_name,
    drop_blocked_name_sentences,
    parse_operator_command,
)
from modules.speaker_capture import SpeakerCapture
from modules.summarizer import SpeakerSummarizer
from llm import health_check as gemini_health_check

PROJECT_ROOT = Path(__file__).resolve().parent.parent
logger = logging.getLogger("ava")


class SessionResponse(BaseModel):
    """Session creation response."""

    session_id: str


class EventCreate(BaseModel):
    """Event data submitted from the anchoring setup form."""

    session_id: str
    name: str = Field(min_length=1, max_length=200)
    event_type: str = Field(default="event", max_length=80)
    venue: str = Field(default="", max_length=200)
    host: str = Field(default="", max_length=200)
    sponsors: list[str] = Field(default_factory=list, max_length=50)
    audience: str = Field(default="", max_length=300)
    style: str = Field(default="Professional host", max_length=80)
    language: str = Field(default="en", pattern="^(en|ml)$")
    special_instructions: str = Field(default="", max_length=3000)
    event_date: str = Field(default="", max_length=30)
    event_start_time: str = Field(default="", max_length=5)
    event_end_time: str = Field(default="", max_length=5)
    chief_guest: str = Field(default="", max_length=200)
    chief_guest_title: str = Field(default="", max_length=200)
    speakers: list[str] = Field(default_factory=list, max_length=40)
    tone: str = Field(default="Professional", max_length=40)
    schedule: list[dict[str, Any]] = Field(default_factory=list, max_length=100)


class ScriptGenerateRequest(BaseModel):
    """Structured full-script or targeted-segment generation request."""

    event_id: int
    targets: list[dict[str, Any]] | None = Field(default=None, max_length=200)
    replace_edited_ids: list[str] = Field(default_factory=list, max_length=200)


class EventCloseRequest(BaseModel):
    """Request to generate a notes-grounded closing before completing an event."""

    language: str = Field(default="en")
    replace_edited: bool = False


class AnchoringClarifyRequest(BaseModel):
    """Event details and schedule submitted for AI gap review and clarifying questions."""

    session_id: str = Field(default="default")
    brief: dict[str, Any] = Field(default_factory=dict)
    event: dict[str, Any] = Field(default_factory=dict)
    schedule: list[dict[str, Any]] = Field(default_factory=list)
    language: str = Field(default="en")


class PreferencesUpdate(BaseModel):
    """Session preferences update."""

    language: str = Field(pattern="^(en|ml)$")
    style: str = Field(default="Professional", max_length=50)
    voice: str = Field(default="", max_length=100)


def build_pipeline(settings: AppSettings) -> ConversationPipeline:
    """Create services once and share them across application requests."""
    initialize_database(settings)
    repository = MemoryRepository(settings.database_path)
    memory_config = settings.memory
    router = ModelRouter(settings)

    def summarize_history(summary: str, messages: list[dict[str, Any]]) -> str:
        history = "\n".join(
            f"{item['role']}: {item['content']}" for item in messages
        )
        prompt_messages = [
            {
                "role": "system",
                "content": (
                    "Update a concise factual conversation summary. Preserve user "
                    "preferences, active event/story names, and decisions."
                ),
            },
            {
                "role": "user",
                "content": f"Current summary:\n{summary}\nNew older messages:\n{history}",
            },
        ]
        chunks, _ = router.stream(
            "conversation_summary", "en", prompt_messages
        )
        return "".join(chunks).strip()

    memory = MemoryManager(
        repository,
        short_term_turns=int(memory_config.get("short_term_turns", 12)),
        retrieval_limit=int(memory_config.get("retrieval_limit", 5)),
        token_budget=int(memory_config.get("token_budget", 4096)),
        summarizer=summarize_history,
    )
    speech = settings.speech
    cache = Path(str(speech.get("cache_dir", "audio/cache")))
    if not cache.is_absolute():
        cache = PROJECT_ROOT / cache
    synthesizer = SpeechSynthesizer(
        voices=speech.get("voices", {}),
        cache_dir=cache,
        provider=str(speech.get("tts_provider", "edge")),
        malayalam_model=str(
            speech.get("offline_malayalam_model", "facebook/mms-tts-mal")
        ),
    )
    robot_config = settings.robot
    robot: SimulatedRobot | SerialRobot
    if robot_config.get("mode") == "serial" and robot_config.get("serial_port"):
        robot = SerialRobot(
            str(robot_config["serial_port"]),
            int(robot_config.get("baud_rate", 115200)),
        )
    else:
        if robot_config.get("mode") == "serial":
            logger.warning("Serial robot selected without a port; using the simulator")
        robot = SimulatedRobot()
    return ConversationPipeline(
        settings,
        repository,
        memory,
        router,
        IntentClassifier(router),
        synthesizer,
        robot=robot,
    )


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Initialize runtime services and structured logging on startup."""
    settings: AppSettings = application.state.settings
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    application.state.pipeline = build_pipeline(settings)
    application.state.event_service = EventService(settings.database_path)
    application.state.event_subscribers = {}
    application.state.recognizer = SpeechRecognizer(
        model_size=str(settings.speech.get("stt_model", "small")),
        language=str(settings.speech.get("language", "en")),
        vad_filter=bool(settings.speech.get("vad_filter", True)),
    )
    logger.info("AVA initialized")
    application.state.health = await asyncio.to_thread(collect_health, settings)
    application.state.health_checked_at = 0.0
    await asyncio.to_thread(log_health, application.state.health)
    yield


def create_app(settings: AppSettings | None = None) -> FastAPI:
    """Build an application instance, supporting isolated test settings."""
    application = FastAPI(title="AVA", version="1.0.0", lifespan=lifespan)
    application.state.settings = settings or load_settings()
    application.mount(
        "/static",
        StaticFiles(directory=PROJECT_ROOT / "static"),
        name="static",
    )

    async def broadcast_event(event: dict[str, Any], *, action: str = "updated") -> None:
        """Notify every connected UI for the event's session with its new version."""
        subscribers = application.state.event_subscribers.get(event["session_id"], set())
        payload = {
            "type": "event_state_changed",
            "action": action,
            "event": event,
            "state_version": event["state_version"],
        }
        for queue in tuple(subscribers):
            try:
                await queue.put(payload)
            except RuntimeError:
                logger.warning("Event update subscriber closed", extra={"event_id": event["id"]})

    @application.exception_handler(Exception)
    async def friendly_server_error(request: Request, exc: Exception) -> JSONResponse:
        """Log unexpected HTTP failures and return a non-sensitive user message."""
        logger.error(
            "Unhandled request error",
            exc_info=(type(exc), exc, exc.__traceback__),
            extra={"path": request.url.path},
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "AVA could not complete that request. Please try again."},
        )

    @application.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        """Serve the single-page application."""
        return FileResponse(PROJECT_ROOT / "static" / "index.html")

    @application.get("/control", include_in_schema=False)
    async def operator_console() -> FileResponse:
        """Serve the live event operator console."""
        return FileResponse(PROJECT_ROOT / "static" / "control.html")

    @application.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        """Avoid a browser-console 404 when the browser requests its default icon."""
        return Response(status_code=204)

    @application.get("/api/health")
    async def health(refresh: bool = False) -> dict[str, Any]:
        """Report live Gemini generation, speech synthesis, and runtime status."""
        is_stale = time.monotonic() - application.state.health_checked_at > 300
        if refresh or is_stale:
            checks = await asyncio.to_thread(
                collect_health, application.state.settings
            )
            checks["gemini"] = await gemini_health_check()
            try:
                audio_path = await application.state.pipeline.synthesizer.synthesize(
                    "AVA speech system health check.",
                    "en",
                    cache_version=f"health-{time.time_ns()}",
                )
                tts_message = (
                    application.state.pipeline.synthesizer.get_status(audio_path)
                    or "Speech synthesis succeeded."
                )
                tts_available = audio_path.is_file()
                audio_path.unlink(missing_ok=True)
                checks["tts"] = {
                    "available": tts_available,
                    "message": tts_message,
                }
            except Exception as exc:
                logger.exception("TTS health check failed")
                checks["tts"] = {
                    "available": False,
                    "message": f"Speech synthesis failed ({type(exc).__name__}).",
                }
            application.state.health = checks
            application.state.health_checked_at = time.monotonic()
        else:
            checks = application.state.health
        all_ready = all(check["available"] for check in checks.values())
        return {
            "status": "ok" if all_ready else "degraded",
            "api": {"available": True, "message": "AVA API is running"},
            "checks": checks,
        }

    @application.get("/health")
    async def liveness() -> dict[str, str]:
        """Maintain the original lightweight liveness endpoint."""
        return {"status": "ok"}

    @application.post("/api/sessions", response_model=SessionResponse)
    async def create_session() -> SessionResponse:
        """Create a durable conversation session."""
        session_id = await asyncio.to_thread(
            application.state.pipeline.repository.create_session
        )
        return SessionResponse(session_id=session_id)

    @application.post("/api/transcribe")
    async def transcribe_audio(
        audio: UploadFile = File(...),
        language: str = Form("en"),
    ) -> dict[str, str]:
        """Transcribe one browser-recorded audio segment using Faster-Whisper."""
        if language not in {"en", "ml"}:
            raise HTTPException(status_code=422, detail="Language must be 'en' or 'ml'.")
        contents = await audio.read(25 * 1024 * 1024 + 1)
        if not contents:
            raise HTTPException(status_code=400, detail="Audio recording is empty.")
        if len(contents) > 25 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Audio recording exceeds 25 MB.")
        suffix = Path(audio.filename or "recording.webm").suffix
        if suffix.lower() not in {".webm", ".wav", ".mp3", ".mp4", ".ogg", ".m4a"}:
            suffix = ".webm"
        handle, temporary_name = tempfile.mkstemp(suffix=suffix)
        try:
            def write_recording() -> None:
                with os.fdopen(handle, "wb") as recording:
                    recording.write(contents)

            await asyncio.to_thread(write_recording)
            result = await asyncio.to_thread(
                application.state.recognizer.transcribe, temporary_name, language
            )
            return {"text": result.text, "language": result.language}
        except Exception as exc:
            logger.exception("Speech recognition failed")
            raise HTTPException(
                status_code=503,
                detail="Speech recognition is unavailable. Check the STT model and audio setup.",
            ) from exc
        finally:
            Path(temporary_name).unlink(missing_ok=True)

    @application.websocket("/ws/{session_id}")
    async def conversation_socket(websocket: WebSocket, session_id: str) -> None:
        """Stream conversation events over WebSocket."""
        await websocket.accept()
        pipeline: ConversationPipeline = application.state.pipeline
        await asyncio.to_thread(pipeline.repository.create_session, session_id)
        outgoing: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=64)
        incoming: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=8)

        async def send_events() -> None:
            while True:
                event = await outgoing.get()
                if event is None:
                    return
                await websocket.send_json(event)

        async def process_requests() -> None:
            while True:
                payload = await incoming.get()
                if payload is None:
                    return
                try:
                    async for event in pipeline.process(
                        session_id,
                        str(payload.get("message", "")),
                        mode=str(payload.get("mode", "chat")),
                        language=payload.get("language"),
                    ):
                        await outgoing.put(event)
                except Exception:
                    logger.exception(
                        "Conversation request failed",
                        extra={"session_id": session_id},
                    )
                    await outgoing.put(
                        {
                            "type": "error",
                            "message": "AVA could not complete that request. Please try again.",
                        }
                    )

        sender = asyncio.create_task(send_events())
        processor = asyncio.create_task(process_requests())
        subscribers = application.state.event_subscribers.setdefault(session_id, set())
        subscribers.add(outgoing)
        live_event = await asyncio.to_thread(
            application.state.event_service.get_for_session, session_id
        )
        if live_event:
            await outgoing.put(
                {
                    "type": "event_state_changed",
                    "action": "initial",
                    "event": live_event,
                    "state_version": live_event["state_version"],
                }
            )
        try:
            while True:
                raw_payload = await websocket.receive_text()
                try:
                    payload = json.loads(raw_payload)
                except json.JSONDecodeError:
                    await outgoing.put(
                        {"type": "error", "message": "Send a valid JSON object."}
                    )
                    continue
                if not isinstance(payload, dict):
                    await outgoing.put(
                        {"type": "error", "message": "Send a JSON object with a message."}
                    )
                    continue
                if payload.get("type") == "ping":
                    await outgoing.put(
                        {"type": "pong", "time": payload.get("time")}
                    )
                    continue
                if payload.get("type") == "stop":
                    pipeline.stop_speaking(session_id)
                    await outgoing.put({"type": "speech_stop"})
                    continue
                if payload.get("type") in {"subscribe_event", "event_command"}:
                    continue
                await incoming.put(payload)
        except WebSocketDisconnect:
            logger.info("Conversation socket disconnected", extra={"session_id": session_id})
        except Exception:
            logger.exception(
                "WebSocket handler failed", extra={"session_id": session_id}
            )
            try:
                await websocket.send_json(
                    {"type": "error", "message": "Connection error. Please reconnect."}
                )
            except Exception:
                logger.debug("Unable to send final WebSocket error", exc_info=True)
        finally:
            subscribers.discard(outgoing)
            if not subscribers:
                application.state.event_subscribers.pop(session_id, None)
            for task in (processor, sender):
                task.cancel()
            await asyncio.gather(processor, sender, return_exceptions=True)

    @application.get("/api/memory/{session_id}")
    async def memory_state(session_id: str) -> dict[str, Any]:
        """Expose summarized session state for the sidebar."""
        pipeline: ConversationPipeline = application.state.pipeline
        if not await asyncio.to_thread(pipeline.repository.session_exists, session_id):
            raise HTTPException(status_code=404, detail="Conversation session not found.")
        state, summary, preferences, message_count = await asyncio.gather(
            asyncio.to_thread(pipeline.repository.get_active_state, session_id),
            asyncio.to_thread(pipeline.repository.get_summary, session_id),
            asyncio.to_thread(pipeline.repository.get_preferences, session_id),
            asyncio.to_thread(pipeline.repository.get_message_count, session_id),
        )
        return {
            "summary": summary,
            "preferences": preferences,
            "event": state["event"],
            "story": state["story"],
            "message_count": message_count,
        }

    @application.put("/api/preferences/{session_id}")
    async def update_preferences(
        session_id: str, preferences: PreferencesUpdate
    ) -> dict[str, str]:
        """Persist language, style, and voice preferences."""
        pipeline: ConversationPipeline = application.state.pipeline
        await asyncio.to_thread(pipeline.repository.create_session, session_id)
        await asyncio.to_thread(
            pipeline.repository.set_preferences,
            session_id,
            language=preferences.language,
            style=preferences.style,
            voice=preferences.voice,
        )
        return {"status": "saved"}

    @application.get("/api/preferences/{session_id}")
    async def read_preferences(session_id: str) -> dict[str, str]:
        """Return the persisted language, delivery style, and voice."""
        pipeline: ConversationPipeline = application.state.pipeline
        return await asyncio.to_thread(pipeline.repository.get_preferences, session_id)

    @application.post("/api/events")
    async def create_event(event: EventCreate) -> dict[str, Any]:
        """Persist event details and establish the session's active event."""
        pipeline: ConversationPipeline = application.state.pipeline
        await asyncio.to_thread(pipeline.repository.create_session, event.session_id)
        schedule = event.schedule
        event_times: dict[str, datetime_time] = {}
        for field_name, field_value in (
            ("event_start_time", event.event_start_time),
            ("event_end_time", event.event_end_time),
        ):
            if field_value:
                try:
                    event_times[field_name] = datetime_time.fromisoformat(field_value)
                except ValueError as exc:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Invalid {field_name.replace('_', ' ')}: {field_value}",
                    ) from exc
        if (
            "event_start_time" in event_times
            and "event_end_time" in event_times
            and event_times["event_end_time"] <= event_times["event_start_time"]
        ):
            raise HTTPException(
                status_code=422,
                detail="Event end time must be later than its start time.",
            )
        for item in schedule:
            if not item.get("title", "").strip():
                raise HTTPException(
                    status_code=422, detail="Every schedule item needs a title."
                )
            try:
                if item.get("time"):
                    datetime_time.fromisoformat(item["time"])
                if int(item.get("duration_minutes") or 0) < 0:
                    raise ValueError("Duration cannot be negative.")
            except ValueError as exc:
                raise HTTPException(
                    status_code=422,
                    detail=str(exc) if "duration" in str(exc).lower() else f"Invalid schedule time: {item['time']}",
                ) from exc

        def persist_event() -> int:
            with connection_scope(pipeline.repository.database_path) as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO events(
                        name, event_type, venue, host, sponsors_json, audience, style,
                        special_instructions, event_date, event_start_time, event_end_time,
                        chief_guest, chief_guest_title, speakers_json,
                        schedule_json, tone, session_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (
                        event.name,
                        event.event_type,
                        event.venue,
                        event.host,
                        json.dumps(event.sponsors, ensure_ascii=False),
                        event.audience,
                        event.style,
                        event.special_instructions,
                        event.event_date,
                        event.event_start_time,
                        event.event_end_time,
                        event.chief_guest,
                        event.chief_guest_title,
                        json.dumps(event.speakers, ensure_ascii=False),
                        json.dumps(schedule, ensure_ascii=False),
                        event.tone,
                        event.session_id,
                    ),
                )
                event_id = int(cursor.lastrowid or 0)
                for item in schedule:
                    connection.execute(
                        """
                        INSERT INTO schedule(
                            event_id, event_time, title, event_type, participant,
                            designation, participant_details, location, duration_minutes,
                            performance_style
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event_id,
                            item.get("time", ""),
                            item.get("title", ""),
                            item.get("type", "other"),
                            item.get("participant", ""),
                            item.get("designation", ""),
                            item.get("participant_details", ""),
                            item.get("location", ""),
                            int(item.get("duration_minutes") or 0),
                            item.get("performance_style", "event_style"),
                        ),
                    )
                return event_id

        event_id = await asyncio.to_thread(persist_event)
        live_event = await asyncio.to_thread(
            application.state.event_service.create_event,
            event.session_id,
            name=event.name,
            event_type=event.event_type,
            date=event.event_date,
            event_start_time=event.event_start_time,
            event_end_time=event.event_end_time,
            venue=event.venue,
            host=event.host,
            sponsors_json=json.dumps(event.sponsors, ensure_ascii=False),
            audience=event.audience,
            style=event.style,
            special_instructions=event.special_instructions,
            tone=event.tone,
            language=event.language,
            chief_guest=event.chief_guest,
            chief_guest_title=event.chief_guest_title,
            schedule=[
                {
                    "title": item["title"],
                    "type": item.get("type", "other"),
                    "person_name": item.get("person_name", item.get("participant", "")),
                    "designation": item.get("designation", ""),
                    "planned_duration": int(item.get("duration_minutes", item.get("planned_duration", 0)) or 0),
                    "scheduled_time": item.get("time", ""),
                    "location": item.get("location", ""),
                    "participant_details": item.get("participant_details", ""),
                    "performance_style": item.get("performance_style", "event_style"),
                    "notes": item.get("notes", ""),
                    "transcript_language": item.get("transcript_language", "auto"),
                }
                for item in schedule
            ],
            event_id=event_id,
        )
        await asyncio.to_thread(
            pipeline.repository.set_active_state,
            event.session_id,
            event_id=event_id,
        )
        await broadcast_event(live_event, action="created")
        return {
            "id": event_id,
            "name": event.name,
            "schedule": schedule,
            "state_version": live_event["state_version"],
        }

    @application.post("/api/anchoring/clarify")
    async def clarify_anchoring_details(
        request: AnchoringClarifyRequest,
    ) -> dict[str, Any]:
        """Review event outline with Gemini/gap analyzer and return clarifying questions."""
        return await generate_clarification_questions(
            event=request.event,
            brief=request.brief,
            schedule=request.schedule,
            language=request.language,
        )

    @application.get("/api/anchoring/scripts/{event_id}")
    async def get_anchoring_script(event_id: int) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                application.state.event_service.get_script, event_id
            )
        except EventNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @application.post("/api/anchoring/scripts/generate")
    async def generate_anchoring_script(
        request: ScriptGenerateRequest,
    ) -> dict[str, Any]:
        service: EventService = application.state.event_service
        try:
            event = await asyncio.to_thread(service.get_event, request.event_id)
            existing = await asyncio.to_thread(service.get_script, request.event_id)
        except EventNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        eligible = [
            item for item in event["items"]
            if item["status"] not in {"absent", "skipped", "removed"}
        ]
        targets = request.targets
        if targets is None:
            targets = [
                {"id": "opening", "type": "opening", "itemId": None},
                *[
                    {
                        "id": f"{'break' if item['type'] == 'break' else 'intro'}-{item['id']}",
                        "type": "break" if item["type"] == "break" else "intro",
                        "itemId": item["id"],
                    }
                    for item in eligible
                ],
                *[
                    {
                        "id": f"transition-{left['id']}-{right['id']}",
                        "type": "transition",
                        "itemId": right["id"],
                    }
                    for left, right in zip(eligible, eligible[1:])
                ],
                {"id": "closing", "type": "closing", "itemId": None},
            ]
        allowed_targets: dict[str, dict[str, Any]] = {
            "opening": {"id": "opening", "type": "opening", "itemId": None},
            "closing": {"id": "closing", "type": "closing", "itemId": None},
        }
        for item in eligible:
            item_id = item["id"]
            kind = "break" if item["type"] == "break" else "intro"
            allowed_targets[f"{kind}-{item_id}"] = {
                "id": f"{kind}-{item_id}", "type": kind, "itemId": item_id,
            }
            allowed_targets[f"recovery-{item_id}"] = {
                "id": f"recovery-{item_id}", "type": "transition", "itemId": item_id,
            }
        for left, right in zip(eligible, eligible[1:]):
            identifier = f"transition-{left['id']}-{right['id']}"
            allowed_targets[identifier] = {
                "id": identifier, "type": "transition", "itemId": right["id"],
            }
        valid_segment_ids = {
            "opening",
            "closing",
            *[
                f"{'break' if item['type'] == 'break' else 'intro'}-{item['id']}"
                for item in eligible
            ],
            *[
                f"transition-{left['id']}-{right['id']}"
                for left, right in zip(eligible, eligible[1:])
            ],
            *[f"recovery-{item['id']}" for item in eligible],
        }
        normalized_targets: list[dict[str, Any]] = []
        for target in targets:
            if not isinstance(target, dict):
                raise HTTPException(status_code=422, detail="Every target must be an object.")
            identifier = str(target.get("id", ""))
            approved = allowed_targets.get(identifier)
            if not approved or target != approved:
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid or ineligible script segment target: {identifier}",
                )
            normalized_targets.append(approved)
        if len({target["id"] for target in normalized_targets}) != len(normalized_targets):
            raise HTTPException(status_code=422, detail="Script target ids must be unique.")

        existing_by_id = {segment["id"]: segment for segment in existing["segments"]}
        protected_ids = [
            target["id"] for target in normalized_targets
            if existing_by_id.get(target["id"], {}).get("edited")
            and target["id"] not in request.replace_edited_ids
        ]
        safe_existing = [
            segment for segment in existing["segments"]
            if segment["id"] in valid_segment_ids
        ]
        pending_targets = [
            target for target in normalized_targets if target["id"] not in protected_ids
        ]
        if not pending_targets:
            if safe_existing != existing["segments"]:
                saved = await asyncio.to_thread(
                    service.save_script, request.event_id, safe_existing
                )
                return {
                    **saved,
                    "protected_ids": protected_ids,
                    "generated_ids": [],
                }
            return {
                "segments": existing["segments"],
                "protected_ids": protected_ids,
                "generated_ids": [],
                "version": existing["version"],
            }

        prompt = build_structured_script_prompt(
            event,
            event["items"],
            targets=pending_targets,
            existing_segments=safe_existing,
        )
        language = event.get("language", "en")

        def generate_segments() -> tuple[str, str, str]:
            chunks, decision = application.state.pipeline.router.stream(
                "anchoring_script",
                language,
                [{"role": "user", "content": prompt}],
            )
            raw = "".join(chunks).strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
            return raw, decision.backend, decision.model

        try:
            raw, backend, model = await asyncio.to_thread(generate_segments)
        except Exception as exc:
            logger.exception("Structured anchoring script generation failed")
            status_code = 429 if getattr(exc, "status_code", None) == 429 else 503
            if status_code == 429:
                detail = "Gemini is rate limiting requests. Wait briefly and retry."
            elif "api key" in str(exc).lower() or "credential" in str(exc).lower():
                detail = "Gemini is not configured. Set the server-side Gemini API key, then retry."
            else:
                detail = "Gemini could not generate the script. Check the connection and retry."
            raise HTTPException(status_code=status_code, detail=detail) from exc
        if backend == "template":
            raise HTTPException(
                status_code=503,
                detail="Gemini is unavailable (missing key, rate limit, or network issue). Check the server API key, wait briefly, and retry.",
            )
        try:
            generated = validate_script_segments(json.loads(raw), pending_targets)
        except (json.JSONDecodeError, ValueError) as exc:
            message = str(exc) if isinstance(exc, ValueError) else "Gemini returned invalid JSON for the script segments."
            raise HTTPException(status_code=502, detail=message) from exc
        excluded = blocked_names(event)
        for segment in generated:
            segment["text"] = drop_blocked_name_sentences(segment["text"], excluded).strip()
            if not segment["text"]:
                raise HTTPException(
                    status_code=502,
                    detail="Gemini returned a segment that mentioned an absent person. No script changes were saved; retry.",
                )
        try:
            generated = validate_script_segments(generated, pending_targets)
        except ValueError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        generated_by_id = {segment["id"]: segment for segment in generated}
        merged = [
            generated_by_id.get(segment["id"], segment)
            for segment in existing["segments"]
            if segment["id"] in valid_segment_ids
            and segment["id"] not in {target["id"] for target in pending_targets}
        ]
        merged_by_id = {segment["id"]: segment for segment in merged}
        merged_by_id.update(generated_by_id)
        ordered = [
            merged_by_id.pop(target["id"])
            for target in normalized_targets
            if target["id"] in merged_by_id
        ]
        ordered.extend(merged_by_id.values())
        saved = await asyncio.to_thread(service.save_script, request.event_id, ordered)
        return {
            **saved,
            "protected_ids": protected_ids,
            "generated_ids": [segment["id"] for segment in generated],
            "backend": backend,
            "model": model,
        }

    @application.put("/api/anchoring/scripts/{event_id}")
    async def update_anchoring_script(
        event_id: int, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        segments = payload.get("segments")
        if not isinstance(segments, list) or len(segments) > 200:
            raise HTTPException(status_code=422, detail="segments must be a list of at most 200 items.")
        expected = []
        for segment in segments:
            if not isinstance(segment, dict):
                raise HTTPException(status_code=422, detail="Every script segment must be an object.")
            expected.append({
                "id": segment.get("id"),
                "type": segment.get("type"),
                "itemId": segment.get("itemId"),
            })
        try:
            checked = validate_script_segments(segments, expected)
            current_event = await asyncio.to_thread(
                application.state.event_service.get_event, event_id
            )
            item_ids = {item["id"] for item in current_event["items"]}
            for segment in checked:
                if segment["itemId"] is not None and segment["itemId"] not in item_ids:
                    raise ValueError("A script segment refers to an unknown event item.")
                incoming = next(item for item in segments if item["id"] == segment["id"])
                segment["edited"] = bool(incoming.get("edited"))
            return await asyncio.to_thread(
                application.state.event_service.save_script, event_id, checked
            )
        except EventNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.post("/api/anchoring/scripts/{event_id}/undo")
    async def undo_anchoring_script(event_id: int) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                application.state.event_service.undo_script, event_id
            )
        except EventNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.post("/api/live-events/{event_id}/close")
    async def close_anchoring_event(
        event_id: int, request: EventCloseRequest
    ) -> dict[str, Any]:
        service: EventService = application.state.event_service
        if request.language not in {"en", "ml"}:
            raise HTTPException(status_code=422, detail="language must be 'en' or 'ml'.")
        try:
            event = await asyncio.to_thread(service.get_event, event_id)
            current_script = await asyncio.to_thread(service.get_script, event_id)
        except EventNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        closing_segment = next(
            (segment for segment in current_script["segments"] if segment["id"] == "closing"),
            None,
        )
        if closing_segment and closing_segment.get("edited") and not request.replace_edited:
            raise HTTPException(
                status_code=409,
                detail="The closing segment was edited by hand. Confirm before replacing it.",
            )
        notes = [
            {
                "speaker": item["person_name"],
                "item": item["title"],
                "notes": item["notes"],
                "transcript": item["transcript"],
                "summary": item["summary"],
            }
            for item in event["items"]
            if any(str(item.get(field) or "").strip() for field in ("notes", "transcript", "summary"))
        ]
        summary_skipped = not notes
        if summary_skipped:
            text = service.build_closing_template({**event, "language": request.language})
            text += (
                "പ്രസംഗകുറിപ്പുകളോ ട്രാൻസ്ക്രിപ്റ്റുകളോ നൽകിയിട്ടില്ലാത്തതിനാൽ പരിപാടിയുടെ സംഗ്രഹം ഒഴിവാക്കി."
                if request.language == "ml"
                else " The event summary was skipped because no speaker notes or transcripts were provided."
            )
        else:
            target = [{"id": "closing", "type": "closing", "itemId": None}]
            closing_event = {
                "name": event["name"],
                "language": request.language,
                "speaker_notes_and_transcripts": notes,
                "summary_policy": (
                    "Use only the supplied speaker notes and transcripts. Include a concise "
                    "high-level recap and thank-you. Do not infer content from schedule titles."
                ),
            }
            prompt = build_structured_script_prompt(
                closing_event, [], targets=target, existing_segments=[]
            )
            try:
                def generate_closing() -> tuple[str, str]:
                    chunks, decision = application.state.pipeline.router.stream(
                        "anchoring_script",
                        request.language,
                        [{"role": "user", "content": prompt}],
                    )
                    return "".join(chunks).strip(), decision.backend

                raw, backend = await asyncio.to_thread(generate_closing)
                if backend == "template":
                    raise ValueError("Gemini is unavailable or not configured.")
                if raw.startswith("```"):
                    raw = re.sub(r"^```(?:json)?\\s*|\\s*```$", "", raw, flags=re.IGNORECASE)
                generated = validate_script_segments(json.loads(raw), target)
                text = generated[0]["text"]
                text = drop_blocked_name_sentences(text, blocked_names(event)).strip()
                if not text:
                    raise ValueError("Gemini returned an unsafe closing segment.")
            except Exception as exc:
                logger.exception("Closing script generation failed")
                if getattr(exc, "status_code", None) == 429:
                    detail = "Gemini is rate limiting requests. Wait briefly and retry."
                    status_code = 429
                elif any(
                    cue in str(exc).lower()
                    for cue in ("api key", "credential", "not configured", "unavailable")
                ):
                    detail = "Gemini is unavailable (missing key, rate limit, or network issue). Check the server API key, wait briefly, and retry."
                    status_code = 503
                else:
                    detail = "AVA could not generate the closing. The event is still open; retry when ready."
                    status_code = 503
                raise HTTPException(status_code=status_code, detail=detail) from exc
        closing = {
            "id": "closing",
            "type": "closing",
            "itemId": None,
            "text": text,
            "estimatedSeconds": max(1, round(len(text.split()) / 2.2)),
            "edited": False,
            "summarySkipped": summary_skipped,
        }
        segments = [segment for segment in current_script["segments"] if segment["id"] != "closing"]
        segments.append(closing)
        saved = await asyncio.to_thread(service.save_script, event_id, segments)
        return {
            "event": event,
            "closing": closing,
            "script": saved,
            "summary_skipped": summary_skipped,
        }

    @application.post("/api/events/{event_id}/next")
    async def advance_event(event_id: int) -> dict[str, Any]:
        """Advance a persisted event to its next schedule item."""
        try:
            before = await asyncio.to_thread(
                application.state.event_service.get_event, event_id
            )
            updated = await mutate_live_event(
                event_id,
                lambda: application.state.event_service.advance(event_id),
                action="next",
            )
        except EventNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        current = next(
            (item for item in updated["items"] if item["id"] == updated["current_item_id"]),
            None,
        )
        current_order = current["order_index"] if current else -1
        next_item = next(
            (item for item in updated["items"]
             if item["order_index"] > current_order
             and item["status"] in {"pending", "arrived"}),
            None,
        )
        return {
            "event_id": event_id,
            "current_item_index": current_order,
            "advanced": updated["current_item_id"] != before["current_item_id"],
            "current": current,
            "next": next_item,
            "state_version": updated["state_version"],
        }

    async def mutate_live_event(
        event_id: int,
        operation: Any,
        *,
        action: str,
        changed_item_id: int | None = None,
    ) -> dict[str, Any]:
        """Run an event service mutation and publish the committed version."""
        event_service: EventService = application.state.event_service
        try:
            before = await asyncio.to_thread(event_service.get_event, event_id)
            updated = await asyncio.to_thread(operation)
        except EventNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        stopped_current = (
            changed_item_id is not None
            and before["current_item_id"] == changed_item_id
            and any(
                item["id"] == changed_item_id and item["status"] in {"absent", "removed", "skipped"}
                for item in updated["items"]
            )
        )
        if stopped_current:
            application.state.pipeline.stop_speaking(before["session_id"])
            await asyncio.gather(
                application.state.pipeline.robot.stop(),
                return_exceptions=True,
            )
        await broadcast_event(
            updated,
            action="speech_stop" if stopped_current else action,
        )
        return updated

    @application.get("/api/live-events/session/{session_id}")
    async def live_event_for_session(session_id: str) -> dict[str, Any]:
        """Read the current authoritative live event for a conversation session."""
        event = await asyncio.to_thread(
            application.state.event_service.get_for_session, session_id
        )
        if event is None:
            raise HTTPException(status_code=404, detail="No live event is linked to this session.")
        return event

    @application.get("/api/live-events/{event_id}")
    async def read_live_event(event_id: int) -> dict[str, Any]:
        """Read versioned live event state, schedule, audit, and speaker summaries."""
        try:
            return await asyncio.to_thread(
                application.state.event_service.get_event, event_id
            )
        except EventNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @application.patch("/api/live-events/{event_id}")
    async def patch_live_event(
        event_id: int, changes: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        return await mutate_live_event(
            event_id,
            lambda: application.state.event_service.update_event(event_id, changes),
            action="event_updated",
        )

    @application.post("/api/live-events/{event_id}/start")
    async def start_live_event(event_id: int) -> dict[str, Any]:
        return await mutate_live_event(
            event_id, lambda: application.state.event_service.start(event_id), action="started"
        )

    @application.post("/api/live-events/{event_id}/pause")
    async def pause_live_event(event_id: int) -> dict[str, Any]:
        return await mutate_live_event(
            event_id,
            lambda: application.state.event_service.update_event(event_id, {"status": "paused"}),
            action="paused",
        )

    @application.post("/api/live-events/{event_id}/end")
    async def end_live_event(event_id: int) -> dict[str, Any]:
        updated = await mutate_live_event(
            event_id,
            lambda: application.state.event_service.update_event(event_id, {"status": "ended"}),
            action="ended",
        )
        application.state.pipeline.stop_speaking(updated["session_id"])
        stop_results = await asyncio.gather(
            application.state.pipeline.robot.stop(),
            return_exceptions=True,
        )
        if any(isinstance(result, Exception) for result in stop_results):
            logger.warning("Robot could not stop cleanly after event end")
        updated["closing_script"] = EventService.build_closing_template(updated)
        return updated

    @application.post("/api/live-events/{event_id}/next")
    async def next_live_item(event_id: int) -> dict[str, Any]:
        return await mutate_live_event(
            event_id, lambda: application.state.event_service.advance(event_id), action="next"
        )

    @application.post("/api/live-events/{event_id}/previous")
    async def previous_live_item(event_id: int) -> dict[str, Any]:
        return await mutate_live_event(
            event_id, lambda: application.state.event_service.previous(event_id), action="previous"
        )

    @application.post("/api/live-events/{event_id}/jump/{item_id}")
    async def jump_live_item(event_id: int, item_id: int) -> dict[str, Any]:
        return await mutate_live_event(
            event_id,
            lambda: application.state.event_service.advance(event_id, item_id=item_id),
            action="jump",
        )

    @application.post("/api/live-events/{event_id}/call-now")
    async def call_live_item(
        event_id: int, payload: dict[str, Any] = Body(default_factory=dict)
    ) -> dict[str, Any]:
        """Select the next or requested live item, generate an intro, and speak it."""
        event_service: EventService = application.state.event_service
        try:
            before = await asyncio.to_thread(event_service.get_event, event_id)
        except EventNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        item_id = payload.get("item_id")
        if item_id is not None and (
            isinstance(item_id, bool) or not isinstance(item_id, int)
        ):
            raise HTTPException(status_code=422, detail="item_id must be an integer.")
        include_summary = bool(payload.get("include_summary", True))
        previous_item = next(
            (item for item in before["items"] if item["id"] == before["current_item_id"]),
            None,
        )
        updated = await mutate_live_event(
            event_id,
            lambda: event_service.advance(event_id, item_id=item_id),
            action="call_now" if item_id is not None else "next",
        )
        current_item = next(
            (item for item in updated["items"] if item["id"] == updated["current_item_id"]),
            None,
        )
        if (
            item_id is None
            and before["current_item_id"] is not None
            and updated["current_item_id"] == before["current_item_id"]
        ):
            raise HTTPException(status_code=409, detail="There is no next schedule item.")
        if current_item is None:
            raise HTTPException(
                status_code=409, detail="There is no eligible schedule item to announce."
            )
        if previous_item and previous_item["id"] == current_item["id"]:
            previous_item = None
        if previous_item and previous_item["status"] in {"absent", "removed", "skipped"}:
            previous_item = None
        if previous_item and contains_blocked_name(
            str(previous_item.get("person_name") or ""),
            blocked_names(updated),
        ):
            previous_item = None

        fallback = event_service.build_call_now_template(
            updated,
            previous_item,
            current_item,
            include_summary=include_summary,
        )
        eligible_schedule = [
            {
                "title": item["title"],
                "type": item["type"],
                "person_name": item["person_name"],
                "designation": item["designation"],
                "status": item["status"],
            }
            for item in updated["items"]
            if item["status"] in {"pending", "arrived", "on_stage", "done"}
        ]
        response_language = (
            "natural spoken Malayalam in Malayalam script"
            if updated["language"] == "ml"
            else "natural spoken English"
        )
        previous_prompt_item = (
            {
                "type": previous_item["type"],
                "person_name": previous_item["person_name"],
                "summary": previous_item["summary"] if include_summary else "",
            }
            if previous_item
            else None
        )
        prompt = (
            "Write one concise spoken event transition. Thank the previous speaker by name "
            "when the previous item is a speech. Use only the supplied live schedule and "
            "summary; do not invent names, topics, or facts. Introduce the current item and "
            "its person/designation if present. Never mention anyone absent, removed, or skipped. "
            f"Write in {response_language}.\n"
            f"Event: {updated['name']}\n"
            f"Previous speaker and summary: {previous_prompt_item or 'None'}\n"
            f"Current item: {current_item}\n"
            f"Eligible live schedule: {eligible_schedule}"
        )

        def generate_announcement() -> tuple[str, str, str]:
            chunks, decision = application.state.pipeline.router.stream(
                "anchoring_script",
                updated["language"],
                [{"role": "user", "content": prompt}],
            )
            return "".join(chunks).strip(), decision.backend, decision.model

        try:
            generated, backend, model = await asyncio.to_thread(generate_announcement)
            forbidden = [
                item["person_name"]
                for item in updated["items"]
                if item["status"] in {"absent", "removed", "skipped"} and item["person_name"]
            ]
            generated = drop_blocked_name_sentences(generated, forbidden)
            previous_speaker = (
                str(previous_item.get("person_name") or "").strip().casefold()
                if previous_item and previous_item.get("type") == "speech"
                else ""
            )
            thanks_marker = "നന്ദി" if updated["language"] == "ml" else "thank you"
            thanks_missing = bool(
                previous_item
                and previous_item.get("type") == "speech"
                and (
                    thanks_marker not in generated.casefold()
                    or (
                        previous_speaker
                        and previous_speaker not in generated.casefold()
                    )
                )
            )
            if (
                backend == "template"
                or not generated
                or contains_blocked_name(generated, forbidden)
                or thanks_missing
            ):
                message = fallback
                used_fallback = True
            else:
                message = generated
                used_fallback = False
        except Exception:
            logger.exception(
                "Live event announcement generation failed; using schedule template"
            )
            message = fallback
            backend = "template"
            model = "template"
            used_fallback = True

        preferences = await asyncio.to_thread(
            application.state.pipeline.repository.get_preferences,
            updated["session_id"],
        )
        audio_url: str | None = None
        notice: str | None = None
        try:
            audio = await application.state.pipeline.synthesizer.synthesize(
                message,
                updated["language"],
                voice=preferences.get("voice") or None,
                cache_version=updated["state_version"],
            )
            audio_url = f"/api/audio/{audio.name}"
            notice = application.state.pipeline.synthesizer.get_status(audio)
        except Exception:
            logger.exception("Announcement TTS failed; browser speech fallback will be used")
            notice = "Audio synthesis failed; using browser speech if available."
        robot_results = await asyncio.gather(
            application.state.pipeline.robot.speak(message),
            application.state.pipeline.robot.gesture("announcement"),
            return_exceptions=True,
        )
        if any(isinstance(result, Exception) for result in robot_results):
            logger.warning("Robot could not perform the call-now announcement")
        return {
            "event": updated,
            "text": message,
            "audio_url": audio_url,
            "notice": notice,
            "backend": backend,
            "model": model,
            "used_fallback": used_fallback,
        }

    @application.get("/api/live-events/{event_id}/replan")
    async def replan_live_event(event_id: int) -> dict[str, str]:
        try:
            transition = await asyncio.to_thread(
                application.state.event_service.replan_upcoming, event_id
            )
            return {"transition": transition}
        except EventNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @application.post("/api/live-events/{event_id}/undo")
    async def undo_live_change(event_id: int) -> dict[str, Any]:
        return await mutate_live_event(
            event_id, lambda: application.state.event_service.undo(event_id), action="undo"
        )

    @application.post("/api/event/items")
    async def add_live_item(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        event_id = int(payload.pop("event_id", 0))
        position = payload.pop("position", None)
        return await mutate_live_event(
            event_id,
            lambda: application.state.event_service.add_item(
                event_id, payload, position=position
            ),
            action="item_added",
        )

    @application.patch("/api/event/items/{item_id}")
    async def patch_live_item(
        item_id: int, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        event_id = int(payload.pop("event_id", 0))
        operator = str(payload.pop("operator", "operator"))
        return await mutate_live_event(
            event_id,
            lambda: application.state.event_service.update_item(
                event_id, item_id, payload, operator
            ),
            action="item_updated",
            changed_item_id=item_id,
        )

    @application.delete("/api/event/items/{item_id}")
    async def delete_live_item(item_id: int, event_id: int) -> dict[str, Any]:
        return await mutate_live_event(
            event_id,
            lambda: application.state.event_service.delete_item(event_id, item_id),
            action="item_removed",
            changed_item_id=item_id,
        )

    @application.post("/api/live-events/{event_id}/items/{item_id}/skip")
    async def skip_live_item(
        event_id: int, item_id: int, payload: dict[str, Any] = Body(default_factory=dict)
    ) -> dict[str, Any]:
        move_to_end = payload.get("move_to_end", False)
        if not isinstance(move_to_end, bool):
            raise HTTPException(status_code=422, detail="move_to_end must be a boolean.")
        return await mutate_live_event(
            event_id,
            lambda: application.state.event_service.skip_item(
                event_id, item_id, move_to_end=move_to_end
            ),
            action="item_skipped",
            changed_item_id=item_id,
        )

    @application.post("/api/live-events/{event_id}/items/{item_id}/restore")
    async def restore_live_item(
        event_id: int, item_id: int, payload: dict[str, Any] = Body(default_factory=dict)
    ) -> dict[str, Any]:
        position = payload.get("position")
        if position is not None and (
            isinstance(position, bool) or not isinstance(position, int)
        ):
            raise HTTPException(status_code=422, detail="position must be an integer.")
        return await mutate_live_event(
            event_id,
            lambda: application.state.event_service.restore_item(
                event_id, item_id, position=position
            ),
            action="item_restored",
            changed_item_id=item_id,
        )

    @application.post("/api/event/reorder")
    async def reorder_live_items(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        event_id = int(payload.get("event_id", 0))
        item_ids = payload.get("item_ids")
        if not isinstance(item_ids, list) or any(not isinstance(item_id, int) for item_id in item_ids):
            raise HTTPException(status_code=422, detail="item_ids must be a list of item IDs.")
        return await mutate_live_event(
            event_id,
            lambda: application.state.event_service.reorder(event_id, item_ids),
            action="schedule_reordered",
        )

    @application.post("/api/event/next")
    async def advance_live_event(payload: dict[str, int] = Body(...)) -> dict[str, Any]:
        event_id = int(payload.get("event_id", 0))
        return await mutate_live_event(
            event_id, lambda: application.state.event_service.advance(event_id), action="next"
        )

    @application.post("/api/live-events/{event_id}/stop-speaking")
    async def stop_speaking(event_id: int) -> dict[str, Any]:
        """Immediately cancel browser audio and request that the robot stop."""
        try:
            event = await asyncio.to_thread(application.state.event_service.get_event, event_id)
        except EventNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        await asyncio.gather(
            asyncio.to_thread(
                application.state.pipeline.stop_speaking, event["session_id"]
            ),
            application.state.pipeline.robot.stop(),
            return_exceptions=True,
        )
        for queue in tuple(application.state.event_subscribers.get(event["session_id"], set())):
            try:
                await queue.put(
                    {"type": "speech_stop", "state_version": event["state_version"]}
                )
            except RuntimeError:
                logger.warning("Could not broadcast emergency stop", extra={"event_id": event_id})
        return {"status": "stopped"}

    @application.post("/api/live-events/{event_id}/items/{item_id}/transcript")
    async def append_speaker_transcript(
        event_id: int, item_id: int, payload: dict[str, str] = Body(...)
    ) -> dict[str, Any]:
        """Append operator notes or a transcript segment to a speech item."""
        text = payload.get("text", "")
        if not text.strip():
            raise HTTPException(status_code=422, detail="Transcript text cannot be empty.")
        updated = await mutate_live_event(
            event_id,
            lambda: application.state.event_service.append_transcript(event_id, item_id, text),
            action="transcript_appended",
        )
        return updated

    @application.put("/api/live-events/{event_id}/items/{item_id}/transcript")
    async def edit_speaker_transcript(
        event_id: int, item_id: int, payload: dict[str, str] = Body(...)
    ) -> dict[str, Any]:
        updated = await mutate_live_event(
            event_id,
            lambda: application.state.event_service.set_transcript(
                event_id, item_id, payload.get("text", "")
            ),
            action="transcript_edited",
        )
        return updated

    @application.post("/api/live-events/{event_id}/items/{item_id}/capture")
    async def capture_speaker_segment(
        event_id: int,
        item_id: int,
        audio: UploadFile = File(...),
        language: str = Form("auto"),
    ) -> dict[str, Any]:
        """Transcribe one browser-recorded segment in a worker thread."""
        if language not in {"en", "ml", "auto"}:
            raise HTTPException(status_code=422, detail="Language must be en, ml, or auto.")
        contents = await audio.read(25 * 1024 * 1024 + 1)
        if not contents or len(contents) > 25 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Audio segment is empty or exceeds 25 MB.")
        suffix = Path(audio.filename or "segment.webm").suffix
        if suffix.lower() not in {".webm", ".wav", ".mp3", ".mp4", ".ogg", ".m4a"}:
            suffix = ".webm"
        descriptor, temporary_name = tempfile.mkstemp(suffix=suffix)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(contents)
            result = await asyncio.to_thread(
                SpeakerCapture(
                    application.state.recognizer, application.state.event_service
                ).transcribe_and_store,
                event_id, item_id, temporary_name, language,
            )
            await broadcast_event(result["event"], action="transcript_appended")
            return {"text": result["text"], "language": result["language"]}
        except EventNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Speaker capture transcription failed")
            raise HTTPException(
                status_code=503,
                detail="Could not transcribe this segment. Paste notes or the transcript instead.",
            ) from exc
        finally:
            Path(temporary_name).unlink(missing_ok=True)

    @application.post("/api/live-events/{event_id}/items/{item_id}/finish")
    async def finish_speaker(
        event_id: int, item_id: int, payload: dict[str, Any] = Body(default_factory=dict)
    ) -> dict[str, Any]:
        """Finish capture, optionally append operator notes, and generate a summary."""
        try:
            event = await asyncio.to_thread(application.state.event_service.get_event, event_id)
            item = next(item for item in event["items"] if item["id"] == item_id)
        except (EventNotFoundError, StopIteration) as exc:
            raise HTTPException(status_code=404, detail="Speech item not found.") from exc
        notes = str(payload.get("notes", "")).strip()
        if item["type"] != "speech":
            raise HTTPException(status_code=422, detail="Only speech items can be summarized.")
        language = str(payload.get("language", event["language"]))
        if language not in {"en", "ml"}:
            raise HTTPException(status_code=422, detail="Summary language must be en or ml.")
        transcript = f"{item['transcript']} {notes}".strip()
        summarizer = SpeakerSummarizer(application.state.pipeline.router)
        summary = await asyncio.to_thread(
            summarizer.summarize,
            transcript,
            language=language,
            speaker_name=item["person_name"],
        )
        updated = await asyncio.to_thread(
            application.state.event_service.save_summary, event_id, item_id, summary
        )
        if item["status"] == "on_stage":
            updated = await asyncio.to_thread(
                application.state.event_service.update_item,
                event_id, item_id, {"status": "done"},
            )
        await broadcast_event(updated, action="speaker_finished")
        return {"summary": summary, "event": updated}

    @application.post("/api/live-events/{event_id}/items/{item_id}/summary")
    async def regenerate_speaker_summary(
        event_id: int, item_id: int, payload: dict[str, str] = Body(default_factory=dict)
    ) -> dict[str, Any]:
        try:
            event = await asyncio.to_thread(application.state.event_service.get_event, event_id)
            item = next(item for item in event["items"] if item["id"] == item_id)
        except (EventNotFoundError, StopIteration) as exc:
            raise HTTPException(status_code=404, detail="Speech item not found.") from exc
        if item["type"] != "speech":
            raise HTTPException(status_code=422, detail="Only speech items can be summarized.")
        language = payload.get("language", event["language"])
        if language not in {"en", "ml"}:
            raise HTTPException(status_code=422, detail="Summary language must be en or ml.")
        summarizer = SpeakerSummarizer(application.state.pipeline.router)
        summary = await asyncio.to_thread(
            summarizer.summarize,
            item["transcript"],
            language=language,
            speaker_name=item["person_name"],
        )
        updated = await mutate_live_event(
            event_id,
            lambda: application.state.event_service.save_summary(event_id, item_id, summary),
            action="summary_regenerated",
        )
        return {"summary": summary, "event": updated}

    @application.post("/api/live-events/{event_id}/items/{item_id}/summary/speak")
    async def speak_speaker_summary(event_id: int, item_id: int) -> dict[str, str]:
        """Synthesize a reviewed summary using the configured speech provider."""
        try:
            event = await asyncio.to_thread(application.state.event_service.get_event, event_id)
        except EventNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        item = next((entry for entry in event["items"] if entry["id"] == item_id), None)
        if item is None or not item["summary"]:
            raise HTTPException(status_code=404, detail="No speaker summary is available.")
        language = event["language"]
        preferences = await asyncio.to_thread(
            application.state.pipeline.repository.get_preferences,
            event["session_id"],
        )
        audio = await application.state.pipeline.synthesizer.synthesize(
            item["summary"],
            language,
            voice=preferences.get("voice") or None,
            cache_version=event["state_version"],
        )
        return {"url": f"/api/audio/{audio.name}"}

    @application.get("/api/event/settings/{session_id}")
    async def read_event_settings(session_id: str) -> dict[str, bool]:
        enabled = await asyncio.to_thread(
            application.state.event_service.get_auto_speak, session_id
        )
        return {"auto_speak_summary": enabled}

    @application.put("/api/event/settings/{session_id}")
    async def update_event_settings(
        session_id: str, payload: dict[str, bool] = Body(...)
    ) -> dict[str, bool]:
        enabled = bool(payload.get("auto_speak_summary", False))
        await asyncio.to_thread(
            application.state.event_service.set_auto_speak, session_id, enabled
        )
        return {"auto_speak_summary": enabled}

    @application.post("/api/live-events/{event_id}/parse-command")
    async def parse_event_command(
        event_id: int, payload: dict[str, str] = Body(...)
    ) -> dict[str, Any]:
        """Return a structured candidate edit for explicit operator confirmation."""
        message = payload.get("message", "").strip()
        if not message:
            raise HTTPException(status_code=422, detail="Message cannot be empty.")
        try:
            event = await asyncio.to_thread(application.state.event_service.get_event, event_id)
        except EventNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        intent = await asyncio.to_thread(
            application.state.pipeline.classifier.classify, message,
        )
        command = (
            parse_operator_command(message, event)
            if intent.intent == "event_command"
            else None
        )
        return {"proposal": command}

    @application.post("/api/live-events/{event_id}/announcement")
    async def operator_announcement(
        event_id: int, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        """Speak literal operator text or polish it against the current schedule."""
        try:
            event = await asyncio.to_thread(application.state.event_service.get_event, event_id)
        except EventNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        message = str(payload.get("text", "")).strip()
        if not message or len(message) > 2000:
            raise HTTPException(status_code=422, detail="Announcement must contain 1–2000 characters.")
        if payload.get("polish"):
            prompt = (
                "Polish this operator announcement for natural spoken delivery. Keep only the "
                "facts supplied by the operator and current event. Do not mention absent, "
                "removed, or skipped people.\n"
                f"Event: {event['name']}\nSchedule: {event['items']}\n"
                f"Operator text: {message}"
            )
            def polish_text() -> str:
                chunks, _ = application.state.pipeline.router.stream(
                    "anchoring_script",
                    event["language"],
                    [{"role": "user", "content": prompt}],
                )
                return "".join(chunks).strip()

            message = await asyncio.to_thread(polish_text) or str(payload["text"]).strip()
        forbidden = [
            item["person_name"] for item in event["items"]
            if item["status"] in {"absent", "removed", "skipped"} and item["person_name"]
        ]
        message = drop_blocked_name_sentences(message, forbidden)
        if not message:
            raise HTTPException(
                status_code=422,
                detail="Announcement contains no speakable sentences after excluding absent or removed people.",
            )
        preferences = await asyncio.to_thread(
            application.state.pipeline.repository.get_preferences,
            event["session_id"],
        )
        audio = await application.state.pipeline.synthesizer.synthesize(
            message,
            event["language"],
            voice=preferences.get("voice") or None,
            cache_version=event["state_version"],
        )
        results = await asyncio.gather(
            application.state.pipeline.robot.speak(message),
            application.state.pipeline.robot.gesture("point"),
            return_exceptions=True,
        )
        if any(isinstance(result, Exception) for result in results):
            logger.warning("Robot could not perform the operator announcement action")
        return {
            "text": message,
            "url": f"/api/audio/{audio.name}",
            "notice": application.state.pipeline.synthesizer.get_status(audio),
        }

    @application.get("/api/live-events/{event_id}/report")
    async def export_event_report(event_id: int, format: str = "md") -> Response:
        """Export a full event report as Markdown, PDF, or DOCX."""
        try:
            report = await asyncio.to_thread(
                application.state.event_service.export_markdown, event_id
            )
        except EventNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if format == "md":
            return Response(
                report,
                media_type="text/markdown; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="ava-event-{event_id}.md"'},
            )
        from io import BytesIO

        buffer = BytesIO()
        if format == "docx":
            try:
                from docx import Document
            except ImportError as exc:
                raise HTTPException(status_code=501, detail="DOCX export requires python-docx.") from exc
            document = Document()
            for line in report.splitlines():
                document.add_paragraph(line)
            document.save(buffer)
            media_type, suffix = "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"
        elif format == "pdf":
            try:
                from reportlab.lib.pagesizes import letter
                from reportlab.pdfbase import pdfmetrics
                from reportlab.pdfbase.ttfonts import TTFont
                from reportlab.pdfgen import canvas
            except ImportError as exc:
                raise HTTPException(status_code=501, detail="PDF export requires reportlab.") from exc
            font_candidates = (
                Path(r"C:\Windows\Fonts\Nirmala.ttc"),
                Path("/usr/share/fonts/truetype/noto/NotoSansMalayalam-Regular.ttf"),
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                Path("/Library/Fonts/NotoSansMalayalam-Regular.ttf"),
            )
            font_path = next((path for path in font_candidates if path.is_file()), None)
            font_name = "Helvetica"
            if font_path:
                font_name = "AVAUnicode"
                pdfmetrics.registerFont(
                    TTFont(
                        font_name,
                        str(font_path),
                        subfontIndex=0,
                    )
                )
            pdf = canvas.Canvas(buffer, pagesize=letter)
            _, height = letter
            y = height - 40
            pdf.setFont(font_name, 9)
            for line in report.splitlines():
                if y < 40:
                    pdf.showPage()
                    pdf.setFont(font_name, 9)
                    y = height - 40
                pdf.drawString(40, y, line[:110])
                y -= 15
            pdf.save()
            media_type, suffix = "application/pdf", "pdf"
        else:
            raise HTTPException(status_code=422, detail="format must be md, pdf, or docx.")
        return Response(
            buffer.getvalue(),
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="ava-event-{event_id}.{suffix}"'},
        )

    @application.get("/api/audio/{filename}")
    async def cached_audio(filename: str) -> FileResponse:
        """Serve only cache files named by a SHA-256 content key."""
        if not re.fullmatch(r"[a-f0-9]{64}\.mp3", filename):
            raise HTTPException(status_code=400, detail="Invalid audio cache key.")
        speech = application.state.settings.speech
        cache = Path(str(speech.get("cache_dir", "audio/cache")))
        if not cache.is_absolute():
            cache = PROJECT_ROOT / cache
        file_path = cache / filename
        if not file_path.is_file():
            raise HTTPException(status_code=404, detail="Audio is not cached.")
        return FileResponse(file_path, media_type="audio/mpeg")

    @application.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        """Return editable, non-secret UI settings."""
        config = application.state.settings
        return {
            "model": config.llm.get("default_model", "gemini-3.5-flash"),
            "voices": config.speech.get("voices", {}),
            "language": config.speech.get("language", "en"),
            "speaker_silence_suggestion_seconds": int(
                config.speech.get("speaker_silence_suggestion_seconds", 8)
            ),
            "robot_mode": (
                "serial"
                if isinstance(application.state.pipeline.robot, SerialRobot)
                else "simulated"
            ),
            "robot_port": config.robot.get("serial_port", ""),
        }

    return application


app = create_app()
