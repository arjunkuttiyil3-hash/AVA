"""Scheduled event-summary and event-linked transcription behavior."""

from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import create_app
from config.settings import AppSettings
from services.event_service import EventService


def test_due_summaries_use_local_event_end_and_once_flag(tmp_path: Path) -> None:
    service = EventService(tmp_path / "scheduled-summary.sqlite3")
    from memory.repository import MemoryRepository

    session_id = MemoryRepository(service.database_path).create_session()
    event = service.create_event(
        session_id,
        name="Guest lecture",
        event_type="lecture",
        date="2026-10-27",
        event_start_time="10:00",
        event_end_time="11:00",
    )
    event_id = event["id"]
    service.start(event_id)
    assert service.append_event_transcript(
        event_id,
        "The lecturer described the project timeline and thanked students for their questions.",
    )

    before_end = service.get_due_event_summaries(
        now=datetime(2026, 10, 27, 10, 59, 59)
    )
    after_end = service.get_due_event_summaries(
        now=datetime(2026, 10, 27, 11, 0)
    )

    assert before_end == []
    assert [row["id"] for row in after_end] == [event_id]
    service.set_auto_speak(session_id, False)
    assert service.get_due_event_summaries(
        now=datetime(2026, 10, 27, 11, 0)
    ) == []
    service.set_auto_speak(session_id, True)
    assert service.mark_event_summary_played(
        event_id, "The lecturer outlined the timeline. Students asked questions."
    )
    assert not service.mark_event_summary_played(event_id, "A duplicate summary.")
    saved = service.get_event(event_id)
    assert saved["summary_played"] == 1
    assert saved["event_summary"].startswith("The lecturer")
    assert service.get_due_event_summaries(now=datetime(2026, 10, 27, 12, 0)) == []


def test_event_transcripts_and_summaries_skip_unconfigured_types(tmp_path: Path) -> None:
    service = EventService(tmp_path / "non-summary-event.sqlite3")
    from memory.repository import MemoryRepository

    session_id = MemoryRepository(service.database_path).create_session()
    event = service.create_event(
        session_id,
        name="Music event",
        event_type="festival",
        date="2026-10-27",
        event_start_time="10:00",
        event_end_time="11:00",
    )
    service.start(event["id"])
    assert not service.append_event_transcript(event["id"], "A transcript that should be ignored.")
    assert service.get_event(event["id"])["transcript"] == ""


def test_transcribe_endpoint_returns_clear_error_when_model_loading_fails(
    tmp_path: Path, monkeypatch
) -> None:
    from interface import api as api_module

    monkeypatch.setattr(
        api_module,
        "collect_health",
        lambda settings: {
            "gemini": {"available": True, "configured": True, "message": "ok"},
            "ffmpeg": {"available": True, "message": "ok"},
            "microphone": {"available": True, "message": "ok"},
            "database": {"available": True, "message": str(settings.database_path)},
        },
    )
    application = create_app(
        AppSettings(
            config_path=tmp_path / "ava.yaml",
            memory={"database_path": str(tmp_path / "stt-error.sqlite3")},
            llm={"routes": {"default": {"backend": "template"}}},
            speech={"cache_dir": str(tmp_path / "audio")},
            robot={"mode": "simulated"},
        )
    )

    def fail_model_load() -> None:
        raise TypeError("open() got an unexpected keyword argument 'metadata_errors'")

    with TestClient(application) as client:
        application.state.recognizer.load_model = fail_model_load
        response = client.post(
            "/api/transcribe",
            files={"audio": ("sample.webm", b"sample audio", "audio/webm")},
            data={"language": "en"},
        )

    assert response.status_code == 503
    assert response.json()["error"] == "speech_recognition_unavailable"
    assert response.json()["cause"] == "TypeError"
    assert "metadata_errors" in response.json()["diagnostic"]


