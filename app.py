"""Single-file AVA chat and live event-control application.

Run with ``python app.py``. The app reads the existing ``config/ava.yaml`` for
the Ollama endpoint/model and stores its isolated SQLite data in
``database/ava-single-file.db`` (or ``AVA_DB_PATH`` when set).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import sqlite3
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import edge_tts
import uvicorn
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from ollama import AsyncClient
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
CONFIG_PATH = Path(os.getenv("AVA_CONFIG_PATH", ROOT / "config" / "ava.yaml"))
if not CONFIG_PATH.is_absolute():
    CONFIG_PATH = ROOT / CONFIG_PATH
try:
    _CONFIG: dict[str, Any] = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
except FileNotFoundError:
    _CONFIG = {}
_LLM = _CONFIG.get("llm", {})
_SPEECH = _CONFIG.get("speech", {})
_APP = _CONFIG.get("app", {})
MODEL = os.getenv("AVA_MODEL", str(_LLM.get("default_model", "gemma3:4b")))
OLLAMA_URL = os.getenv("OLLAMA_HOST", str(_LLM.get("base_url", "http://localhost:11434")))
HOST = os.getenv("AVA_HOST", str(_APP.get("host", "127.0.0.1")))
PORT = int(os.getenv("AVA_PORT", str(_APP.get("port", 8000))))
DATABASE = Path(os.getenv("AVA_DB_PATH", ROOT / "database" / "ava-single-file.db"))
if not DATABASE.is_absolute():
    DATABASE = ROOT / DATABASE
VOICES = _SPEECH.get(
    "voices",
    {"en": "en-IN-NeerjaNeural", "ml": "ml-IN-SobhanaNeural"},
)
logger = logging.getLogger("ava.single_file")

ollama = AsyncClient(host=OLLAMA_URL, timeout=float(_LLM.get("timeout_seconds", 90)))
SOCKETS: dict[str, set[WebSocket]] = {}
ACTIVE_GENERATIONS: dict[str, asyncio.Event] = {}


class ClosingConnection(sqlite3.Connection):
    """Close SQLite handles after their transaction context exits."""

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool | None:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect_db() -> sqlite3.Connection:
    """Open a short-lived SQLite connection with dictionary-like rows."""
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE, timeout=15, factory=ClosingConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_db() -> None:
    """Create only this standalone app's namespaced tables."""
    with connect_db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS single_messages(
                id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS single_messages_recent
                ON single_messages(session_id, id DESC);
            CREATE TABLE IF NOT EXISTS single_events(
                id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,
                name TEXT NOT NULL, language TEXT NOT NULL DEFAULT 'en',
                status TEXT NOT NULL DEFAULT 'not_started',
                current_item_id INTEGER, state_version INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS single_schedule(
                id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES single_events(id),
                order_index INTEGER NOT NULL, title TEXT NOT NULL,
                person_name TEXT NOT NULL DEFAULT '', designation TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending', transcript TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS single_undo(
                id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL,
                snapshot TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )


class ChatMessage(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    language: str = Field(default="en", pattern="^(en|ml)$")


class EventCreate(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    language: str = Field(default="en", pattern="^(en|ml)$")
    items: list[dict[str, str]] = Field(default_factory=list, max_length=100)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Initialize the isolated persistence tables at server startup."""
    await asyncio.to_thread(initialize_db)
    yield


app = FastAPI(
    title="AVA — AI Virtual Anchoring Assistant",
    version="1.0.0",
    lifespan=lifespan,
)


def read_event(event_id: int) -> dict[str, Any]:
    """Load an event and schedule from the standalone database."""
    with connect_db() as connection:
        event_row = connection.execute(
            "SELECT * FROM single_events WHERE id = ?", (event_id,)
        ).fetchone()
        if event_row is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        items = connection.execute(
            "SELECT * FROM single_schedule WHERE event_id = ? ORDER BY order_index, id",
            (event_id,),
        ).fetchall()
    event = dict(event_row)
    event["items"] = [dict(item) for item in items]
    return event


def event_for_session(session_id: str) -> dict[str, Any] | None:
    """Return the newest live event attached to a chat session."""
    with connect_db() as connection:
        row = connection.execute(
            "SELECT id FROM single_events WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    return read_event(int(row["id"])) if row else None


def absent_names(event: dict[str, Any] | None) -> list[str]:
    """List names that must be excluded from generated speech."""
    if not event:
        return []
    return sorted(
        {
            str(item["person_name"]).strip()
            for item in event["items"]
            if item["status"] in {"absent", "removed", "skipped"}
            and str(item["person_name"]).strip()
        },
        key=len,
        reverse=True,
    )


def safe_sentences(text: str, blocked_names: list[str]) -> list[str]:
    """Remove each sentence that includes an absent or removed speaker."""
    sentences, _ = split_sentences(text, flush=True)
    accepted: list[str] = []
    for sentence in sentences:
        if sentence.strip() and not any(
            re.search(rf"(?<!\w){re.escape(name)}(?!\w)", sentence, re.IGNORECASE)
            for name in blocked_names
        ):
            accepted.append(sentence.strip())
    return accepted


def split_sentences(text: str, *, flush: bool = False) -> tuple[list[str], str]:
    """Split completed sentence boundaries without treating titles as endings."""
    titles = {"dr", "mr", "mrs", "ms", "prof", "rev", "st", "sr", "jr"}
    sentences: list[str] = []
    start = 0
    for index, character in enumerate(text):
        if character not in ".!?。！？":
            continue
        if character == ".":
            prefix = text[start : index + 1]
            title_match = re.search(r"([A-Za-z]+)\.$", prefix)
            if title_match and title_match.group(1).casefold() in titles:
                continue
            if (
                index > 0
                and index + 1 < len(text)
                and text[index - 1].isdigit()
                and text[index + 1].isdigit()
            ):
                continue
        boundary = index + 1 == len(text) and flush
        if index + 1 < len(text) and (text[index + 1].isspace() or text[index + 1] in "\n\r"):
            boundary = True
        if boundary:
            sentence = text[start : index + 1].strip()
            if sentence:
                sentences.append(sentence)
            start = index + 1
            while start < len(text) and text[start].isspace():
                start += 1
    remainder = text[start:]
    if flush and remainder.strip():
        sentences.append(remainder.strip())
        remainder = ""
    return sentences, remainder


async def make_audio(text: str, language: str) -> str | None:
    """Return one sentence as base64 MP3; browser speech synthesis is the fallback."""
    voice = str(VOICES.get(language, VOICES.get("en", "en-IN-NeerjaNeural")))
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as temporary:
            temporary_path = Path(temporary.name)
        await edge_tts.Communicate(text, voice).save(str(temporary_path))
        return base64.b64encode(temporary_path.read_bytes()).decode("ascii")
    except Exception:
        logger.warning("Edge TTS unavailable; browser speech synthesis will be used", exc_info=True)
        return None
    finally:
        if temporary_path:
            temporary_path.unlink(missing_ok=True)


async def broadcast(session_id: str, payload: dict[str, Any]) -> None:
    """Send an event update to all UI sockets for a session."""
    dead: list[WebSocket] = []
    for websocket in tuple(SOCKETS.get(session_id, set())):
        try:
            await websocket.send_json(payload)
        except Exception:
            dead.append(websocket)
    for websocket in dead:
        SOCKETS.get(session_id, set()).discard(websocket)


async def save_message(session_id: str, role: str, text: str) -> None:
    """Persist one turn without holding SQLite on the event loop."""
    await asyncio.to_thread(_save_message, session_id, role, text)


def _save_message(session_id: str, role: str, text: str) -> None:
    with connect_db() as connection:
        connection.execute(
            "INSERT INTO single_messages(session_id, role, content) VALUES (?, ?, ?)",
            (session_id, role, text),
        )


def _recent_messages(session_id: str) -> list[dict[str, str]]:
    with connect_db() as connection:
        rows = connection.execute(
            """SELECT role, content FROM single_messages
               WHERE session_id = ? ORDER BY id DESC LIMIT 11""",
            (session_id,),
        ).fetchall()
    return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]


async def generate_reply(
    session_id: str, message: str, language: str, websocket: WebSocket
) -> None:
    """Stream Ollama output sentence-by-sentence, rejecting absent-name sentences."""
    stop_event = asyncio.Event()
    ACTIVE_GENERATIONS[session_id] = stop_event
    event = await asyncio.to_thread(event_for_session, session_id)
    names = absent_names(event)
    language_instruction = (
        "Write natural spoken Malayalam in Malayalam script."
        if language == "ml"
        else "Write natural spoken English."
    )
    schedule = []
    if event:
        schedule = [
            {
                "title": item["title"],
                "person_name": item["person_name"],
                "status": item["status"],
            }
            for item in event["items"]
            if item["status"] not in {"absent", "removed", "skipped"}
        ]
    system = (
        "You are AVA, a friendly AI Virtual Anchoring Assistant. "
        f"{language_instruction} Never mention a person marked absent, removed, or skipped. "
        f"Use only eligible live schedule entries when discussing the event: "
        f"{json.dumps(schedule, ensure_ascii=False)}"
    )
    history = await asyncio.to_thread(_recent_messages, session_id)
    messages = [{"role": "system", "content": system}, *history, {"role": "user", "content": message}]
    await save_message(session_id, "user", message)
    response_text = ""
    pending = ""
    try:
        await websocket.send_json({"type": "status", "message": "thinking"})
        stream = await ollama.chat(model=MODEL, messages=messages, stream=True)
        async for chunk in stream:
            if stop_event.is_set():
                break
            token = chunk.message.content or ""
            pending += token
            sentences, pending = split_sentences(pending)
            for sentence in sentences:
                if not safe_sentences(sentence, names):
                    logger.info("Omitted a generated sentence mentioning an absent person")
                    continue
                response_text += sentence + " "
                audio = await make_audio(sentence, language)
                await websocket.send_json(
                    {"type": "sentence", "text": sentence, "audio": audio, "language": language}
                )
        final_sentences, _ = split_sentences(pending, flush=True)
        for final in final_sentences:
            if stop_event.is_set() or not safe_sentences(final, names):
                continue
            response_text += final + " "
            audio = await make_audio(final, language)
            await websocket.send_json(
                {"type": "sentence", "text": final, "audio": audio, "language": language}
            )
        if response_text.strip():
            await save_message(session_id, "assistant", response_text.strip())
        await websocket.send_json({"type": "complete", "stopped": stop_event.is_set()})
    except Exception as exc:
        logger.exception("Chat generation failed")
        await websocket.send_json(
            {
                "type": "error",
                "message": (
                    f"Could not connect to Ollama/model {MODEL}. Start Ollama and pull "
                    f"the configured model. ({type(exc).__name__})"
                ),
            }
        )
    finally:
        if ACTIVE_GENERATIONS.get(session_id) is stop_event:
            ACTIVE_GENERATIONS.pop(session_id, None)


@app.get("/", response_class=HTMLResponse)
async def home() -> str:
    """Serve the embedded chat and microphone UI."""
    return CHAT_PAGE


@app.get("/control", response_class=HTMLResponse)
async def control_page() -> str:
    """Serve the embedded operator console."""
    return CONTROL_PAGE


@app.get("/api/health")
async def health() -> dict[str, Any]:
    """Report server/database state and Ollama/model availability."""
    try:
        tags = await ollama.list()
        listed_models = tags.get("models", []) if isinstance(tags, dict) else getattr(tags, "models", [])
        models = {
            str(
                model.get("name", model.get("model", ""))
                if isinstance(model, dict)
                else getattr(model, "name", getattr(model, "model", ""))
            )
            for model in listed_models
        }
        available = any(name == MODEL or name.startswith(f"{MODEL}:") for name in models)
        return {
            "status": "ok" if available else "degraded",
            "ollama": True,
            "model": MODEL,
            "model_available": available,
            "database": str(DATABASE),
        }
    except Exception as exc:
        return {
            "status": "degraded",
            "ollama": False,
            "model": MODEL,
            "model_available": False,
            "database": str(DATABASE),
            "error": type(exc).__name__,
        }


@app.websocket("/ws")
async def chat_socket(websocket: WebSocket) -> None:
    """Accept chat turns and send sentence, base64 audio, and errors."""
    await websocket.accept()
    session_id = websocket.query_params.get("session_id") or str(uuid.uuid4())
    SOCKETS.setdefault(session_id, set()).add(websocket)
    await websocket.send_json({"type": "ready", "session_id": session_id, "model": MODEL})
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json(
                    {"type": "error", "message": "Send a valid JSON object."}
                )
                continue
            if not isinstance(payload, dict):
                await websocket.send_json({"type": "error", "message": "Expected a JSON object."})
                continue
            kind = payload.get("type")
            if kind == "ping":
                await websocket.send_json({"type": "pong"})
            elif kind == "stop":
                stop_event = ACTIVE_GENERATIONS.get(session_id)
                if stop_event:
                    stop_event.set()
                await websocket.send_json({"type": "stopped"})
            elif kind == "message":
                try:
                    data = ChatMessage.model_validate(payload)
                except Exception as exc:
                    await websocket.send_json({"type": "error", "message": str(exc)})
                    continue
                await generate_reply(session_id, data.message, data.language, websocket)
            elif kind == "subscribe":
                event = await asyncio.to_thread(event_for_session, session_id)
                if event:
                    await websocket.send_json({"type": "event_state", "event": event})
            else:
                await websocket.send_json({"type": "error", "message": "Unknown WebSocket message type."})
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected", extra={"session_id": session_id})
    except Exception:
        logger.exception("WebSocket handler failed", extra={"session_id": session_id})
        try:
            await websocket.send_json({"type": "error", "message": "Connection error. Please reconnect."})
        except Exception:
            pass
    finally:
        SOCKETS.get(session_id, set()).discard(websocket)
        if not SOCKETS.get(session_id):
            SOCKETS.pop(session_id, None)


@app.post("/api/events")
async def create_event(data: EventCreate) -> dict[str, Any]:
    """Create a live event and its initial ordered schedule."""
    def create() -> int:
        with connect_db() as connection:
            cursor = connection.execute(
                "INSERT INTO single_events(session_id, name, language) VALUES (?, ?, ?)",
                (data.session_id, data.name.strip(), data.language),
            )
            event_id = int(cursor.lastrowid)
            for index, item in enumerate(data.items):
                title = str(item.get("title", "")).strip()
                if not title:
                    raise ValueError("Every schedule item needs a title.")
                connection.execute(
                    """INSERT INTO single_schedule(event_id, order_index, title, person_name, designation)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        event_id, index, title, item.get("person_name", ""),
                        item.get("designation", ""),
                    ),
                )
            first = connection.execute(
                "SELECT id FROM single_schedule WHERE event_id = ? ORDER BY order_index LIMIT 1",
                (event_id,),
            ).fetchone()
            if first:
                connection.execute(
                    "UPDATE single_events SET current_item_id = ? WHERE id = ?",
                    (first["id"], event_id),
                )
            return event_id
    try:
        event_id = await asyncio.to_thread(create)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    event = await asyncio.to_thread(read_event, event_id)
    await broadcast(data.session_id, {"type": "event_state", "event": event})
    return event


@app.get("/api/events/session/{session_id}")
async def get_session_event(session_id: str) -> dict[str, Any]:
    """Return latest event linked to a UI session."""
    event = await asyncio.to_thread(event_for_session, session_id)
    if not event:
        raise HTTPException(status_code=404, detail="No event for this session.")
    return event


def _snapshot(connection: sqlite3.Connection, event_id: int) -> str:
    event = connection.execute(
        "SELECT * FROM single_events WHERE id = ?", (event_id,)
    ).fetchone()
    items = connection.execute(
        "SELECT * FROM single_schedule WHERE event_id = ? ORDER BY order_index",
        (event_id,),
    ).fetchall()
    return json.dumps(
        {"event": dict(event), "items": [dict(item) for item in items]},
        ensure_ascii=False,
    )


async def change_item(event_id: int, item_id: int, changes: dict[str, Any]) -> dict[str, Any]:
    """Validate, snapshot, and apply an item mutation."""
    allowed = {"title", "person_name", "designation", "status", "transcript", "summary"}
    if not changes or set(changes) - allowed:
        raise HTTPException(status_code=422, detail="Unsupported schedule item fields.")
    if "status" in changes and changes["status"] not in {
        "pending", "arrived", "absent", "on_stage", "done", "skipped", "removed"
    }:
        raise HTTPException(status_code=422, detail="Invalid item status.")

    def mutate() -> None:
        with connect_db() as connection:
            item = connection.execute(
                "SELECT * FROM single_schedule WHERE id = ? AND event_id = ?",
                (item_id, event_id),
            ).fetchone()
            if item is None:
                raise HTTPException(status_code=404, detail="Schedule item not found.")
            connection.execute(
                "INSERT INTO single_undo(event_id, snapshot) VALUES (?, ?)",
                (event_id, _snapshot(connection, event_id)),
            )
            assignments = ", ".join(f"{key} = ?" for key in changes)
            connection.execute(
                f"UPDATE single_schedule SET {assignments} WHERE id = ?",
                (*changes.values(), item_id),
            )
            if changes.get("status") == "absent" and item["person_name"]:
                connection.execute(
                    """UPDATE single_schedule SET status = 'absent'
                       WHERE event_id = ? AND lower(person_name) = lower(?)
                       AND order_index >= ? AND status IN ('pending', 'arrived', 'on_stage')""",
                    (event_id, item["person_name"], item["order_index"]),
                )
            if changes.get("status") in {"absent", "removed", "skipped"}:
                event = connection.execute(
                    "SELECT current_item_id FROM single_events WHERE id = ?", (event_id,)
                ).fetchone()
                if event and event["current_item_id"] == item_id:
                    next_item = connection.execute(
                        """SELECT id FROM single_schedule WHERE event_id = ? AND order_index > ?
                           AND status IN ('pending', 'arrived') ORDER BY order_index LIMIT 1""",
                        (event_id, item["order_index"]),
                    ).fetchone()
                    if next_item:
                        connection.execute(
                            "UPDATE single_schedule SET status = 'on_stage' WHERE id = ?",
                            (next_item["id"],),
                        )
                    connection.execute(
                        "UPDATE single_events SET current_item_id = ? WHERE id = ?",
                        (next_item["id"] if next_item else None, event_id),
                    )
            connection.execute(
                "UPDATE single_events SET state_version = state_version + 1 WHERE id = ?",
                (event_id,),
            )
    await asyncio.to_thread(mutate)
    event = await asyncio.to_thread(read_event, event_id)
    await broadcast(event["session_id"], {"type": "event_state", "event": event})
    return event


@app.patch("/api/events/{event_id}/items/{item_id}")
async def patch_item(event_id: int, item_id: int, changes: dict[str, Any]) -> dict[str, Any]:
    return await change_item(event_id, item_id, changes)


@app.post("/api/events/{event_id}/items")
async def add_item(event_id: int, payload: dict[str, str]) -> dict[str, Any]:
    title = payload.get("title", "").strip()
    if not title:
        raise HTTPException(status_code=422, detail="Schedule item title is required.")

    def insert() -> None:
        with connect_db() as connection:
            connection.execute(
                "INSERT INTO single_undo(event_id, snapshot) VALUES (?, ?)",
                (event_id, _snapshot(connection, event_id)),
            )
            order = int(connection.execute(
                "SELECT COALESCE(MAX(order_index), -1) + 1 FROM single_schedule WHERE event_id = ?",
                (event_id,),
            ).fetchone()[0])
            connection.execute(
                """INSERT INTO single_schedule(event_id, order_index, title, person_name, designation)
                   VALUES (?, ?, ?, ?, ?)""",
                (event_id, order, title, payload.get("person_name", ""), payload.get("designation", "")),
            )
            connection.execute(
                "UPDATE single_events SET state_version = state_version + 1 WHERE id = ?",
                (event_id,),
            )
    try:
        await asyncio.to_thread(insert)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=404, detail="Event not found.") from exc
    event = await asyncio.to_thread(read_event, event_id)
    await broadcast(event["session_id"], {"type": "event_state", "event": event})
    return event


@app.delete("/api/events/{event_id}/items/{item_id}")
async def delete_item(event_id: int, item_id: int) -> dict[str, Any]:
    return await change_item(event_id, item_id, {"status": "removed"})


@app.post("/api/events/{event_id}/reorder")
async def reorder_items(event_id: int, payload: dict[str, list[int]]) -> dict[str, Any]:
    item_ids = payload.get("item_ids", [])

    def reorder() -> None:
        with connect_db() as connection:
            existing = [
                int(row[0]) for row in connection.execute(
                    "SELECT id FROM single_schedule WHERE event_id = ? ORDER BY order_index", (event_id,)
                )
            ]
            if len(existing) != len(item_ids) or set(existing) != set(item_ids):
                raise ValueError("Supply every schedule item exactly once.")
            connection.execute(
                "INSERT INTO single_undo(event_id, snapshot) VALUES (?, ?)",
                (event_id, _snapshot(connection, event_id)),
            )
            for index, item_id in enumerate(item_ids):
                connection.execute(
                    "UPDATE single_schedule SET order_index = ? WHERE id = ? AND event_id = ?",
                    (100000 + index, item_id, event_id),
                )
            for index, item_id in enumerate(item_ids):
                connection.execute(
                    "UPDATE single_schedule SET order_index = ? WHERE id = ? AND event_id = ?",
                    (index, item_id, event_id),
                )
            connection.execute(
                "UPDATE single_events SET state_version = state_version + 1 WHERE id = ?",
                (event_id,),
            )
    try:
        await asyncio.to_thread(reorder)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    event = await asyncio.to_thread(read_event, event_id)
    await broadcast(event["session_id"], {"type": "event_state", "event": event})
    return event


@app.post("/api/events/{event_id}/undo")
async def undo_event(event_id: int) -> dict[str, Any]:
    def undo() -> None:
        with connect_db() as connection:
            row = connection.execute(
                "SELECT id, snapshot FROM single_undo WHERE event_id = ? ORDER BY id DESC LIMIT 1",
                (event_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Nothing to undo.")
            snapshot = json.loads(row["snapshot"])
            old_version = connection.execute(
                "SELECT state_version FROM single_events WHERE id = ?", (event_id,)
            ).fetchone()
            if old_version is None:
                raise LookupError("Event not found.")
            event = snapshot["event"]
            connection.execute(
                """UPDATE single_events SET name = ?, language = ?, status = ?,
                   current_item_id = ?, state_version = ? WHERE id = ?""",
                (
                    event["name"], event["language"], event["status"], event["current_item_id"],
                    old_version["state_version"] + 1, event_id,
                ),
            )
            current_ids = {
                row[0] for row in connection.execute(
                    "SELECT id FROM single_schedule WHERE event_id = ?", (event_id,)
                )
            }
            snapshot_ids = {item["id"] for item in snapshot["items"]}
            connection.executemany(
                "UPDATE single_schedule SET order_index = ? WHERE id = ?",
                [(100000 + index, item["id"]) for index, item in enumerate(snapshot["items"])],
            )
            for item in snapshot["items"]:
                connection.execute(
                    """UPDATE single_schedule SET order_index = ?, title = ?, person_name = ?,
                       designation = ?, status = ?, transcript = ?, summary = ? WHERE id = ?""",
                    (
                        item["order_index"], item["title"], item["person_name"],
                        item["designation"], item["status"], item["transcript"],
                        item["summary"], item["id"],
                    ),
                )
            for removed_id in current_ids - snapshot_ids:
                connection.execute(
                    "UPDATE single_schedule SET status = 'removed' WHERE id = ?", (removed_id,)
                )
            connection.execute("DELETE FROM single_undo WHERE id = ?", (row["id"],))
    try:
        await asyncio.to_thread(undo)
    except (ValueError, LookupError) as exc:
        raise HTTPException(status_code=404 if isinstance(exc, LookupError) else 409, detail=str(exc)) from exc
    event = await asyncio.to_thread(read_event, event_id)
    await broadcast(event["session_id"], {"type": "event_state", "event": event})
    return event


@app.post("/api/events/{event_id}/call-now")
async def call_next(event_id: int) -> dict[str, Any]:
    def advance() -> None:
        with connect_db() as connection:
            event = connection.execute(
                "SELECT current_item_id FROM single_events WHERE id = ?", (event_id,)
            ).fetchone()
            if event is None:
                raise LookupError("Event not found.")
            current = connection.execute(
                "SELECT order_index FROM single_schedule WHERE id = ?",
                (event["current_item_id"],),
            ).fetchone()
            cursor = int(current["order_index"]) if current else -1
            next_item = connection.execute(
                """SELECT id FROM single_schedule WHERE event_id = ? AND order_index > ?
                   AND status IN ('pending', 'arrived') ORDER BY order_index LIMIT 1""",
                (event_id, cursor),
            ).fetchone()
            if next_item is None:
                return
            connection.execute(
                "INSERT INTO single_undo(event_id, snapshot) VALUES (?, ?)",
                (event_id, _snapshot(connection, event_id)),
            )
            connection.execute(
                """UPDATE single_schedule SET status = 'done'
                   WHERE id = ? AND status IN ('on_stage', 'pending', 'arrived')""",
                (event["current_item_id"],),
            )
            connection.execute(
                "UPDATE single_schedule SET status = 'on_stage' WHERE id = ?", (next_item["id"],)
            )
            connection.execute(
                "UPDATE single_events SET current_item_id = ?, status = 'running', state_version = state_version + 1 WHERE id = ?",
                (next_item["id"], event_id),
            )
    try:
        await asyncio.to_thread(advance)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    event = await asyncio.to_thread(read_event, event_id)
    await broadcast(event["session_id"], {"type": "event_state", "event": event})
    current = next(
        (item for item in event["items"] if item["id"] == event["current_item_id"]),
        None,
    )
    announcement = ""
    if current:
        blocked = absent_names(event)
        guest = current["person_name"].strip()
        title = current["title"].strip()
        prompt = (
            "Write one concise spoken introduction for the next live event item. "
            "Use only the supplied title and guest; never invent facts. "
            f"Item title: {title}\nGuest: {guest or 'not specified'}"
        )
        try:
            result = await ollama.chat(
                model=MODEL,
                messages=[{"role": "user", "content": prompt}],
            )
            announcement = " ".join(
                safe_sentences(result.message.content or "", blocked)
            ).strip()
        except Exception:
            logger.exception("Could not generate a live item introduction")
        if not announcement:
            safe_title = title
            for name in blocked:
                safe_title = re.sub(re.escape(name), "", safe_title, flags=re.IGNORECASE)
            announcement = f"We now move to {safe_title.strip()}."
    return {"event": event, "announcement": announcement}


@app.post("/api/events/{event_id}/stop")
async def stop_event_speech(event_id: int) -> dict[str, str]:
    event = await asyncio.to_thread(read_event, event_id)
    stop_event = ACTIVE_GENERATIONS.get(event["session_id"])
    if stop_event:
        stop_event.set()
    await broadcast(
        event["session_id"],
        {"type": "speech_stop", "event_id": event_id},
    )
    return {"status": "stopped"}


@app.post("/api/sessions/{session_id}/stop")
async def stop_session_speech(session_id: str) -> dict[str, str]:
    """Stop an in-progress response without requiring an event to exist."""
    stop_event = ACTIVE_GENERATIONS.get(session_id)
    if stop_event:
        stop_event.set()
    await broadcast(session_id, {"type": "speech_stop"})
    return {"status": "stopped"}


@app.post("/api/events/{event_id}/items/{item_id}/summarize")
async def summarize_speech(event_id: int, item_id: int, payload: dict[str, str]) -> dict[str, Any]:
    """Summarize operator-provided or captured transcript using Ollama."""
    event = await asyncio.to_thread(read_event, event_id)
    item = next((item for item in event["items"] if item["id"] == item_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="Schedule item not found.")
    transcript = (payload.get("transcript") or item["transcript"]).strip()
    if len(transcript.split()) < 8:
        summary = (
            "ട്രാൻസ്ക്രിപ്റ്റ് വളരെ ചുരുങ്ങിയതിനാൽ സംഗ്രഹം തയ്യാറാക്കാനായില്ല."
            if event["language"] == "ml"
            else "The summary could not be generated because the transcript is too short."
        )
    else:
        language = (
            "natural spoken Malayalam in Malayalam script"
            if event["language"] == "ml"
            else "natural spoken English"
        )
        prompt = (
            "Summarize only explicit transcript facts in 2–3 short sentences. "
            "Do not invent anything and do not mention absent, removed, or skipped people. "
            "Use " + language + f". Blocked names: {json.dumps(absent_names(event), ensure_ascii=False)}"
            + ".\nTranscript:\n" + transcript
        )
        try:
            response = await ollama.chat(
                model=MODEL, messages=[{"role": "user", "content": prompt}]
            )
            summary = " ".join(
                safe_sentences(response.message.content or "", absent_names(event))
            ).strip()
            if not summary:
                summary = "The summary could not be generated."
        except Exception as exc:
            logger.exception("Speaker summary generation failed")
            raise HTTPException(status_code=503, detail=f"Ollama summary failed ({type(exc).__name__}).") from exc
    with connect_db() as connection:
        connection.execute(
            "UPDATE single_schedule SET transcript = ?, summary = ? WHERE id = ? AND event_id = ?",
            (transcript, summary, item_id, event_id),
        )
        connection.execute(
            "UPDATE single_events SET state_version = state_version + 1 WHERE id = ?",
            (event_id,),
        )
    updated = await asyncio.to_thread(read_event, event_id)
    await broadcast(updated["session_id"], {"type": "event_state", "event": updated})
    return {"summary": summary, "event": updated}


CHAT_PAGE = r"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AVA — Your voice on stage</title>
<style>
body{margin:0;background:#101522;color:#eef2ff;font:16px system-ui}main{max-width:900px;margin:auto;padding:28px}
header,nav,form,.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}h1{margin:4px 0}
button,select,textarea{font:inherit;color:inherit;background:#202a3c;border:1px solid #39465e;border-radius:12px;padding:12px}
button,a{cursor:pointer}a{color:#80baff}.chat{height:55vh;overflow:auto;padding:12px;border:1px solid #28344a;border-radius:16px}
.msg{white-space:pre-wrap;margin:10px 0;padding:12px;background:#1b2638;border-radius:12px}.user{background:#26364e}
textarea{flex:1;min-width:220px}.status{color:#a9bdd9}#health{padding:12px;color:#ffcb84}.hidden{display:none}
</style><main><header><div><small>AI VIRTUAL ANCHORING ASSISTANT</small><h1>Your voice on stage.</h1></div>
<a href="/control">Live event control</a><select id="lang" aria-label="Language"><option value="en">English</option><option value="ml">മലയാളം</option></select><button id="enable-audio">Enable audio</button><span id="status" class="status">Connecting</span></header>
<p id="health"></p><section id="chat" class="chat" aria-live="polite"></section>
<form id="form"><textarea id="input" rows="2" maxlength="8000" placeholder="Ask AVA…"></textarea>
<button type="button" id="mic">🎙 Speak</button><button type="submit">Send</button><button type="button" id="stop">Stop</button></form>
<p class="status">Audio plays from Edge TTS when available; browser speech synthesis is the fallback.</p></main>
<script>
const $=s=>document.querySelector(s), chat=$("#chat"), sid=localStorage.getItem("ava-single-session")||crypto.randomUUID();
localStorage.setItem("ava-single-session",sid); let ws,retry=0,timer,assistant=null,activeAudio=null,audioQueue=Promise.resolve(),audioGeneration=0;
function bubble(text,role){const d=document.createElement("div");d.className="msg "+role;d.textContent=text;chat.append(d);chat.scrollTop=chat.scrollHeight;return d}
function connect(){clearTimeout(timer);$("#status").textContent="Connecting";ws=new WebSocket(`${location.protocol==="https:"?"wss":"ws"}://${location.host}/ws?session_id=${encodeURIComponent(sid)}`);
ws.onopen=()=>{retry=0;$("#status").textContent="Connected";ws.send(JSON.stringify({type:"subscribe"}));};
ws.onclose=()=>{$("#status").textContent="Disconnected — reconnecting";timer=setTimeout(connect,Math.min(500*2**retry++,30000));};
ws.onerror=()=>{$("#status").textContent="Connection error";};
ws.onmessage=async e=>{const p=JSON.parse(e.data);if(p.type==="ready")$("#status").textContent="Connected · "+p.model;
if(p.type==="sentence"){if(!assistant)assistant=bubble("","assistant");assistant.textContent+=(assistant.textContent?" ":"")+p.text;
const generation=audioGeneration;audioQueue=audioQueue.then(()=>playSentence(p,generation));}
if(p.type==="complete"||p.type==="error"){if(p.type==="error")bubble(p.message,"assistant");assistant=null;}
if(p.type==="speech_stop"){stopPlayback();}
if(p.type==="event_state")localStorage.setItem("ava-single-event",p.event.id);};}
function stopPlayback(){audioGeneration++;activeAudio?.pause();activeAudio=null;speechSynthesis.cancel()}
function speak(text,language){return new Promise(resolve=>{const u=new SpeechSynthesisUtterance(text);u.lang=language==="ml"?"ml-IN":"en-IN";u.onend=resolve;u.onerror=resolve;speechSynthesis.speak(u)})}
async function playSentence(p,generation){if(generation!==audioGeneration)return;if(p.audio){const a=new Audio("data:audio/mpeg;base64,"+p.audio);activeAudio=a;
try{await a.play();await new Promise(resolve=>{a.onended=resolve;a.onerror=resolve})}catch{await speak(p.text,p.language)}finally{if(activeAudio===a)activeAudio=null}}
else await speak(p.text,p.language)}
$("#enable-audio").onclick=()=>{const u=new SpeechSynthesisUtterance("");speechSynthesis.speak(u);$("#enable-audio").textContent="Audio enabled"};
$("#form").onsubmit=e=>{e.preventDefault();const message=$("#input").value.trim();if(!message)return;if(!ws||ws.readyState!==1){bubble("Still reconnecting; please try again when connected.","assistant");return}
bubble(message,"user");ws.send(JSON.stringify({type:"message",message,language:$("#lang").value}));$("#input").value=""};
$("#stop").onclick=()=>{stopPlayback();fetch(`/api/sessions/${encodeURIComponent(sid)}/stop`,{method:"POST"})};
$("#mic").onclick=()=>{const SR=window.SpeechRecognition||window.webkitSpeechRecognition;if(!SR){bubble("SpeechRecognition is unavailable; use the text box.","assistant");return}
const rec=new SR();rec.lang=$("#lang").value==="ml"?"ml-IN":"en-IN";rec.interimResults=false;rec.onresult=e=>{$("#input").value=e.results[0][0].transcript};rec.onerror=e=>bubble("Microphone recognition failed: "+e.error,"assistant");rec.start()};
fetch("/api/health").then(r=>r.json()).then(h=>{if(h.status!=="ok")$("#health").textContent="Ollama/model unavailable. Start Ollama and pull "+h.model+" before chatting."});
connect();
</script></html>"""


CONTROL_PAGE = r"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AVA — Live event control</title><style>
body{margin:0;background:#101522;color:#eef2ff;font:16px system-ui}main{max-width:1100px;margin:auto;padding:24px}
button,input,select,textarea{font:inherit;color:inherit;background:#202a3c;border:1px solid #39465e;border-radius:10px;padding:10px;margin:4px}
button,a{cursor:pointer;color:#a8d2ff}section,.item{border:1px solid #39465e;border-radius:14px;padding:14px;margin:12px 0}
.item.current{border-color:#65d9b0}.row{display:flex;gap:8px;flex-wrap:wrap}textarea{width:95%;min-height:90px}
</style><main><a href="/">← Chat</a><h1>Live event control</h1><p id="status">Loading…</p>
<section><h2>Event</h2><form id="create"><input name="name" required placeholder="Event name"><select name="language"><option value="en">English</option><option value="ml">മലയാളം</option></select><button>Create event</button></form>
<div id="event"></div><div class="row"><button id="add">Add item</button><button id="undo">Undo</button><button id="call">Call next item now</button><button id="stop">AVA stop speaking</button></div></section>
<section><h2>Speaker summary</h2><label>Choose item <select id="speaker"></select></label><textarea id="transcript" placeholder="Paste or edit transcript (mic capture can be added by browser)"></textarea>
<button id="summarize">Generate summary</button><textarea id="summary" placeholder="Editable summary"></textarea><button id="save-summary">Save edited summary</button></section></main>
<script>
const $=s=>document.querySelector(s),sid=localStorage.getItem("ava-single-session")||crypto.randomUUID();localStorage.setItem("ava-single-session",sid);
let eventState=null;const esc=s=>String(s||"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
async function req(url,options={}){const r=await fetch(url,{headers:{"Content-Type":"application/json"},...options});const d=await r.json();if(!r.ok)throw Error(d.detail||r.statusText);return d}
function render(ev){eventState=ev;localStorage.setItem("ava-single-event",ev.id);$("#status").textContent=`${ev.name} · ${ev.status} · version ${ev.state_version}`;
$("#event").innerHTML=ev.items.map((i,n)=>`<article class="item ${i.id===ev.current_item_id?"current":""}"><b>${n+1}. ${esc(i.title)}</b> — ${esc(i.person_name)} (${i.status})
<div class="row"><button data-action="arrived" data-id="${i.id}">Arrived</button><button data-action="absent" data-id="${i.id}">Absent</button><button data-action="skipped" data-id="${i.id}">Skip</button>
<button data-action="edit" data-id="${i.id}">Edit</button><button data-action="delete" data-id="${i.id}">Delete</button>
<button data-action="up" data-id="${i.id}">↑</button><button data-action="down" data-id="${i.id}">↓</button></div></article>`).join("");
$("#speaker").innerHTML=ev.items.map(i=>`<option value="${i.id}">${esc(i.title)} — ${esc(i.person_name)}</option>`).join("");}
async function refresh(){const id=Number(localStorage.getItem("ava-single-event"));if(id){render(await req(`/api/events/${id}`));return}
try{render(await req(`/api/events/session/${encodeURIComponent(sid)}`))}catch{$("#status").textContent="Create an event to begin."}}
$("#create").onsubmit=async e=>{e.preventDefault();try{const f=new FormData(e.currentTarget);render(await req("/api/events",{method:"POST",body:JSON.stringify({session_id:sid,name:f.get("name"),language:f.get("language"),items:[]})}));}catch(err){alert(err.message)}};
$("#event").onclick=async e=>{const b=e.target.closest("button[data-action]");if(!b)return;const id=Number(b.dataset.id),item=eventState.items.find(i=>i.id===id);try{
if(b.dataset.action==="edit"){const title=prompt("Item title",item.title);if(title===null)return;const person=prompt("Speaker / person",item.person_name);await req(`/api/events/${eventState.id}/items/${id}`,{method:"PATCH",body:JSON.stringify({title,person_name:person||""})});}
else if(b.dataset.action==="delete"){if(confirm("Remove this item?"))await req(`/api/events/${eventState.id}/items/${id}`,{method:"DELETE"});}
else if(["arrived","absent","skipped"].includes(b.dataset.action))await req(`/api/events/${eventState.id}/items/${id}`,{method:"PATCH",body:JSON.stringify({status:b.dataset.action})});
else {const ids=eventState.items.map(i=>i.id),at=ids.indexOf(id),to=at+(b.dataset.action==="up"?-1:1);if(to>=0&&to<ids.length){[ids[at],ids[to]]=[ids[to],ids[at]];await req(`/api/events/${eventState.id}/reorder`,{method:"POST",body:JSON.stringify({item_ids:ids})})}}
await refresh()}catch(err){alert(err.message)}};
$("#add").onclick=async()=>{if(!eventState)return alert("Create an event first.");const title=prompt("Item title");if(!title)return;const person=prompt("Speaker / person","")||"";try{await req(`/api/events/${eventState.id}/items`,{method:"POST",body:JSON.stringify({title,person_name:person})});await refresh()}catch(e){alert(e.message)}};
$("#undo").onclick=async()=>{if(eventState)try{render(await req(`/api/events/${eventState.id}/undo`,{method:"POST"}))}catch(e){alert(e.message)}};
$("#call").onclick=async()=>{if(eventState)try{const result=await req(`/api/events/${eventState.id}/call-now`,{method:"POST"});render(result.event);if(result.announcement){speechSynthesis.cancel();const u=new SpeechSynthesisUtterance(result.announcement);u.lang=eventState.language==="ml"?"ml-IN":"en-IN";speechSynthesis.speak(u)}}catch(e){alert(e.message)}};
$("#stop").onclick=async()=>{speechSynthesis.cancel();if(eventState)await req(`/api/events/${eventState.id}/stop`,{method:"POST"})};
$("#summarize").onclick=async()=>{if(!eventState)return;const id=Number($("#speaker").value);try{const result=await req(`/api/events/${eventState.id}/items/${id}/summarize`,{method:"POST",body:JSON.stringify({transcript:$("#transcript").value})});$("#summary").value=result.summary;render(result.event)}catch(e){alert(e.message)}};
$("#save-summary").onclick=async()=>{if(!eventState)return;const id=Number($("#speaker").value);try{render(await req(`/api/events/${eventState.id}/items/${id}`,{method:"PATCH",body:JSON.stringify({summary:$("#summary").value,transcript:$("#transcript").value})}))}catch(e){alert(e.message)}};
$("#speaker").onchange=()=>{const item=eventState?.items.find(i=>i.id===Number($("#speaker").value));$("#transcript").value=item?.transcript||"";$("#summary").value=item?.summary||""};
refresh();setInterval(()=>{if(!document.hidden)refresh()},5000);
</script></html>"""


@app.get("/api/events/{event_id}")
async def get_event(event_id: int) -> dict[str, Any]:
    return await asyncio.to_thread(read_event, event_id)


def main() -> None:
    """Start the local development server using configured bind settings."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()


def create_app(settings: Any = None) -> FastAPI:
    """Backward-compatible factory for the original modular app's test callers."""
    from interface.api import create_app as create_modular_app

    return create_modular_app(settings)
