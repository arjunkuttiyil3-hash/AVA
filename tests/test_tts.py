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
