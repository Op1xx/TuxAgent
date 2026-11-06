"""Настройка логирования приложения."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import AppSettings

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(settings: AppSettings, level: int = logging.INFO) -> Path:
    """Настроить запись логов в ``<data_dir>/logs/tuxagent.log``."""
    log_dir = Path(settings.data_dir).expanduser() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "tuxagent.log"

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_tuxagent", False):
            root.removeHandler(handler)
            handler.close()

    handler = RotatingFileHandler(
        log_path,
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler._tuxagent = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)
    return log_path
