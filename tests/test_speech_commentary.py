"""Tests for speech transcription commentary generation API."""

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import create_app
from config.settings import AppSettings


def test_speech_commentary_analyzes_transcript_and_returns_anchor_script(
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
    database = tmp_path / "speech-commentary.sqlite3"
    settings = AppSettings(
        config_path=tmp_path / "ava.yaml",
        memory={"database_path": str(database)},
        llm={"routes": {"default": {"backend": "template"}}},
        speech={"cache_dir": str(tmp_path / "audio")},
        robot={"mode": "simulated"},
    )
    application = create_app(settings)
    transcript = (
        "Our students planted 200 native saplings this year. "
        "We hope to involve every class in caring for them."
    )
    prompts: list[tuple[str, str, str]] = []
    decision = SimpleNamespace(backend="gemini", model="gemini-test")

    def fake_stream(
        task: str, language: str, messages: list[dict[str, str]]
    ) -> tuple[list[str], SimpleNamespace]:
        prompts.append((task, language, messages[-1]["content"]))
        return [
            '{"key_points":["Students planted 200 native saplings this year.",'
            '"The speaker hopes every class will help care for them."],'
            '"commentary_script":"That is a wonderful effort by the students. '
            'We look forward to seeing the whole school care for these saplings."}'
        ], decision

    with TestClient(application) as client:
        session_id = client.post("/api/sessions").json()["session_id"]
        event = client.post(
            "/api/events",
            json={
                "session_id": session_id,
                "name": "Green campus day",
                "venue": "Main hall",
                "schedule": [{"title": "Student address", "type": "speech"}],
            },
        ).json()
        application.state.pipeline.router.stream = fake_stream

        response = client.post(
            "/api/anchoring/speech-commentary",
            json={"event_id": event["id"], "transcript": transcript},
        )

    assert response.status_code == 200
    result = response.json()
    assert result["key_points"] == [
        "Students planted 200 native saplings this year.",
        "The speaker hopes every class will help care for them.",
    ]
    assert "wonderful effort" in result["commentary_script"]
    assert result["language"] == "en"
    assert prompts[0][0:2] == ("anchoring_script", "en")
    assert transcript in prompts[0][2]
    assert "Green campus day" in prompts[0][2]


def test_speech_commentary_rejects_short_transcript(tmp_path: Path, monkeypatch) -> None:
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
    database = tmp_path / "short-speech.sqlite3"
    settings = AppSettings(
        config_path=tmp_path / "ava.yaml",
        memory={"database_path": str(database)},
        llm={"routes": {"default": {"backend": "template"}}},
        speech={"cache_dir": str(tmp_path / "audio")},
        robot={"mode": "simulated"},
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/anchoring/speech-commentary",
            json={"event_id": 1, "transcript": "hi"},
        )
    assert response.status_code == 422


def test_anchoring_speech_transcription_accepts_recorded_audio(
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
    database = tmp_path / "speech-transcription.sqlite3"
    settings = AppSettings(
        config_path=tmp_path / "ava.yaml",
        memory={"database_path": str(database)},
        llm={"routes": {"default": {"backend": "template"}}},
        speech={"cache_dir": str(tmp_path / "audio")},
        robot={"mode": "simulated"},
    )
    application = create_app(settings)

    with TestClient(application) as client:
        application.state.recognizer.transcribe = lambda path, language: SimpleNamespace(
            text="We are celebrating the work of our students.", language="en"
        )
        response = client.post(
            "/api/transcribe",
            files={"audio": ("anchoring-speech.webm", b"recorded audio", "audio/webm")},
            data={"language": "en"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "text": "We are celebrating the work of our students.",
        "language": "en",
    }
