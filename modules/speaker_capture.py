"""Background transcription helpers for event speaker capture."""

from __future__ import annotations

from typing import Any


class SpeakerCapture:
    """Run segment transcription off the event loop and append event transcripts."""

    def __init__(self, recognizer: Any, event_service: Any) -> None:
        self.recognizer = recognizer
        self.event_service = event_service

    def transcribe_and_store(
        self, event_id: int, item_id: int, audio_path: str, language: str = "auto"
    ) -> dict[str, Any]:
        """Transcribe one uploaded speech segment and persist the recognized words."""
        if language == "auto":
            result = self.recognizer.transcribe_detect(audio_path)
        else:
            result = self.recognizer.transcribe(audio_path, language)
        event = self.event_service.append_transcript(event_id, item_id, result.text)
        return {"text": result.text, "language": result.language, "event": event}
