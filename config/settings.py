"""Load and validate AVA's YAML and environment configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _environment_value(name: str, default: str) -> str:
    """Ignore empty optional environment values from the example dotenv file."""
    value = os.getenv(name)
    return value if value else default


@dataclass(frozen=True)
class AppSettings:
    """Typed, validated application settings."""

    name: str = "AVA"
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    config_path: Path = PROJECT_ROOT / "config" / "ava.yaml"
    llm: dict[str, Any] = field(default_factory=dict)
    speech: dict[str, Any] = field(default_factory=dict)
    memory: dict[str, Any] = field(default_factory=dict)
    robot: dict[str, Any] = field(default_factory=dict)

    @property
    def database_path(self) -> Path:
        """Return the configured database path, resolved against the project."""
        raw_path = _environment_value(
            "AVA_DB_PATH", str(self.memory.get("database_path", "database/ava.db"))
        )
        path = Path(raw_path).expanduser()
        return path if path.is_absolute() else PROJECT_ROOT / path


def load_settings(config_path: str | Path | None = None) -> AppSettings:
    """Load YAML settings, then apply supported environment overrides."""
    load_dotenv(PROJECT_ROOT / ".env")
    env_config = _environment_value(
        "AVA_CONFIG_PATH", str(PROJECT_ROOT / "config" / "ava.yaml")
    )
    selected_path = Path(config_path or env_config).expanduser()
    if not selected_path.is_absolute():
        selected_path = PROJECT_ROOT / selected_path
    if not selected_path.is_file():
        raise FileNotFoundError(f"AVA configuration file not found: {selected_path}")

    with selected_path.open("r", encoding="utf-8") as config_file:
        raw = yaml.safe_load(config_file) or {}
    if not isinstance(raw, dict):
        raise ValueError("AVA configuration must contain a YAML mapping.")

    app = raw.get("app", {})
    settings = AppSettings(
        name=str(app.get("name", "AVA")),
        host=_environment_value("AVA_HOST", str(app.get("host", "127.0.0.1"))),
        port=int(_environment_value("AVA_PORT", str(app.get("port", 8000)))),
        log_level=_environment_value(
            "AVA_LOG_LEVEL", str(app.get("log_level", "INFO"))
        ),
        config_path=selected_path.resolve(),
        llm={
            **raw.get("llm", {}),
            "default_model": _environment_value(
                "GEMINI_MODEL",
                str(raw.get("llm", {}).get("default_model", "gemini-2.5-flash")),
            ),
        },
        speech=raw.get("speech", {}),
        memory=raw.get("memory", {}),
        robot=raw.get("robot", {}),
    )
    if not 1 <= settings.port <= 65535:
        raise ValueError("AVA_PORT must be between 1 and 65535.")
    return settings
