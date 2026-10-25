"""FastAPI integration tests without a live model, microphone, or robot."""

from pathlib import Path

from fastapi.testclient import TestClient

from app import create_app
from config.settings import AppSettings


def test_api_serves_ui_session_and_event_state(tmp_path: Path, monkeypatch) -> None:
    from interface import api as api_module

    monkeypatch.setattr(
        api_module,
        "collect_health",
        lambda settings: {
            "gemini": {
                "available": False,
                "configured": False,
                "model": "gemini-2.5-flash",
                "message": "Gemini API key missing",
            },
            "ffmpeg": {"available": True, "message": "ffmpeg"},
            "microphone": {"available": False, "message": "No input"},
            "database": {"available": True, "message": str(settings.database_path)},
        },
    )
    database = tmp_path / "integration.sqlite3"
    monkeypatch.setenv("AVA_DB_PATH", str(database))
    settings = AppSettings(
        config_path=tmp_path / "ava.yaml",
        memory={"database_path": str(database)},
        llm={"routes": {"default": {"backend": "template"}}},
        speech={"voices": {"en": "en-IN-NeerjaNeural"}, "cache_dir": str(tmp_path / "audio")},
        robot={"mode": "simulated"},
    )
    application = create_app(settings)

    with TestClient(application) as client:
        assert client.get("/health").json() == {"status": "ok"}
        status = client.get("/api/health").json()
        assert status["status"] == "degraded"
        assert status["checks"]["gemini"]["available"] is False
        home = client.get("/").text
        assert "Your voice on stage" in home
        assert "Anchoring Studio" in home
        assert "Generate script" in home
        assert "gemini-2.5-flash" in home
        assert "Live event control" in client.get("/control").text
        assert client.get("/static/css/ava.css").status_code == 200
        session_id = client.post("/api/sessions").json()["session_id"]

        event = client.post(
            "/api/events",
            json={
                "session_id": session_id,
                "name": "Arts day",
                "schedule": [
                    {"time": "11:00", "title": "Dance", "participant": "Maya"},
                    {"time": "10:00", "title": "Welcome"},
                ],
            },
        )

        assert event.status_code == 200
        assert [item["time"] for item in event.json()["schedule"]] == ["10:00", "11:00"]
        state = client.get(f"/api/memory/{session_id}").json()
        assert state["event"]["name"] == "Arts day"
        assert state["message_count"] == 0
        next_item = client.post(f"/api/events/{event.json()['id']}/next").json()
        assert next_item["current"]["title"] == "Dance"


def test_websocket_streams_template_response(tmp_path: Path, monkeypatch) -> None:
    from interface import api as api_module

    monkeypatch.setattr(
        api_module,
        "collect_health",
        lambda settings: {
            "gemini": {"available": True, "configured": True, "model": "gemini-2.5-flash", "message": "ok"},
            "ffmpeg": {"available": True, "message": "ok"},
            "microphone": {"available": True, "message": "ok"},
            "database": {"available": True, "message": str(settings.database_path)},
        },
    )
    database = tmp_path / "socket.sqlite3"
    settings = AppSettings(
        config_path=tmp_path / "ava.yaml",
        memory={"database_path": str(database), "short_term_turns": 4},
        llm={"routes": {"default": {"backend": "template"}}},
        speech={"voices": {"en": "en-IN-NeerjaNeural"}, "cache_dir": str(tmp_path / "audio")},
        robot={"mode": "simulated"},
    )
    application = create_app(settings)

    with TestClient(application) as client:
        pipeline = application.state.pipeline

        async def no_audio(text: str, language: str, *, voice=None):
            _ = (text, language, voice)
            audio_path = tmp_path / "audio" / ("a" * 64 + ".mp3")
            audio_path.parent.mkdir(parents=True, exist_ok=True)
            audio_path.write_bytes(b"mp3-data")
            return audio_path

        def no_audio_status(audio_path: Path):
            _ = audio_path
            return None

        pipeline.synthesizer.synthesize = no_audio
        pipeline.synthesizer.get_status = no_audio_status
        session_id = client.post("/api/sessions").json()["session_id"]
        with client.websocket_connect(f"/ws/{session_id}") as websocket:
            websocket.send_json({"type": "ping", "time": 123})
            pong = websocket.receive_json()
            assert pong == {"type": "pong", "time": 123}
            websocket.send_text("not-json")
            malformed = websocket.receive_json()
            assert malformed["type"] == "error"
            websocket.send_json({"message": "hello", "mode": "chat", "language": "en"})
            events = []
            while True:
                event = websocket.receive_json()
                events.append(event)
                if event["type"] == "complete":
                    break
        assert any(event["type"] == "decision" for event in events)
        assert any(event["type"] == "token" for event in events)
        assert any(event["type"] == "audio" for event in events)
        assert events[-1]["type"] == "complete"
        audio_event = next(event for event in events if event["type"] == "audio")
        audio_response = client.get(audio_event["url"])
        assert audio_response.status_code == 200
        assert audio_response.content == b"mp3-data"
        assert application.state.pipeline.repository.get_recent_messages(session_id, 2)[-1]["role"] == "assistant"


