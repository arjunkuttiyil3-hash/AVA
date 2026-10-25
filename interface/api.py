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
from modules.robot import SerialRobot, SimulatedRobot
from modules.health import collect_health, log_health
from nlu.intent_classifier import IntentClassifier
from orchestrator.pipeline import ConversationPipeline
from orchestrator.router import ModelRouter
from services.event_service import (
    EventNotFoundError,
    EventService,
    contains_blocked_name,
    parse_operator_command,
)
from modules.speaker_capture import SpeakerCapture
from modules.summarizer import SpeakerSummarizer

PROJECT_ROOT = Path(__file__).resolve().parent.parent
logger = logging.getLogger("ava")


class SessionResponse(BaseModel):
    """Session creation response."""

    session_id: str


class EventCreate(BaseModel):
    """Event data submitted from the anchoring setup form."""

    session_id: str
    name: str = Field(min_length=1, max_length=200)
    venue: str = Field(default="", max_length=200)
    event_date: str = Field(default="", max_length=30)
    chief_guest: str = Field(default="", max_length=200)
    speakers: list[str] = Field(default_factory=list, max_length=40)
    tone: str = Field(default="Professional", max_length=40)
    schedule: list[dict[str, str]] = Field(default_factory=list, max_length=100)


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
    application.state.health_checked_at = time.monotonic()
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
        """Report API liveness plus optional model, audio, and database status."""
        is_stale = time.monotonic() - application.state.health_checked_at > 30
        if refresh or is_stale:
            checks = await asyncio.to_thread(
                collect_health, application.state.settings
            )
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
        schedule = sorted(event.schedule, key=lambda item: item.get("time", ""))
        for item in schedule:
            if not item.get("title", "").strip():
                raise HTTPException(
                    status_code=422, detail="Every schedule item needs a title."
                )
            try:
                if item.get("time"):
                    datetime_time.fromisoformat(item["time"])
            except ValueError as exc:
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid schedule time: {item['time']}",
                ) from exc

        def persist_event() -> int:
            with connection_scope(pipeline.repository.database_path) as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO events(
                        name, venue, event_date, chief_guest, speakers_json,
                        schedule_json, tone, session_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (
                        event.name,
                        event.venue,
                        event.event_date,
                        event.chief_guest,
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
                            event_id, event_time, title, event_type, participant
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            event_id,
                            item.get("time", ""),
                            item.get("title", ""),
                            item.get("type", "other"),
                            item.get("participant", ""),
                        ),
                    )
                return event_id

        event_id = await asyncio.to_thread(persist_event)
        live_event = await asyncio.to_thread(
            application.state.event_service.create_event,
            event.session_id,
            name=event.name,
            event_type="event",
            date=event.event_date,
            venue=event.venue,
            tone=event.tone,
            language="en",
            chief_guest=event.chief_guest,
            schedule=[
                {
                    "title": item["title"],
                    "type": item.get("type", "other"),
                    "person_name": item.get("person_name", item.get("participant", "")),
                    "designation": item.get("designation", ""),
                    "planned_duration": int(item.get("planned_duration", 0)),
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
        return await mutate_live_event(
            event_id,
            lambda: application.state.event_service.update_event(event_id, {"status": "ended"}),
            action="ended",
        )

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
        if contains_blocked_name(message, forbidden):
            raise HTTPException(status_code=422, detail="Announcement includes a person marked absent or removed.")
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
            "model": config.llm.get("default_model", "gemini-2.5-flash"),
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
