"""Multilingual streaming TTS, text cleanup, and content-addressed audio cache."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import tempfile
import os
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
MARKDOWN_RE = re.compile(r"[*_`#~>|]+")
EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0000FE0F"
    "\U0000200D"
    "]",
    flags=re.UNICODE,
)
WHITESPACE_RE = re.compile(r"\s+")
SENTENCE_RE = re.compile(r"(?<=[.!?।])(?:\s+|$)")


def clean_tts_text(text: str) -> str:
    """Remove markdown and pictographs while preserving Malayalam and punctuation."""
    cleaned = MARKDOWN_LINK_RE.sub(r"\1", text)
    cleaned = EMOJI_RE.sub("", cleaned)
    cleaned = MARKDOWN_RE.sub("", cleaned)
    cleaned = re.sub(r"[^\w\s.,!?;:'\"()\-\u0D00-\u0D7F।]", " ", cleaned)
    return WHITESPACE_RE.sub(" ", cleaned).strip()


class SentenceBuffer:
    """Accumulate partial model tokens and emit complete spoken sentences."""

    def __init__(self, max_sentence_chars: int = 500) -> None:
        self.max_sentence_chars = max_sentence_chars
        self._buffer = ""

    def push(self, token: str) -> list[str]:
        """Add a token and return any complete sentences."""
        self._buffer += token
        emitted: list[str] = []
        while self._buffer:
            match = SENTENCE_RE.search(self._buffer)
            if match:
                candidate = self._buffer[: match.start() + 1].strip()
                self._buffer = self._buffer[match.end() :].lstrip()
            elif len(self._buffer) >= self.max_sentence_chars:
                split_at = self._buffer.rfind(" ", 0, self.max_sentence_chars)
                if split_at <= 0:
                    break
                candidate = self._buffer[:split_at].strip()
                self._buffer = self._buffer[split_at + 1 :].lstrip()
            else:
                break
            if candidate:
                emitted.append(candidate)
        return emitted

    def flush(self) -> str:
        """Return and clear any remaining text."""
        remaining, self._buffer = self._buffer.strip(), ""
        return remaining


class SpeechSynthesizer:
    """Generate Edge TTS audio with explicit local fallbacks and hash caching."""

    def __init__(
        self,
        voices: dict[str, str] | None = None,
        cache_dir: str | Path = "audio/cache",
        *,
        provider: str = "edge",
        malayalam_model: str = "facebook/mms-tts-mal",
    ) -> None:
        self.voices = voices or {
            "en": "en-IN-NeerjaNeural",
            "ml": "ml-IN-SobhanaNeural",
            "ml_male": "ml-IN-MidhunNeural",
        }
        self.cache_dir = Path(cache_dir)
        self.provider = provider
        self.malayalam_model = malayalam_model
        self._offline_models: dict[str, tuple[Any, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._status_by_path: dict[str, str | None] = {}
        self._status_lock = threading.Lock()

    def _cache_path(
        self,
        text: str,
        language: str,
        voice: str,
        rate: str,
        cache_version: int | str | None = None,
    ) -> Path:
        key = hashlib.sha256(
            f"{language}\0{voice}\0{rate}\0{cache_version or ''}\0{text}".encode("utf-8")
        ).hexdigest()
        return self.cache_dir / f"{key}.mp3"

    async def synthesize(
        self,
        text: str,
        language: str,
        *,
        voice: str | None = None,
        rate: str = "+0%",
        cache_version: int | str | None = None,
    ) -> Path:
        """Generate audio, returning the cached/generated audio path."""
        cleaned = clean_tts_text(text)
        if not cleaned:
            raise ValueError("Cannot synthesize empty speech.")
        if language not in {"en", "ml"}:
            raise ValueError("Speech language must be 'en' or 'ml'.")
        locale_prefix = f"{language}-"
        configured_voice = self.voices.get(language, "")
        if not configured_voice.casefold().startswith(locale_prefix):
            raise ValueError(
                f"The configured {language} speech voice must use the '{locale_prefix}' locale."
            )
        selected_voice = voice or configured_voice
        if not selected_voice.casefold().startswith(locale_prefix):
            logger.warning(
                "Ignoring speech voice whose locale does not match the requested language",
                extra={"language": language},
            )
            selected_voice = configured_voice
        output_path = self._cache_path(
            cleaned, language, selected_voice, rate, cache_version
        )
        lock = self._locks.setdefault(str(output_path), asyncio.Lock())
        async with lock:
            if output_path.is_file():
                return output_path
            self.cache_dir.mkdir(parents=True, exist_ok=True)

            fallback_notice: str | None = None
            if self.provider == "edge":
                try:
                    import edge_tts

                    communicator = edge_tts.Communicate(
                        cleaned, selected_voice, rate=rate
                    )
                    await communicator.save(str(output_path))
                    with self._status_lock:
                        self._status_by_path[str(output_path)] = None
                    return output_path
                except Exception as exc:
                    output_path.unlink(missing_ok=True)
                    logger.exception(
                        "Edge TTS failed; trying the configured offline fallback"
                    )
                    fallback_notice = (
                        "Edge TTS is unavailable; AVA switched to offline speech."
                    )
                    if language == "ml":
                        fallback_notice += (
                            " The offline Malayalam voice may need its model downloaded first."
                        )
                    logger.warning("Edge TTS fallback reason: %s", type(exc).__name__)

            try:
                await asyncio.to_thread(
                    self._synthesize_offline, cleaned, language, output_path
                )
            except Exception:
                output_path.unlink(missing_ok=True)
                logger.exception("Offline speech synthesis failed")
                raise
            with self._status_lock:
                self._status_by_path[str(output_path)] = fallback_notice
        return output_path

    def get_status(self, audio_path: str | Path) -> str | None:
        """Return a user-visible service fallback notice for synthesized audio."""
        with self._status_lock:
            return self._status_by_path.get(str(audio_path))

    def _synthesize_offline(self, text: str, language: str, output_path: Path) -> None:
        """Run local MMS for Malayalam or pyttsx3 for English."""
        if language == "ml":
            import torch
            from transformers import AutoTokenizer, VitsModel

            if "ml" not in self._offline_models:
                tokenizer = AutoTokenizer.from_pretrained(self.malayalam_model)
                model = VitsModel.from_pretrained(self.malayalam_model)
                self._offline_models["ml"] = (tokenizer, model)
            tokenizer, model = self._offline_models["ml"]
            inputs = tokenizer(text, return_tensors="pt")
            with torch.no_grad():
                waveform = model(**inputs).waveform.squeeze().cpu().numpy()
            import soundfile as sf

            descriptor, temporary_name = tempfile.mkstemp(
                suffix=".wav", dir=self.cache_dir
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            try:
                sf.write(temporary, waveform, model.config.sampling_rate)
                self._wav_to_mp3(temporary, output_path)
            finally:
                temporary.unlink(missing_ok=True)
            return

        import pyttsx3

        temporary = output_path.with_suffix(".wav")
        engine = pyttsx3.init()
        english_voice = next(
            (
                voice for voice in engine.getProperty("voices")
                if self._voice_matches_language(voice, "en")
            ),
            None,
        )
        if english_voice is None:
            raise RuntimeError(
                "No English offline speech voice is installed; install an English voice "
                "or restore the Edge TTS connection."
            )
        engine.setProperty("voice", english_voice.id)
        engine.save_to_file(text, str(temporary))
        engine.runAndWait()
        self._wav_to_mp3(temporary, output_path)
        temporary.unlink(missing_ok=True)

    @staticmethod
    def _voice_matches_language(voice: Any, language: str) -> bool:
        """Check system voice locale metadata without assuming its default voice."""
        metadata = [str(getattr(voice, "name", "")), str(getattr(voice, "id", ""))]
        metadata.extend(
            value.decode("utf-8", errors="ignore") if isinstance(value, bytes) else str(value)
            for value in (getattr(voice, "languages", []) or [])
        )
        locale = re.compile(rf"(?<![a-z]){re.escape(language)}(?:[-_]|$)", re.IGNORECASE)
        return any(locale.search(value) for value in metadata)

    @staticmethod
    def _wav_to_mp3(source: Path, target: Path) -> None:
        """Convert fallback WAV output to MP3 using an installed ffmpeg binary."""
        import subprocess

        result = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(source), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"ffmpeg could not encode TTS audio: {result.stderr.strip()}")

    async def stream_sentences(
        self,
        tokens: AsyncIterator[str],
        language: str,
        *,
        voice: str | None = None,
    ) -> AsyncIterator[tuple[str, Path]]:
        """Synthesize complete sentences as soon as the input stream emits them."""
        buffer = SentenceBuffer()
        async for token in tokens:
            for sentence in buffer.push(token):
                cleaned = clean_tts_text(sentence)
                if cleaned:
                    yield cleaned, await self.synthesize(cleaned, language, voice=voice)
        remainder = clean_tts_text(buffer.flush())
        if remainder:
            yield remainder, await self.synthesize(remainder, language, voice=voice)
