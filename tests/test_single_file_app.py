"""Focused tests for the standalone ``python app.py`` application."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

import app as standalone


def test_standalone_event_control_and_summary_filter(
    tmp_path: Path, monkeypatch
) -> None:
    standalone.DATABASE = tmp_path / "single-file.sqlite3"

    async def fake_chat(*, model: str, messages: list[dict[str, str]], stream: bool = False):
        _ = (model, messages)
        if stream:
            async def tokens():
                yield SimpleNamespace(
                    message=SimpleNamespace(
                        content="Welcome everyone. Please welcome Dr. Nair. The music begins."
                    )
                )
            return tokens()
        return SimpleNamespace(
            message=SimpleNamespace(
                content="Dr. Nair will speak later. The speaker described the garden."
            )
        )

    async def no_audio(text: str, language: str) -> str:
        _ = (text, language)
        return "YXVkaW8="

    async def model_list():
        return SimpleNamespace(models=[SimpleNamespace(name=standalone.MODEL)])

    monkeypatch.setattr(standalone.ollama, "chat", fake_chat)
    monkeypatch.setattr(standalone.ollama, "list", model_list)
    monkeypatch.setattr(standalone, "make_audio", no_audio)
    with TestClient(standalone.app) as client:
        assert client.get("/api/health").json()["model_available"] is True
        created = client.post(
            "/api/events",
            json={
                "session_id": "single-test",
                "name": "Arts day",
                "items": [
                    {"title": "Guest address", "person_name": "Dr. Nair"},
                    {"title": "Music", "person_name": "Anu"},
                ],
            },
        ).json()
        first, second = created["items"]
        absent = client.patch(
            f"/api/events/{created['id']}/items/{first['id']}",
            json={"status": "absent"},
        )
        assert absent.status_code == 200
        assert absent.json()["items"][0]["status"] == "absent"

        summary = client.post(
            f"/api/events/{created['id']}/items/{second['id']}/summarize",
            json={"transcript": "The speaker explained a garden project with student volunteers."},
        )
        assert summary.status_code == 200
        assert "Dr. Nair" not in summary.json()["summary"]
        assert "garden" in summary.json()["summary"]

        reordered = client.post(
            f"/api/events/{created['id']}/reorder",
            json={"item_ids": [second["id"], first["id"]]},
        )
        assert [item["id"] for item in reordered.json()["items"]] == [
            second["id"], first["id"]
        ]
        undone = client.post(f"/api/events/{created['id']}/undo")
        assert undone.status_code == 200
        assert [item["id"] for item in undone.json()["items"]] == [
            first["id"], second["id"]
        ]
        called = client.post(f"/api/events/{created['id']}/call-now")
        assert called.status_code == 200
        assert "Dr. Nair" not in called.json()["announcement"]
        assert called.json()["event"]["items"][1]["status"] == "on_stage"
        assert client.post(f"/api/events/{created['id']}/undo").status_code == 200
        assert client.get("/").status_code == 200
        assert "SpeechRecognition" in client.get("/").text
        assert client.get("/control").status_code == 200


def test_standalone_websocket_filters_absent_speaker_sentences(
    tmp_path: Path, monkeypatch
) -> None:
    standalone.DATABASE = tmp_path / "single-websocket.sqlite3"

    async def fake_chat(*, model: str, messages: list[dict[str, str]], stream: bool = False):
        _ = (model, messages)
        if stream:
            async def tokens():
                yield SimpleNamespace(
                    message=SimpleNamespace(
                        content="Welcome everyone. Please welcome Dr. Nair. The program continues."
                    )
                )
            return tokens()
        return SimpleNamespace(message=SimpleNamespace(content=""))

    async def no_audio(text: str, language: str) -> str:
        _ = (text, language)
        return "YXVkaW8="

    monkeypatch.setattr(standalone.ollama, "chat", fake_chat)
    monkeypatch.setattr(standalone, "make_audio", no_audio)
    with TestClient(standalone.app) as client:
        event = client.post(
            "/api/events",
            json={
                "session_id": "socket-test",
                "name": "Arts day",
                "items": [{"title": "Guest address", "person_name": "Dr. Nair"}],
            },
        ).json()
        item_id = event["items"][0]["id"]
        client.patch(
            f"/api/events/{event['id']}/items/{item_id}",
            json={"status": "absent"},
        )
        with client.websocket_connect("/ws?session_id=socket-test") as websocket:
            assert websocket.receive_json()["type"] == "ready"
            websocket.send_text("not-json")
            assert websocket.receive_json()["type"] == "error"
            websocket.send_json(
                {"type": "message", "message": "Introduce the event", "language": "en"}
            )
            messages: list[dict[str, Any]] = []
            while True:
                payload = websocket.receive_json()
                messages.append(payload)
                if payload["type"] in {"complete", "error"}:
                    break
        sentences = [
            payload["text"] for payload in messages if payload["type"] == "sentence"
        ]
        assert sentences == ["Welcome everyone.", "The program continues."]
        assert all(payload["audio"] == "YXVkaW8=" for payload in messages if payload["type"] == "sentence")
        with standalone.connect_db() as connection:
            saved = connection.execute(
                """SELECT role, content FROM single_messages
                   WHERE session_id = ? ORDER BY id""",
                ("socket-test",),
            ).fetchall()
        assert [row["role"] for row in saved] == ["user", "assistant"]
        assert "Dr. Nair" not in saved[-1]["content"]
