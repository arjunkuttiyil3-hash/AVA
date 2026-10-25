"""Check AVA dependencies, Gemini configuration, microphone, ffmpeg, and database."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import load_settings  # noqa: E402
from llm import GeminiRequestError, api_key, model_name  # noqa: E402


def check_gemini() -> tuple[bool, str]:
    """Check for a Gemini API key without making a billable API request."""
    try:
        api_key()
        return True, f"API key configured for {model_name()}; network is checked during generation"
    except GeminiRequestError as exc:
        return False, str(exc)


def check_microphone() -> tuple[bool, str]:
    """Query sounddevice for at least one available input device."""
    try:
        import sounddevice as sd

        devices: list[dict[str, Any]] = sd.query_devices()
        inputs = [
            device["name"]
            for device in devices
            if int(device.get("max_input_channels", 0)) > 0
        ]
        return (True, f"Microphone available: {inputs[0]}") if inputs else (
            False,
            "No microphone input device found; text chat remains available",
        )
    except Exception as exc:
        return False, f"Microphone check failed ({type(exc).__name__})"


def check_api(host: str, port: int) -> tuple[bool, str]:
    """Check whether the running AVA service exposes its diagnostic endpoint."""
    url = f"http://{host}:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return True, f"AVA API is responding ({payload.get('status', 'unknown')})"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return False, f"AVA API is not running at {url} ({type(exc).__name__})"


def main() -> int:
    """Run all checks, printing actionable status for unavailable services."""
    settings = load_settings()
    checks: list[tuple[str, bool, str, bool]] = []
    for package in (
        "fastapi",
        "uvicorn",
        "google.genai",
        "dotenv",
        "faster_whisper",
        "sounddevice",
        "edge_tts",
        "soundfile",
        "pyttsx3",
        "yaml",
        "serial",
    ):
        present = importlib.util.find_spec(package) is not None
        checks.append((f"Python package {package}", present, "installed" if present else "missing", True))

    gemini_ok, gemini_message = check_gemini()
    checks.append(("Gemini API configuration", gemini_ok, gemini_message, True))
    mic_ok, mic_message = check_microphone()
    checks.append(("Microphone", mic_ok, mic_message, False))

    ffmpeg = shutil.which("ffmpeg")
    checks.append(
        ("ffmpeg", bool(ffmpeg), ffmpeg or "not found; offline TTS audio conversion is unavailable", False)
    )

    try:
        database_path = settings.database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database_path, timeout=3) as connection:
            quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        database_ok = quick_check == "ok"
        checks.append(("SQLite", database_ok, f"{database_path} ({quick_check})", True))
    except (OSError, sqlite3.Error) as exc:
        checks.append(("SQLite", False, str(exc), True))

    api_ok, api_message = check_api(settings.host, settings.port)
    checks.append(("AVA API", api_ok, api_message, False))

    for name, ok, message, required in checks:
        marker = "OK" if ok else "MISSING" if required else "OPTIONAL"
        print(f"[{marker:7}] {name}: {message}")
    print("\nOptional hardware/services may be unavailable; text-based fallback remains enabled.")
    return 1 if any(not ok and required for _, ok, _, required in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