def test_live_event_updates_broadcast_to_two_websocket_clients(
    tmp_path: Path, monkeypatch
) -> None:
    from interface import api as api_module

    monkeypatch.setattr(
        api_module,
        "collect_health",
        lambda settings: {
            "gemini": {"available": True, "configured": True, "model": "gemini-2.5-flash", "message": "ok"},
            "ffmpeg": {"available": True, "message": "ok"},
            "microphone": {"available": True, "message": "ok"},
            "database": {"available": True, "message": str(settings.database_path)},
        },
    )
    database = tmp_path / "event-sync.sqlite3"
    settings = AppSettings(
        config_path=tmp_path / "ava.yaml",
        memory={"database_path": str(database)},
        llm={"routes": {"default": {"backend": "template"}}},
        speech={"cache_dir": str(tmp_path / "audio")},
        robot={"mode": "simulated"},
    )
    application = create_app(settings)
    with TestClient(application) as client:
        session_id = client.post("/api/sessions").json()["session_id"]
        created = client.post(
            "/api/events",
            json={
                "session_id": session_id,
                "name": "Speaker event",
                "schedule": [
                    {"title": "Guest speech", "type": "speech", "participant": "Dr. Nair"},
                    {"title": "മലയാളം പരിപാടി", "type": "performance"},
                ],
            },
        ).json()
        item_id = application.state.event_service.get_event(created["id"])["items"][0]["id"]
        endpoint = f"/ws/{session_id}"
        with client.websocket_connect(endpoint) as control, client.websocket_connect(endpoint) as main:
            assert control.receive_json()["type"] == "event_state_changed"
            assert main.receive_json()["type"] == "event_state_changed"
            response = client.patch(
                f"/api/event/items/{item_id}",
                json={"event_id": created["id"], "status": "absent"},
            )
            assert response.status_code == 200
            markdown = client.get(f"/api/live-events/{created['id']}/report?format=md")
            pdf = client.get(f"/api/live-events/{created['id']}/report?format=pdf")
            docx = client.get(f"/api/live-events/{created['id']}/report?format=docx")
            assert "# Speaker event" in markdown.text
            assert pdf.content.startswith(b"%PDF-")
            assert docx.content.startswith(b"PK")
            assert client.get(f"/api/event/settings/{session_id}").json() == {
                "auto_speak_summary": False
            }
            for websocket in (control, main):
                update = websocket.receive_json()
                assert update["type"] == "event_state_changed"
                assert update["state_version"] == response.json()["state_version"]
                assert update["event"]["items"][0]["status"] == "absent"


def test_live_event_speech_uses_saved_voice_preference(
    tmp_path: Path, monkeypatch
) -> None:
    from interface import api as api_module

    monkeypatch.setattr(
        api_module,
        "collect_health",
        lambda settings: {
            "gemini": {"available": True, "configured": True, "model": "gemini-2.5-flash", "message": "ok"},
            "ffmpeg": {"available": True, "message": "ok"},
            "microphone": {"available": True, "message": "ok"},
            "database": {"available": True, "message": str(settings.database_path)},
        },
    )
    database = tmp_path / "event-voice.sqlite3"
    settings = AppSettings(
        config_path=tmp_path / "ava.yaml",
        memory={"database_path": str(database)},
        llm={"routes": {"default": {"backend": "template"}}},
        speech={"cache_dir": str(tmp_path / "audio")},
        robot={"mode": "simulated"},
    )
    application = create_app(settings)
    with TestClient(application) as client:
        session_id = client.post("/api/sessions").json()["session_id"]
        created = client.post(
            "/api/events",
            json={
                "session_id": session_id,
                "name": "Malayalam event",
                "schedule": [
                    {
                        "title": "Guest speech",
                        "type": "speech",
                        "participant": "Anjali",
                    }
                ],
            },
        ).json()
        event_id = created["id"]
        item = application.state.event_service.get_event(event_id)["items"][0]
        client.patch(f"/api/live-events/{event_id}", json={"language": "ml"})
        application.state.event_service.save_summary(
            event_id, item["id"], "പ്രധാന ആശയങ്ങൾ പങ്കുവെച്ചു."
        )
        client.put(
            f"/api/preferences/{session_id}",
            json={
                "language": "ml",
                "style": "Professional",
                "voice": "ml-IN-MidhunNeural",
            },
        )
        synthesized: list[tuple[str, str, str | None]] = []

        async def no_audio(
            text: str,
            language: str,
            *,
            voice: str | None = None,
            cache_version: int | None = None,
        ) -> Path:
            _ = cache_version
            synthesized.append((text, language, voice))
            audio_path = tmp_path / "audio" / "event.mp3"
            audio_path.parent.mkdir(parents=True, exist_ok=True)
            audio_path.write_bytes(b"mp3-data")
            return audio_path

        application.state.pipeline.synthesizer.synthesize = no_audio

        summary_response = client.post(
            f"/api/live-events/{event_id}/items/{item['id']}/summary/speak"
        )
        announcement_response = client.post(
            f"/api/live-events/{event_id}/announcement",
            json={"text": "അടുത്ത പരിപാടിയിലേക്ക് സ്വാഗതം.", "polish": False},
        )

        assert summary_response.status_code == 200
        assert announcement_response.status_code == 200
        assert [(language, voice) for _, language, voice in synthesized] == [
            ("ml", "ml-IN-MidhunNeural"),
            ("ml", "ml-IN-MidhunNeural"),
        ]
