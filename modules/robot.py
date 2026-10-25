"""Robot controller abstraction with serial and local simulated implementations."""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)
VALID_GESTURES = {"wave", "point", "expressive", "bow", "stop"}


class RobotController(ABC):
    """Common asynchronous control surface used by the conversation pipeline."""

    @abstractmethod
    async def speak(self, text: str) -> None:
        """Send a speech action to the robot, if supported."""
        raise NotImplementedError

    @abstractmethod
    async def gesture(self, name: str) -> None:
        """Perform one named, whitelisted gesture."""
        raise NotImplementedError

    @abstractmethod
    async def stop(self) -> None:
        """Stop the robot's active action."""
        raise NotImplementedError

    @abstractmethod
    def is_connected(self) -> bool:
        """Return whether the controller currently has an active connection."""
        raise NotImplementedError


class SimulatedRobot(RobotController):
    """In-memory robot that reports actions for the animated browser avatar."""

    def __init__(self) -> None:
        self.connected = True
        self.last_gesture = "idle"
        self.last_text = ""

    async def speak(self, text: str) -> None:
        self.last_text = text

    async def gesture(self, name: str) -> None:
        self._validate_gesture(name)
        self.last_gesture = name

    async def stop(self) -> None:
        self.last_gesture = "stop"

    def is_connected(self) -> bool:
        return self.connected

    @staticmethod
    def _validate_gesture(name: str) -> None:
        if name not in VALID_GESTURES:
            raise ValueError(f"Unsupported robot gesture: {name}")


class SerialRobot(RobotController):
    """Reconnect-on-demand serial controller using the documented G:/S: protocol."""

    def __init__(self, port: str, baud_rate: int = 115200, timeout: float = 1.0) -> None:
        if not port.strip():
            raise ValueError("A serial robot port must be configured.")
        self.port = port
        self.baud_rate = baud_rate
        self.timeout = timeout
        self._serial = None
        self._lock = threading.Lock()

    def _connect(self) -> Any:
        if self._serial is not None and self._serial.is_open:
            return self._serial
        try:
            import serial

            self._serial = serial.Serial(
                self.port,
                self.baud_rate,
                timeout=self.timeout,
                write_timeout=self.timeout,
            )
            return self._serial
        except Exception:
            self._serial = None
            logger.exception("Could not connect to the configured robot port")
            raise

    def _send(self, command: str) -> None:
        with self._lock:
            connection = self._connect()
            try:
                connection.write((command + "\n").encode("utf-8"))
                connection.flush()
            except Exception:
                try:
                    connection.close()
                finally:
                    self._serial = None
                logger.exception("Robot serial command failed")
                raise

    async def speak(self, text: str) -> None:
        """Send a bounded UTF-8 speech line to a controller with a display/audio module."""
        safe_text = re.sub(r"[\r\n]+", " ", text).strip()[:160]
        if safe_text:
            await asyncio.to_thread(self._send, f"S:{safe_text}")

    async def gesture(self, name: str) -> None:
        """Send a whitelisted gesture command."""
        SimulatedRobot._validate_gesture(name)
        await asyncio.to_thread(self._send, f"G:{name.upper()}")

    async def stop(self) -> None:
        """Request an immediate controller stop."""
        await asyncio.to_thread(self._send, "G:STOP")

    def is_connected(self) -> bool:
        """Report an open serial link without attempting an implicit connection."""
        return self._serial is not None and bool(self._serial.is_open)


def gesture_for_response(task: str, intent: str = "", message: str = "") -> str:
    """Map output purpose to a consistent named gesture."""
    lowered = message.casefold()
    if intent == "robot_command":
        for name in ("stop", "bow", "point", "wave"):
            if name in lowered:
                return name
        return "wave"
    if any(cue in lowered for cue in ("closing", "vote of thanks", "thank everyone")):
        return "bow"
    if any(cue in lowered for cue in ("welcome", "greet", "hello")):
        return "wave"
    if any(cue in lowered for cue in ("announcement", "next item", "next event")):
        return "point"
    return {
        "greeting": "wave",
        "welcome": "wave",
        "anchoring_script": "point",
        "schedule_next": "point",
        "announcement": "point",
        "story_request": "expressive",
        "story_followup": "expressive",
        "closing": "bow",
        "vote_of_thanks": "bow",
    }.get(task, "expressive")