def test_transcribe_endpoint_stores_chunks_on_running_eligible_event(
    tmp_path: Path, monkeypatch
) -> None:
    from interface import api as api_module

    monkeypatch.setattr(
        api_module,
        "collect_health",
        lambda settings: {
            "gemini": {"available": True, "configured": True, "message": "ok"},
            "ffmpeg": {"available": True, "message": "ok"},
            "microphone": {"available": True, "message": "ok"},
            "database": {"available": True, "message": str(settings.database_path)},
        },
    )
    application = create_app(
        AppSettings(
            config_path=tmp_path / "ava.yaml",
            memory={"database_path": str(tmp_path / "stt-event.sqlite3")},
            llm={"routes": {"default": {"backend": "template"}}},
            speech={"cache_dir": str(tmp_path / "audio")},
            robot={"mode": "simulated"},
        )
    )
    transcript = "The speaker explained the project timeline and thanked all student volunteers."

    with TestClient(application) as client:
        session_id = application.state.pipeline.repository.create_session()
        event = application.state.event_service.create_event(
            session_id,
            name="Project talk",
            event_type="talk",
            date=date.today().isoformat(),
            event_start_time="00:01",
            event_end_time="23:59",
        )
        application.state.event_service.start(event["id"])
        application.state.recognizer.load_model = lambda: None
        application.state.recognizer.transcribe = lambda path, language: SimpleNamespace(
            text=transcript, language=language
        )
        response = client.post(
            "/api/transcribe",
            files={"audio": ("chunk.webm", b"sample audio", "audio/webm")},
            data={"language": "en", "event_id": str(event["id"])},
        )

    assert response.status_code == 200
    assert response.json()["event_transcript_stored"] is True
    assert application.state.event_service.get_event(event["id"])["transcript"] == transcript


def test_scheduler_speaks_due_event_summary_once(tmp_path: Path, monkeypatch) -> None:
    from interface import api as api_module
    from memory.repository import MemoryRepository

    monkeypatch.setattr(
        api_module,
        "collect_health",
        lambda settings: {
            "gemini": {"available": True, "configured": True, "message": "ok"},
            "ffmpeg": {"available": True, "message": "ok"},
            "microphone": {"available": True, "message": "ok"},
            "database": {"available": True, "message": str(settings.database_path)},
        },
    )
    application = create_app(
        AppSettings(
            config_path=tmp_path / "ava.yaml",
            memory={"database_path": str(tmp_path / "event-audio.sqlite3")},
            llm={"routes": {"default": {"backend": "template"}}},
            speech={
                "cache_dir": str(tmp_path / "audio"),
                "event_summary_check_seconds": 0.05,
            },
            robot={"mode": "simulated"},
        )
    )
    summary = (
        "The speaker outlined the project timeline. Students will begin research next week."
    )

    def fake_stream(task, language, messages):
        assert task == "speaker_summary"
        assert "project timeline" in messages[-1]["content"]
        return [summary], SimpleNamespace(backend="template", model="test")

    async def fake_synthesize(
        text, language, *, voice=None, cache_version=None
    ) -> Path:
        _ = (text, language, voice, cache_version)
        path = tmp_path / "audio" / "event-summary.mp3"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"summary-audio")
        return path

    with TestClient(application) as client:
        service = application.state.event_service
        session_id = MemoryRepository(service.database_path).create_session()
        event = service.create_event(
            session_id,
            name="Project presentation",
            event_type="presentation",
            date=(date.today() - timedelta(days=1)).isoformat(),
            event_start_time="09:00",
            event_end_time="23:59",
        )
        service.start(event["id"])
        service.append_event_transcript(
            event["id"],
            "The speaker explained the project timeline and thanked students for helping.",
        )
        application.state.pipeline.router.stream = fake_stream
        application.state.pipeline.synthesizer.synthesize = fake_synthesize
        with client.websocket_connect(f"/ws/{session_id}") as websocket:
            websocket.send_json(
                {"type": "subscribe_event", "client": "control", "event_id": event["id"]}
            )
            messages = []
            while not any(message["type"] == "event_summary_audio" for message in messages):
                messages.append(websocket.receive_json())

    audio_message = next(
        message for message in messages if message["type"] == "event_summary_audio"
    )
    assert audio_message["event_id"] == event["id"]
    assert audio_message["summary"] == summary
    saved = service.get_event(event["id"])
    assert saved["summary_played"] == 1
    assert saved["event_summary"] == summary
    assert service.get_due_event_summaries() == []
