"""Rotating local application logs without user content or credentials."""

import logging
import json
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


class JsonFormatter(logging.Formatter):
    """One structured JSON event per line; omit raw exceptions and stack locals."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({"timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                           "level": record.levelname, "logger": record.name,
                           "message": record.getMessage()}, ensure_ascii=False)


def configure_logging() -> None:
    """Configure once; retain stdout if local log storage is unavailable."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if any(getattr(handler, "policypilot", False) for handler in root.handlers):
        return
    formatter = JsonFormatter()
    try:
        directory = Path(__file__).parent / "logs"
        directory.mkdir(exist_ok=True)
        handler = RotatingFileHandler(directory / "policypilot.log", maxBytes=5_000_000,
                                      backupCount=3, encoding="utf-8")
        handler.setFormatter(formatter)
        handler.policypilot = True
        root.addHandler(handler)
    except OSError:
        logging.getLogger(__name__).warning("File logging unavailable; using console logging")
    # Third-party HTTP request logs can contain query strings; keep them out of INFO logs.
    for name in ("httpx", "httpx2", "httpcore", "openai"):
        logging.getLogger(name).setLevel(logging.WARNING)
