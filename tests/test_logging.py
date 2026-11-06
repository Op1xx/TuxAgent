"""Тесты логирования и audit-журнала."""

from __future__ import annotations

import logging
from pathlib import Path

from audit import AuditLogger, AuditRecord
from config import AppSettings
from logging_setup import configure_logging


def _drop_tuxagent_handlers() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_tuxagent", False):
            root.removeHandler(handler)
            handler.close()


def test_configure_logging_writes_file(tmp_path: Path) -> None:
    settings = AppSettings(
        data_dir=str(tmp_path / "data"),
        models_dir=str(tmp_path / "models"),
    )

    try:
        log_path = configure_logging(settings)
        logging.getLogger("tuxagent.test").info("проверка лога")
        for handler in logging.getLogger().handlers:
            handler.flush()

        assert log_path == tmp_path / "data" / "logs" / "tuxagent.log"
        assert "проверка лога" in log_path.read_text(encoding="utf-8")
    finally:
        _drop_tuxagent_handlers()


def test_audit_jsonl_roundtrip(tmp_path: Path) -> None:
    audit = AuditLogger(tmp_path)
    record = AuditRecord(
        tool="mkdir",
        argv=["mkdir", "-p", "/tmp/project"],
        result="готово",
        returncode=0,
        confirmed=True,
    )

    audit.write(record)
    records = audit.read()

    assert audit.path == tmp_path / "audit" / "audit.jsonl"
    assert len(records) == 1
    assert records[0]["tool"] == "mkdir"
    assert records[0]["argv"] == ["mkdir", "-p", "/tmp/project"]
    assert records[0]["result"] == "готово"
    assert records[0]["returncode"] == 0
    assert records[0]["confirmed"] is True
    assert records[0]["timestamp"]
