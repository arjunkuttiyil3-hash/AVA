"""Faster-Whisper speech recognition and sounddevice microphone capture."""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Transcription:
    """Recognized speech and its detected/requested language."""

    text: str
    language: str


class SpeechRecognizer:
    """Lazy-loaded Faster-Whisper model with push-to-talk and live capture."""

    def __init__(
        self,
        model_size: str = "small",
        language: str | None = None,
        *,
        vad_filter: bool = True,
        device: str = "auto",
        compute_type: str = "default",
        sample_rate: int = 16000,
    ) -> None:
        self.model_size = model_size
        self.language = language
        self.vad_filter = vad_filter
        self.device = device
        self.compute_type = compute_type
        self.sample_rate = sample_rate
        self._model: Any = None
        self._model_device: str | None = None
        self._model_lock = threading.Lock()

    @staticmethod
    def _is_gpu_runtime_failure(error: RuntimeError) -> bool:
        message = str(error).lower()
        return any(
            marker in message
            for marker in ("cublas", "cudnn", "cuda driver version is insufficient")
        )

    def _load_model(self) -> Any:
        if self._model is None:
            with self._model_lock:
                if self._model is None:
                    from faster_whisper import WhisperModel

                    self._model = WhisperModel(
                        self.model_size,
                        device=self.device,
                        compute_type=self.compute_type,
                    )
                    self._model_device = self.device
        return self._model

    def load_model(self) -> None:
        """Load the configured recognition model before a transcription request."""
        try:
            self._load_model()
        except RuntimeError as exc:
            if not self._is_gpu_runtime_failure(exc) or self._model_device == "cpu":
                raise
            logger.warning(
                "Faster-Whisper GPU model loading failed; retrying on CPU: %s", exc
            )
            self._load_cpu_model()

    def _load_cpu_model(self) -> None:
        with self._model_lock:
            if self._model_device != "cpu":
                from faster_whisper import WhisperModel

                self._model = WhisperModel(
                    self.model_size,
                    device="cpu",
                    compute_type=self.compute_type,
                )
                self._model_device = "cpu"

    def transcribe(self, audio: str | np.ndarray, language: str | None = None) -> Transcription:
        """Transcribe a file or mono float audio array using VAD filtering."""
        requested_language = language or self.language
        return self._transcribe(audio, requested_language)

    def transcribe_detect(self, audio: str | np.ndarray) -> Transcription:
        """Transcribe with Faster-Whisper language detection instead of the configured hint."""
        return self._transcribe(audio, None)

    def _transcribe(
        self, audio: str | np.ndarray, requested_language: str | None
    ) -> Transcription:
        try:
            segments, info = self._load_model().transcribe(
                audio,
                language=requested_language,
                vad_filter=self.vad_filter,
                beam_size=5,
            )
            text = " ".join(segment.text.strip() for segment in segments).strip()
        except RuntimeError as exc:
            if not self._is_gpu_runtime_failure(exc) or self._model_device == "cpu":
                raise
            logger.warning(
                "Faster-Whisper GPU transcription failed; retrying on CPU: %s", exc
            )
            self._load_cpu_model()
            return self._transcribe(audio, requested_language)
        if not text:
            raise ValueError("No speech was detected. Please try speaking again.")
        return Transcription(text=text, language=info.language or requested_language or "en")

    def record_push_to_talk(self, duration_seconds: float) -> np.ndarray:
        """Record one push-to-talk clip from the default sounddevice input."""
        if duration_seconds <= 0 or duration_seconds > 120:
            raise ValueError("Recording duration must be between 0 and 120 seconds.")
        import sounddevice as sd

        audio = sd.rec(
            int(duration_seconds * self.sample_rate),
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
        )
        sd.wait()
        return np.asarray(audio, dtype=np.float32).reshape(-1)

    def continuous_audio(self, *, block_seconds: float = 0.5) -> Iterator[np.ndarray]:
        """Yield live mono audio blocks until the consumer stops iteration."""
        import sounddevice as sd

        if block_seconds <= 0:
            raise ValueError("Audio block duration must be positive.")
        blocks: queue.Queue[np.ndarray] = queue.Queue(maxsize=8)

        def callback(
            indata: np.ndarray, frames: int, time_info: Any, status: Any
        ) -> None:
            _ = (frames, time_info)
            if status:
                logger.warning("Microphone stream status: %s", status)
            block = np.asarray(indata[:, 0], dtype=np.float32).copy()
            try:
                blocks.put_nowait(block)
            except queue.Full:
                logger.warning("Dropping microphone audio block because consumer is behind")

        with sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            blocksize=int(self.sample_rate * block_seconds),
            callback=callback,
        ):
            while True:
                yield blocks.get()
