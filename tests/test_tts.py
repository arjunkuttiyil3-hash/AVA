"""Tests for spoken text cleanup and sentence splitting."""

from modules.tts import SentenceBuffer, clean_tts_text
from modules.tts import SpeechSynthesizer


def test_cleaner_removes_markdown_emoji_and_preserves_malayalam() -> None:
    cleaned = clean_tts_text("**നമസ്കാരം!** 😀 [AVA](https://example.test)")
    assert cleaned == "നമസ്കാരം! AVA"
    assert "**" not in cleaned
    assert "😀" not in cleaned


def test_sentence_buffer_emits_complete_sentences_and_flushes() -> None:
    buffer = SentenceBuffer()
    assert buffer.push("Hello there. Here is") == ["Hello there."]
    assert buffer.push(" another sentence!") == ["Here is another sentence!"]
    assert buffer.flush() == ""


def test_edge_tts_failure_falls_back_and_exposes_notice(tmp_path, monkeypatch) -> None:
    import sys
    import types

    class BrokenCommunicator:
        def __init__(self, *args, **kwargs) -> None:
            _ = (args, kwargs)

        async def save(self, output_path: str) -> None:
            _ = output_path
            raise ConnectionError("edge service offline")

    monkeypatch.setitem(
        sys.modules,
        "edge_tts",
        types.SimpleNamespace(Communicate=BrokenCommunicator),
    )
    synthesizer = SpeechSynthesizer(cache_dir=tmp_path, provider="edge")

    def offline_fallback(text: str, language: str, output_path) -> None:
        _ = (text, language)
        output_path.write_bytes(b"offline-audio")

    monkeypatch.setattr(synthesizer, "_synthesize_offline", offline_fallback)

    async def synthesize():
        return await synthesizer.synthesize("Hello AVA.", "en")

    output = __import__("asyncio").run(synthesize())

    assert output.is_file()
    assert synthesizer.get_status(output)
    assert "offline speech" in synthesizer.get_status(output)


def test_synthesis_uses_a_voice_matching_the_requested_language(tmp_path, monkeypatch) -> None:
    import asyncio
    import sys
    import types

    requested_voices: list[str] = []

    class RecordingCommunicator:
        def __init__(self, _text, voice: str, **_kwargs) -> None:
            requested_voices.append(voice)

        async def save(self, output_path: str) -> None:
            from pathlib import Path

            Path(output_path).write_bytes(b"audio")

    monkeypatch.setitem(
        sys.modules,
        "edge_tts",
        types.SimpleNamespace(Communicate=RecordingCommunicator),
    )
    synthesizer = SpeechSynthesizer(cache_dir=tmp_path)

    async def synthesize() -> None:
        await synthesizer.synthesize(
            "English announcement.", "en", voice="ml-IN-SobhanaNeural"
        )
        await synthesizer.synthesize(
            "മലയാളം അറിയിപ്പ്.", "ml", voice="en-IN-NeerjaNeural"
        )

    asyncio.run(synthesize())

    assert requested_voices == ["en-IN-NeerjaNeural", "ml-IN-SobhanaNeural"]


def test_offline_voice_selection_requires_english_locale() -> None:
    from types import SimpleNamespace

    english_voice = SimpleNamespace(
        id="english", name="English voice", languages=[b"\x05en-US"]
    )
    hindi_voice = SimpleNamespace(
        id="hindi", name="Hindi voice", languages=[b"\x05hi-IN"]
    )

    assert SpeechSynthesizer._voice_matches_language(english_voice, "en")
    assert not SpeechSynthesizer._voice_matches_language(hindi_voice, "en")
