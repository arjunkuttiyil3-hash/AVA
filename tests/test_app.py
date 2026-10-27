"""FastAPI integration tests without a live model, microphone, or robot."""

import base64
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import create_app
from config.settings import AppSettings


def test_api_serves_ui_session_and_event_state(tmp_path: Path, monkeypatch) -> None:
    from interface import api as api_module

    async def no_gemini_generation():
        return {
            "available": False,
            "key_set": False,
            "model": "gemini-3.5-flash",
            "fallback_model": "gemini-3.6-flash",
            "message": "Gemini API key missing",
        }

    async def no_tts_generation(
        self, text: str, language: str, *, cache_version: int | str | None = None
    ) -> Path:
        _ = (self, text, language, cache_version)
        audio_path = tmp_path / "audio" / "health.mp3"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"test-audio")
        return audio_path

    monkeypatch.setattr(api_module, "gemini_health_check", no_gemini_generation)
    monkeypatch.setattr(api_module.SpeechSynthesizer, "synthesize", no_tts_generation)

    monkeypatch.setattr(
        api_module,
        "collect_health",
        lambda settings: {
            "gemini": {
                "available": False,
                "configured": False,
                "model": "gemini-3.5-flash",
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
        assert status["checks"]["gemini"]["key_set"] is False
        assert status["checks"]["tts"]["available"] is True
        home = client.get("/").text
        assert "Your voice on stage" in home
        assert "Anchoring Studio" in home
        assert "Generate script" in home
        assert "gemini-3.5-flash" in home
        assert "Live event control" in client.get("/control").text
        assert client.get("/static/.env").status_code == 404
        assert client.get("/static/css/ava.css").status_code == 200
        session_id = client.post("/api/sessions").json()["session_id"]

        event = client.post(
            "/api/events",
            json={
                "session_id": session_id,
                "name": "Arts day",
                "venue": "Main hall",
                "event_start_time": "09:30",
                "event_end_time": "12:00",
                "chief_guest": "Dr. Nair",
                "chief_guest_title": "Principal",
                "schedule": [
                    {
                        "time": "11:00",
                        "title": "Dance",
                        "participant": "Maya",
                        "designation": "Dance club lead",
                        "participant_details": "Classical dance performance",
                        "location": "Main stage",
                        "duration_minutes": 12,
                        "performance_style": "ceremonial",
                        "type": "performance",
                    },
                    {"time": "10:00", "title": "Welcome", "participant": "Anjali"},
                ],
            },
        )

        assert event.status_code == 200
        assert [item["time"] for item in event.json()["schedule"]] == ["11:00", "10:00"]
        state = client.get(f"/api/memory/{session_id}").json()
        assert state["event"]["name"] == "Arts day"
        assert state["message_count"] == 0
        live_event = client.get(f"/api/live-events/{event.json()['id']}").json()
        assert live_event["items"][0]["title"] == "Dance"
        assert live_event["items"][0]["order_index"] == 0
        assert live_event["event_start_time"] == "09:30"
        assert live_event["event_end_time"] == "12:00"
        assert live_event["chief_guest_title"] == "Principal"
        assert live_event["items"][0]["scheduled_time"] == "11:00"
        assert live_event["items"][0]["person_name"] == "Maya"
        assert live_event["items"][0]["designation"] == "Dance club lead"
        assert live_event["items"][0]["participant_details"] == "Classical dance performance"
        assert live_event["items"][0]["location"] == "Main stage"
        assert live_event["items"][0]["planned_duration"] == 12
        assert live_event["items"][0]["performance_style"] == "ceremonial"
        next_item = client.post(f"/api/events/{event.json()['id']}/next").json()
        assert next_item["current"]["title"] == "Welcome"
        assert next_item["current"]["order_index"] == 1


def test_websocket_streams_template_response(tmp_path: Path, monkeypatch) -> None:
    from interface import api as api_module

    monkeypatch.setattr(
        api_module,
        "collect_health",
        lambda settings: {
            "gemini": {"available": True, "configured": True, "model": "gemini-3.5-flash", "message": "ok"},
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
            websocket.send_json({"type": "stop"})
            assert websocket.receive_json()["type"] == "speech_stop"
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
        assert audio_event["mime_type"] == "audio/mpeg"
        assert base64.b64decode(audio_event["audio_base64"]) == b"mp3-data"
        assert application.state.pipeline.repository.get_recent_messages(session_id, 2)[-1]["role"] == "assistant"


def test_live_event_updates_broadcast_to_two_websocket_clients(
    tmp_path: Path, monkeypatch
) -> None:
    from interface import api as api_module

    monkeypatch.setattr(
        api_module,
        "collect_health",
        lambda settings: {
            "gemini": {"available": True, "configured": True, "model": "gemini-3.5-flash", "message": "ok"},
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
            settings_url = f"/api/event/settings/{session_id}"
            assert client.get(settings_url).json() == {"auto_speak_summary": True}
            assert client.put(settings_url, json={"auto_speak_summary": False}).json() == {
                "auto_speak_summary": False
            }
            assert client.get(settings_url).json() == {"auto_speak_summary": False}
            for websocket in (control, main):
                update = websocket.receive_json()
                assert update["type"] == "event_state_changed"
                assert update["state_version"] == response.json()["state_version"]
                assert update["event"]["items"][0]["status"] == "absent"


def test_chat_absence_command_updates_and_broadcasts_to_open_pages(
    tmp_path: Path, monkeypatch
) -> None:
    from interface import api as api_module

    monkeypatch.setattr(
        api_module,
        "collect_health",
        lambda settings: {
            "gemini": {"available": True, "configured": True, "model": "test", "message": "ok"},
            "ffmpeg": {"available": True, "message": "ok"},
            "microphone": {"available": True, "message": "ok"},
            "database": {"available": True, "message": str(settings.database_path)},
        },
    )
    database = tmp_path / "command-broadcast.sqlite3"
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
                "name": "Arts day",
                "schedule": [
                    {"title": "Opening", "type": "welcome"},
                    {
                        "title": "Guest address",
                        "type": "speech",
                        "participant": "Dr. Nair",
                    },
                    {
                        "title": "Second address",
                        "type": "speech",
                        "participant": "Dr. Nair",
                    },
                ],
            },
        ).json()
        event_id = created["id"]
        with client.websocket_connect(f"/ws/{session_id}") as main_page, client.websocket_connect(
            f"/ws/{session_id}"
        ) as control_page:
            assert main_page.receive_json()["type"] == "event_state_changed"
            assert control_page.receive_json()["type"] == "event_state_changed"

            proposal_response = client.post(
                f"/api/live-events/{event_id}/parse-command",
                json={"message": "Dr. Nair has not arrived, remove him"},
            )
            assert proposal_response.status_code == 200
            proposal = proposal_response.json()["proposal"]
            assert proposal["person_name"] == "Dr. Nair"
            assert proposal["action"] == "set_absent"

            applied = client.patch(
                f"/api/event/items/{proposal['item_id']}",
                json={"event_id": event_id, "status": "absent"},
            )
            assert applied.status_code == 200
            assert [
                item["status"]
                for item in applied.json()["items"]
                if item["person_name"] == "Dr. Nair"
            ] == ["absent", "absent"]
            for websocket in (main_page, control_page):
                update = websocket.receive_json()
                assert update["type"] == "event_state_changed"
                assert update["state_version"] == applied.json()["state_version"]
                assert all(
                    item["status"] == "absent"
                    for item in update["event"]["items"]
                    if item["person_name"] == "Dr. Nair"
                )

            undone = client.post(f"/api/live-events/{event_id}/undo")
            assert undone.status_code == 200
            for websocket in (main_page, control_page):
                update = websocket.receive_json()
                assert update["type"] == "event_state_changed"
                assert update["state_version"] == undone.json()["state_version"]
                assert all(
                    item["status"] == "pending"
                    for item in update["event"]["items"]
                    if item["person_name"] == "Dr. Nair"
                )


def test_live_event_speech_uses_saved_voice_preference(
    tmp_path: Path, monkeypatch
) -> None:
    from interface import api as api_module

    monkeypatch.setattr(
        api_module,
        "collect_health",
        lambda settings: {
            "gemini": {"available": True, "configured": True, "model": "gemini-3.5-flash", "message": "ok"},
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


def test_control_schedule_crud_and_reorder_routes(tmp_path: Path, monkeypatch) -> None:
    from interface import api as api_module

    monkeypatch.setattr(
        api_module,
        "collect_health",
        lambda settings: {
            "gemini": {"available": False, "configured": False, "message": "not configured"},
            "ffmpeg": {"available": True, "message": "ok"},
            "microphone": {"available": True, "message": "ok"},
            "database": {"available": True, "message": str(settings.database_path)},
        },
    )
    database = tmp_path / "control-routes.sqlite3"
    settings = AppSettings(
        config_path=tmp_path / "ava.yaml",
        memory={"database_path": str(database)},
        llm={"routes": {"default": {"backend": "template"}}},
        speech={"cache_dir": str(tmp_path / "audio")},
        robot={"mode": "simulated"},
    )

    with TestClient(create_app(settings)) as client:
        session_id = client.post("/api/sessions").json()["session_id"]
        created = client.post(
            "/api/events",
            json={
                "session_id": session_id,
                "name": "Schedule CRUD",
                "schedule": [
                    {"title": "Opening", "type": "welcome"},
                    {"title": "Guest address", "type": "speech", "participant": "Dr. Nair"},
                ],
            },
        )
        assert created.status_code == 200
        event_id = created.json()["id"]
        live = client.get(f"/api/live-events/{event_id}").json()
        first_id, second_id = [item["id"] for item in live["items"]]

        added = client.post(
            "/api/event/items",
            json={
                "event_id": event_id,
                "position": 1,
                "title": "Late arrival",
                "type": "speech",
                "person_name": "Dr. Rao",
            },
        )
        assert added.status_code == 200
        assert [item["title"] for item in added.json()["items"]] == [
            "Opening", "Late arrival", "Guest address"
        ]
        added_item = added.json()["items"][1]

        edited = client.patch(
            f"/api/event/items/{added_item['id']}",
            json={
                "event_id": event_id,
                "title": "Updated late arrival",
                "designation": "Guest speaker",
            },
        )
        assert edited.status_code == 200
        assert edited.json()["items"][1]["title"] == "Updated late arrival"
        assert edited.json()["items"][1]["designation"] == "Guest speaker"

        reordered = client.post(
            "/api/event/reorder",
            json={
                "event_id": event_id,
                "item_ids": [second_id, added_item["id"], first_id],
            },
        )
        assert reordered.status_code == 200
        assert [item["id"] for item in reordered.json()["items"]] == [
            second_id, added_item["id"], first_id
        ]

        deleted = client.delete(
            f"/api/event/items/{added_item['id']}?event_id={event_id}"
        )
        assert deleted.status_code == 200
        assert next(
            item for item in deleted.json()["items"] if item["id"] == added_item["id"]
        )["status"] == "removed"
        refreshed = client.get(f"/api/live-events/{event_id}").json()
        assert refreshed["state_version"] == deleted.json()["state_version"]
        assert refreshed["items"][1]["title"] == "Updated late arrival"

        restored = client.post(
            f"/api/live-events/{event_id}/items/{added_item['id']}/restore",
            json={"position": 1},
        )
        assert restored.status_code == 200
        assert restored.json()["items"][1]["status"] == "pending"
        skipped = client.post(
            f"/api/live-events/{event_id}/items/{added_item['id']}/skip",
            json={"move_to_end": True},
        )
        assert skipped.status_code == 200
        assert skipped.json()["items"][-1]["id"] == added_item["id"]
        assert skipped.json()["items"][-1]["status"] == "skipped"

        started = client.post(
            f"/api/live-events/{event_id}/start",
            json={"defer_first_item": True},
        )
        assert started.status_code == 200
        assert started.json()["status"] == "running"
        assert started.json()["current_item_id"] is None

        ended = client.post(f"/api/live-events/{event_id}/end")
        assert ended.status_code == 200
        assert "Thank you for joining us" in ended.json()["closing_script"]
        post_end_edit = client.post(
            "/api/event/items", json={"event_id": event_id, "title": "Post-event edit"}
        )
        assert post_end_edit.status_code == 200
        assert post_end_edit.json()["status"] == "ended"
        restarted = client.post(f"/api/live-events/{event_id}/start")
        assert restarted.status_code == 200
        assert restarted.json()["status"] == "running"
        assert restarted.json()["current_item_id"] == post_end_edit.json()["items"][0]["id"]


def test_absent_item_announcement_and_saved_script_audio(tmp_path: Path, monkeypatch) -> None:
    from interface import api as api_module

    monkeypatch.setattr(
        api_module,
        "collect_health",
        lambda settings: {
            "gemini": {"available": False, "configured": False, "message": "not configured"},
            "ffmpeg": {"available": True, "message": "ok"},
            "microphone": {"available": True, "message": "ok"},
            "database": {"available": True, "message": str(settings.database_path)},
        },
    )
    settings = AppSettings(
        config_path=tmp_path / "ava.yaml",
        memory={"database_path": str(tmp_path / "audio-routes.sqlite3")},
        llm={"routes": {"default": {"backend": "template"}}},
        speech={"cache_dir": str(tmp_path / "audio")},
        robot={"mode": "simulated"},
    )
    application = create_app(settings)

    async def no_audio(
        text: str,
        language: str,
        *,
        voice: str | None = None,
        cache_version: int | str | None = None,
    ) -> Path:
        _ = (text, language, voice, cache_version)
        path = tmp_path / "audio" / "test-script.mp3"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test-audio")
        return path

    with TestClient(application) as client:
        application.state.pipeline.synthesizer.synthesize = no_audio
        session_id = client.post("/api/sessions").json()["session_id"]
        event = client.post(
            "/api/events",
            json={
                "session_id": session_id,
                "name": "Community program",
                "schedule": [{
                    "title": "Guest speech",
                    "type": "speech",
                    "person_name": "Asha Nair",
                }],
            },
        ).json()
        event_id = event["id"]
        item = application.state.event_service.get_event(event_id)["items"][0]
        application.state.event_service.save_script(
            event_id,
            [
                {
                    "id": f"intro-{item['id']}",
                    "type": "intro",
                    "itemId": item["id"],
                    "text": "Please welcome Asha Nair.",
                    "estimatedSeconds": 4,
                    "edited": False,
                },
                {
                    "id": "opening",
                    "type": "opening",
                    "itemId": None,
                    "text": "Welcome to the community program.",
                    "estimatedSeconds": 4,
                    "edited": False,
                },
            ],
        )
        application.state.event_service.update_item(
            event_id, item["id"], {"status": "absent"}
        )

        notice = client.post(
            f"/api/live-events/{event_id}/items/{item['id']}/absence-announcement"
        )
        assert notice.status_code == 200
        assert "Asha Nair is unable to join us" in notice.json()["text"]
        assert "cancelled" in notice.json()["text"]
        assert notice.json()["url"].startswith("/api/audio/")
        saved = application.state.event_service.get_script(event_id)
        absence = next(
            segment for segment in saved["segments"]
            if segment["id"] == f"absence-{item['id']}"
        )
        assert absence["text"] == notice.json()["text"]
        assert absence["type"] == "absence"

        blocked_script = client.get(
            f"/api/live-events/{event_id}/scripts/intro-{item['id']}/audio"
        )
        assert blocked_script.status_code == 409
        absence_audio = client.get(
            f"/api/live-events/{event_id}/scripts/absence-{item['id']}/audio"
        )
        assert absence_audio.status_code == 200
        opening = client.get(
            f"/api/live-events/{event_id}/scripts/opening/audio"
        )
        assert opening.status_code == 200
        assert opening.json()["url"].startswith("/api/audio/")


def test_next_and_call_now_speak_live_schedule_with_template_on_llm_failure(
    tmp_path: Path, monkeypatch
) -> None:
    from interface import api as api_module

    monkeypatch.setattr(
        api_module,
        "collect_health",
        lambda settings: {
            "gemini": {"available": False, "configured": False, "message": "not configured"},
            "ffmpeg": {"available": True, "message": "ok"},
            "microphone": {"available": True, "message": "ok"},
            "database": {"available": True, "message": str(settings.database_path)},
        },
    )
    database = tmp_path / "call-now.sqlite3"
    settings = AppSettings(
        config_path=tmp_path / "ava.yaml",
        memory={"database_path": str(database)},
        llm={"routes": {"default": {"backend": "template"}}},
        speech={"cache_dir": str(tmp_path / "audio")},
        robot={"mode": "simulated"},
    )
    application = create_app(settings)

    async def no_audio(
        text: str,
        language: str,
        *,
        voice: str | None = None,
        cache_version: int | str | None = None,
    ) -> Path:
        _ = (text, language, voice, cache_version)
        audio_path = tmp_path / "audio" / "call-now.mp3"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"mp3-data")
        return audio_path

    with TestClient(application) as client:
        application.state.pipeline.synthesizer.synthesize = no_audio
        session_id = client.post("/api/sessions").json()["session_id"]
        created = client.post(
            "/api/events",
            json={
                "session_id": session_id,
                "name": "Arts day",
                "schedule": [
                    {"title": "Guest address", "type": "speech", "participant": "Dr. Nair"},
                    {"title": "Student address", "type": "speech", "participant": "Anu"},
                    {"title": "Music performance", "type": "performance"},
                ],
            },
        ).json()
        event_id = created["id"]
        items = application.state.event_service.get_event(event_id)["items"]

        def gemini_failure(*args, **kwargs):
            _ = (args, kwargs)
            raise RuntimeError("Gemini unavailable")

        application.state.event_service.save_summary(
            event_id,
            items[0]["id"],
            "The school garden project helps students learn. The speaker thanked volunteers.",
        )
        application.state.pipeline.router.stream = gemini_failure

        next_response = client.post(f"/api/live-events/{event_id}/call-now", json={})
        assert next_response.status_code == 200
        next_result = next_response.json()
        assert next_result["used_fallback"] is True
        assert "Thank you, Dr. Nair" in next_result["text"]
        assert "sharing insights on The school garden project helps students learn" in next_result["text"]
        assert "Student address" in next_result["text"]
        assert next_result["event"]["current_item_id"] == items[1]["id"]
        assert next_result["audio_url"].startswith("/api/audio/")

        call_response = client.post(
            f"/api/live-events/{event_id}/call-now",
            json={"item_id": items[2]["id"], "include_summary": True},
        )
        assert call_response.status_code == 200
        call_result = call_response.json()
        assert call_result["used_fallback"] is True
        assert "Thank you, Anu" in call_result["text"]
        assert "Music performance" in call_result["text"]
        assert call_result["event"]["current_item_id"] == items[2]["id"]

        fourth_item = application.state.event_service.add_item(
            event_id, {"title": "Closing performance", "type": "performance"}
        )["items"][-1]

        def wrong_language_announcement(*args, **kwargs):
            _ = (args, kwargs)
            return ["यह घोषणा हिन्दी में है।"], SimpleNamespace(
                backend="gemini", model="gemini-test"
            )

        application.state.pipeline.router.stream = wrong_language_announcement
        wrong_language_response = client.post(
            f"/api/live-events/{event_id}/call-now",
            json={"item_id": fourth_item["id"]},
        )
        assert wrong_language_response.status_code == 200
        assert wrong_language_response.json()["used_fallback"] is True
        assert wrong_language_response.json()["text"] == (
            "We now move to Closing performance."
        )
        assert "did not match the event language" in wrong_language_response.json()["notice"]


def test_finished_speaker_summary_is_saved_and_used_in_next_transition(
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
    database = tmp_path / "speaker-summary.sqlite3"
    settings = AppSettings(
        config_path=tmp_path / "ava.yaml",
        memory={"database_path": str(database)},
        llm={"routes": {"default": {"backend": "template"}}},
        speech={"cache_dir": str(tmp_path / "audio")},
        robot={"mode": "simulated"},
    )
    application = create_app(settings)
    transcript = (
        "The speaker described the school garden project. "
        "Students will plant native seeds next week."
    )
    summary = (
        "Anjali described the school garden project. "
        "She thanked students for preparing native seeds."
    )
    prompts: list[tuple[str, str]] = []
    decision = SimpleNamespace(backend="gemini", model="gemini-3.5-flash")

    def fake_stream(
        task: str, language: str, messages: list[dict[str, str]]
    ) -> tuple[list[str], SimpleNamespace]:
        _ = language
        prompts.append((task, messages[-1]["content"]))
        if task == "speaker_summary":
            return [summary], decision
        return [
            "Thank you, Anjali, for sharing those insights. We now move to the student performance."
        ], decision

    async def no_audio(
        text: str,
        language: str,
        *,
        voice: str | None = None,
        cache_version: int | str | None = None,
    ) -> Path:
        _ = (text, language, voice, cache_version)
        audio = tmp_path / "audio" / "transition.mp3"
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(b"mp3-data")
        return audio

    with TestClient(application) as client:
        application.state.pipeline.router.stream = fake_stream
        application.state.pipeline.synthesizer.synthesize = no_audio
        session_id = client.post("/api/sessions").json()["session_id"]
        created = client.post(
            "/api/events",
            json={
                "session_id": session_id,
                "name": "School day",
                "schedule": [
                    {"title": "Guest address", "type": "speech", "participant": "Anjali"},
                    {"title": "Student performance", "type": "performance"},
                ],
            },
        ).json()
        event_id = created["id"]
        items = application.state.event_service.get_event(event_id)["items"]
        application.state.event_service.advance(event_id, item_id=items[0]["id"])

        transcript_response = client.put(
            f"/api/live-events/{event_id}/items/{items[0]['id']}/transcript",
            json={"text": transcript},
        )
        finish_response = client.post(
            f"/api/live-events/{event_id}/items/{items[0]['id']}/finish"
        )
        transition_response = client.post(
            f"/api/live-events/{event_id}/call-now", json={}
        )

    assert transcript_response.status_code == 200
    assert finish_response.status_code == 200
    assert finish_response.json()["summary"] == summary
    finished_item = next(
        item
        for item in finish_response.json()["event"]["items"]
        if item["id"] == items[0]["id"]
    )
    assert finished_item["transcript"] == transcript
    assert finished_item["summary"] == summary
    assert finished_item["status"] == "done"
    assert transition_response.status_code == 200
    transition_prompt = next(
        prompt for task, prompt in prompts if task == "anchoring_script"
    )
    assert summary in transition_prompt
    assert transition_response.json()["text"].startswith("Thank you, Anjali")


def test_talk_items_can_be_summarized_but_other_types_cannot(
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
    settings = AppSettings(
        config_path=tmp_path / "ava.yaml",
        memory={"database_path": str(tmp_path / "talk-summary.sqlite3")},
        llm={"routes": {"default": {"backend": "template"}}},
        speech={"cache_dir": str(tmp_path / "audio")},
        robot={"mode": "simulated"},
    )
    application = create_app(settings)
    summary = "Asha described the community garden. She thanked volunteers for their support."
    decision = SimpleNamespace(backend="template", model="test")

    def fake_stream(
        task: str, language: str, messages: list[dict[str, str]]
    ) -> tuple[list[str], SimpleNamespace]:
        _ = (language, messages)
        assert task == "speaker_summary"
        return [summary], decision

    with TestClient(application) as client:
        application.state.pipeline.router.stream = fake_stream
        session_id = client.post("/api/sessions").json()["session_id"]
        event = client.post(
            "/api/events",
            json={
                "session_id": session_id,
                "name": "Community event",
                "schedule": [
                    {"title": "Community talk", "type": "talk", "participant": "Asha"},
                    {"title": "Music", "type": "performance"},
                ],
            },
        ).json()
        talk, performance = application.state.event_service.get_event(event["id"])["items"]

        transcript_response = client.put(
            f"/api/live-events/{event['id']}/items/{talk['id']}/transcript",
            json={
                "text": "Asha described the community garden and thanked volunteers for their support."
            },
        )
        talk_response = client.post(
            f"/api/live-events/{event['id']}/items/{talk['id']}/finish"
        )
        performance_response = client.post(
            f"/api/live-events/{event['id']}/items/{performance['id']}/finish"
        )
        capture_response = client.post(
            f"/api/live-events/{event['id']}/items/{performance['id']}/capture",
            files={"audio": ("sample.webm", b"audio", "audio/webm")},
            data={"language": "auto"},
        )

    assert transcript_response.status_code == 200
    assert talk_response.status_code == 200
    assert talk_response.json()["summary"] == summary
    assert performance_response.status_code == 422
    assert capture_response.status_code == 422


def test_structured_script_generation_protects_hand_edited_segments(
    tmp_path: Path, monkeypatch
) -> None:
    import json

    from interface import api as api_module

    monkeypatch.setattr(
        api_module,
        "collect_health",
        lambda settings: {
            "gemini": {
                "available": False,
                "configured": False,
                "model": "gemini-test",
                "message": "Gemini API key missing",
            },
            "ffmpeg": {"available": True, "message": "ok"},
            "microphone": {"available": True, "message": "ok"},
            "database": {"available": True, "message": str(settings.database_path)},
        },
    )
    database = tmp_path / "structured-script.sqlite3"
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
                "name": "Arts night",
                "event_type": "Cultural program",
                "venue": "Main hall",
                "host": "Arts department",
                "sponsors": ["Community Arts Fund"],
                "audience": "Students and families",
                "style": "Warm and confident",
                "special_instructions": "Keep introductions concise.",
                "event_date": "2026-11-01",
                "event_start_time": "17:00",
                "event_end_time": "20:00",
                "tone": "Friendly",
                "schedule": [{
                    "title": "Welcome address",
                    "type": "speech",
                    "participant": "Anu Nair",
                    "participant_details": "Department chair",
                }],
            },
        )
        assert created.status_code == 200, created.text
        event_id = created.json()["id"]
        item_id = client.get(f"/api/live-events/{event_id}").json()["items"][0]["id"]

        def fake_stream(_intent, _language, _messages):
            segments = [
                {
                    "id": "opening", "type": "opening", "itemId": None,
                    "text": "Welcome to Arts night.", "estimatedSeconds": 8,
                },
                {
                    "id": f"intro-{item_id}", "type": "intro", "itemId": item_id,
                    "text": "Please welcome Anu Nair.", "estimatedSeconds": 7,
                },
                {
                    "id": "closing", "type": "closing", "itemId": None,
                    "text": "Thank you for joining us.", "estimatedSeconds": 6,
                },
            ]
            return iter([json.dumps({"segments": segments})]), SimpleNamespace(
                backend="gemini", model="gemini-test"
            )

        application.state.pipeline.router.stream = fake_stream
        generated = client.post(
            "/api/anchoring/scripts/generate", json={"event_id": event_id}
        )
        assert generated.status_code == 200, generated.text
        assert [segment["type"] for segment in generated.json()["segments"]] == [
            "opening", "intro", "closing",
        ]
        assert generated.json()["segments"][1]["itemId"] == item_id

        edited = [dict(segment) for segment in generated.json()["segments"]]
        edited[1]["text"] = "Hand-edited introduction."
        edited[1]["edited"] = True
        saved = client.put(
            f"/api/anchoring/scripts/{event_id}", json={"segments": edited}
        )
        assert saved.status_code == 200, saved.text
        protected = client.post(
            "/api/anchoring/scripts/generate",
            json={
                "event_id": event_id,
                "targets": [{
                    "id": f"intro-{item_id}", "type": "intro", "itemId": item_id,
                }],
            },
        )
        assert protected.status_code == 200, protected.text
        assert protected.json()["protected_ids"] == [f"intro-{item_id}"]
        intro = next(
            segment for segment in protected.json()["segments"]
            if segment["id"] == f"intro-{item_id}"
        )
        assert intro["text"] == "Hand-edited introduction."
        undone = client.post(f"/api/anchoring/scripts/{event_id}/undo")
        assert undone.status_code == 200
        assert next(
            segment for segment in undone.json()["segments"]
            if segment["id"] == f"intro-{item_id}"
        )["text"] == "Please welcome Anu Nair."
        absent = client.patch(
            f"/api/event/items/{item_id}",
            json={"event_id": event_id, "status": "absent"},
        )
        assert absent.status_code == 200, absent.text
        pruned = client.post(
            "/api/anchoring/scripts/generate",
            json={"event_id": event_id, "targets": []},
        )
        assert pruned.status_code == 200, pruned.text
        assert [segment["id"] for segment in pruned.json()["segments"]] == [
            "opening", "closing",
        ]
        closing = client.post(
            f"/api/live-events/{event_id}/close", json={"language": "en"}
        )
        assert closing.status_code == 200, closing.text
        assert closing.json()["summary_skipped"] is True
        assert "summary was skipped" in closing.json()["closing"]["text"]
        assert closing.json()["event"]["status"] == "not_started"
        completed = client.post(f"/api/live-events/{event_id}/end")
        assert completed.status_code == 200
        assert completed.json()["status"] == "ended"

        updated_event = client.patch(
            f"/api/live-events/{event_id}", json={"name": "Arts night revisited"}
        )
        assert updated_event.status_code == 200, updated_event.text
        assert updated_event.json()["status"] == "ended"
        updated_item = client.patch(
            f"/api/event/items/{item_id}",
            json={"event_id": event_id, "title": "Revised welcome address"},
        )
        assert updated_item.status_code == 200, updated_item.text

        def ended_event_stream(_intent, _language, _messages):
            return iter([json.dumps({"segments": [
                {
                    "id": "opening", "type": "opening", "itemId": None,
                    "text": "Welcome back to Arts night.", "estimatedSeconds": 8,
                },
                {
                    "id": "closing", "type": "closing", "itemId": None,
                    "text": "Thank you for joining us again.", "estimatedSeconds": 6,
                },
            ]})]), SimpleNamespace(backend="gemini", model="gemini-test")

        application.state.pipeline.router.stream = ended_event_stream
        regenerated = client.post(
            "/api/anchoring/scripts/generate", json={"event_id": event_id}
        )
        assert regenerated.status_code == 200, regenerated.text
        assert "Welcome back" in regenerated.json()["segments"][0]["text"]

        def template_after_rate_limit(_intent, _language, _messages):
            return iter(["template text"]), SimpleNamespace(
                backend="template",
                model="template",
                failure_status=429,
                failure_detail=(
                    "Gemini API rate limit or quota exceeded (HTTP 429). "
                    "Provider detail: RESOURCE_EXHAUSTED"
                ),
            )

        application.state.pipeline.router.stream = template_after_rate_limit
        rate_limited = client.post(
            "/api/anchoring/scripts/generate", json={"event_id": event_id}
        )
        assert rate_limited.status_code == 503
        assert "RESOURCE_EXHAUSTED" in rate_limited.json()["detail"]

        def wrong_language_script(_intent, _language, _messages):
            return iter([json.dumps({"segments": [{
                "id": "opening",
                "type": "opening",
                "itemId": None,
                "text": "यह उद्घाटन हिन्दी में है।",
                "estimatedSeconds": 8,
            }]})]), SimpleNamespace(backend="gemini", model="gemini-test")

        application.state.pipeline.router.stream = wrong_language_script
        wrong_language_script_response = client.post(
            "/api/anchoring/scripts/generate",
            json={
                "event_id": event_id,
                "targets": [{"id": "opening", "type": "opening", "itemId": None}],
            },
        )
        assert wrong_language_script_response.status_code == 502
        assert "does not match the event language" in wrong_language_script_response.json()["detail"]
