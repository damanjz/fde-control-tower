"""Structured JSON logs. Callers pass fields via `log(logger, level, msg, **fields)`; never pass secrets or PII."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

_SENSITIVE = ("secret", "key", "password", "token", "signature", "phone", "bank", "authorization")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k, v in (getattr(record, "fields", None) or {}).items():
            out[k] = "[redacted]" if any(s in k.lower() for s in _SENSITIVE) else v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info).splitlines()[-1]
        return json.dumps(out, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "psycopg.pool"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log(logger: logging.Logger, level: int, msg: str, **fields) -> None:
    logger.log(level, msg, extra={"fields": fields})
