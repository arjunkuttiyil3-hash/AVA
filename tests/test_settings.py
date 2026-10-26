"""Tests for environment-backed settings and blank optional path values."""

from config.settings import PROJECT_ROOT, load_settings


def test_empty_optional_path_values_use_project_defaults(monkeypatch) -> None:
    monkeypatch.setenv("AVA_CONFIG_PATH", "")
    monkeypatch.setenv("AVA_DB_PATH", "")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.5-flash")
    monkeypatch.setenv("GEMINI_FALLBACK_MODEL", "gemini-3.6-flash")

    settings = load_settings()

    assert settings.config_path == PROJECT_ROOT / "config" / "ava.yaml"
    assert settings.database_path == PROJECT_ROOT / "database" / "ava.db"
    assert settings.llm["default_model"] == "gemini-3.5-flash"
    assert settings.llm["fallback_model"] == "gemini-3.6-flash"
