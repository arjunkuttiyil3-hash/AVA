"""Compatibility tests for the ``python app.py`` entry point."""

import app as standalone
from interface.api import app as modular_app, create_app as modular_factory


def test_app_entrypoint_reuses_the_modular_fastapi_application() -> None:
    assert standalone.app is modular_app
    assert standalone.create_app is modular_factory
    assert any(
        getattr(route, "path", None) == "/ws/{session_id}"
        for route in standalone.app.routes
    )
