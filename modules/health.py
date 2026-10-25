"""Bounded runtime checks for optional and required AVA services."""

from __future__ import annotations

import logging
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from llm import GeminiRequestError, api_key, fallback_model_name, model_name

logger = logging.getLogger(__name__)


def check_gemini() -> dict[str, Any]:
    """Report Gemini configuration without making a billable network request."""
    try:
        api_key()
        selected_model = model_name()
        return {
            "available": True,
            "configured": True,
            "key_set": True,
            "model": selected_model,
            "fallback_model": fallback_model_name() or None,
            "message": (
                f"{selected_model} configured; API reachability is verified by "
                "the health endpoint's test generation"
            ),
        }
    except GeminiRequestError as exc:
        return {
            "available": False,
            "configured": False,
            "key_set": False,
            "model": model_name(),
            "fallback_model": fallback_model_name() or None,
            "message": str(exc),
        }


def check_ffmpeg() -> dict[str, Any]:
    """Check whether the ffmpeg executable is on PATH."""
    executable = shutil.which("ffmpeg")
    return {
        "available": executable is not None,
        "message": executable or "ffmpeg was not found on PATH; offline TTS encoding is unavailable",
    }


def check_microphone() -> dict[str, Any]:
    """Check for a sounddevice audio input without starting a recording."""
    try:
        import sounddevice as sd

        devices = sd.query_devices()
        input_devices = [
            str(device.get("name", "input"))
            for device in devices
            if int(device.get("max_input_channels", 0)) > 0
        ]
        if input_devices:
            return {"available": True, "message": input_devices[0]}
        return {
            "available": False,
            "message": "No microphone input device found; text chat remains available",
        }
    except Exception as exc:
        logger.debug("Microphone probe failed", exc_info=True)
        return {
            "available": False,
            "message": f"Microphone check failed: {type(exc).__name__}: {exc}",
        }


def check_database(database_path: str | Path) -> dict[str, Any]:
    """Verify SQLite can be opened and passes its quick integrity check."""
    try:
        with sqlite3.connect(database_path, timeout=2) as connection:
            result = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        return {"available": result == "ok", "message": result}
    except (OSError, sqlite3.Error) as exc:
        return {"available": False, "message": str(exc)}


def collect_health(settings: Any) -> dict[str, Any]:
    """Collect service checks for logs, the UI banner, and API health endpoint."""
    gemini = check_gemini()
    return {
        "gemini": gemini,
        "ffmpeg": check_ffmpeg(),
        "microphone": check_microphone(),
        "database": check_database(settings.database_path),
    }


def log_health(health: dict[str, Any]) -> None:
    """Log explicit healthy/degraded status for each configured dependency."""
    gemini = health["gemini"]
    if not gemini["available"]:
        logger.error("Gemini configuration check failed: %s", gemini["message"])
    else:
        logger.info("Gemini configuration check passed: %s", gemini["message"])

    for service in ("ffmpeg", "microphone", "database"):
        result = health[service]
        if result["available"]:
            logger.info("%s health check passed: %s", service, result["message"])
        elif service == "database":
            logger.error("%s health check failed: %s", service, result["message"])
        else:
            logger.warning("%s health check failed: %s", service, result["message"])
