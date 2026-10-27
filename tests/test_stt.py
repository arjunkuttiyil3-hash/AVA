"""Speech-recognition fallback behavior for unavailable GPU runtimes."""

import sys
from types import ModuleType, SimpleNamespace

import numpy as np

from modules.stt import SpeechRecognizer


def test_gpu_runtime_failure_retries_transcription_on_cpu(monkeypatch) -> None:
    devices: list[str] = []

    class FakeWhisperModel:
        def __init__(self, model_size: str, *, device: str, compute_type: str) -> None:
            _ = (model_size, compute_type)
            self.device = device
            devices.append(device)

        def transcribe(self, audio, **kwargs):
            _ = (audio, kwargs)
            if self.device != "cpu":
                def unavailable_gpu_segments():
                    raise RuntimeError("Library cublas64_12.dll is not found")
                    yield SimpleNamespace(text="")

                return unavailable_gpu_segments(), SimpleNamespace(language="en")
            return iter([SimpleNamespace(text=" The speaker said hello. ")]), SimpleNamespace(
                language="en"
            )

    fake_module = ModuleType("faster_whisper")
    fake_module.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)

    recognizer = SpeechRecognizer(device="auto")
    result = recognizer.transcribe(np.zeros(16000, dtype=np.float32))

    assert result.text == "The speaker said hello."
    assert result.language == "en"
    assert devices == ["auto", "cpu"]


def test_gpu_model_loading_failure_retries_on_cpu(monkeypatch) -> None:
    devices: list[str] = []

    class FakeWhisperModel:
        def __init__(self, model_size: str, *, device: str, compute_type: str) -> None:
            _ = (model_size, compute_type)
            devices.append(device)
            if device != "cpu":
                raise RuntimeError("Library cublas64_12.dll is not found")

    fake_module = ModuleType("faster_whisper")
    fake_module.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)

    recognizer = SpeechRecognizer(device="auto")
    recognizer.load_model()

    assert devices == ["auto", "cpu"]
