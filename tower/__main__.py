"""`python -m tower`: run the ingestion API with its embedded workers on 127.0.0.1:8500."""

from __future__ import annotations

import argparse

import uvicorn

from tower.config import load_settings
from tower.logging_ import configure_logging
from tower.main import create_app


def main() -> None:
    settings = load_settings()
    parser = argparse.ArgumentParser(description="Tarangini control tower ingestion service")
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    args = parser.parse_args()
    configure_logging()
    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
