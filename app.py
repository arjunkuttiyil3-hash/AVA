"""Compatibility launcher for the modular AVA FastAPI application."""

from __future__ import annotations

from interface.api import app, create_app
from run import main

__all__ = ["app", "create_app"]


if __name__ == "__main__":
    main()
