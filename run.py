"""One-command development server launcher for AVA."""

from __future__ import annotations

import argparse

import uvicorn

from config.settings import load_settings


def main() -> None:
    """Start the FastAPI server with the configured host and port."""
    settings = load_settings()
    parser = argparse.ArgumentParser(description="Run AVA locally.")
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    args = parser.parse_args()
    uvicorn.run("interface.api:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
