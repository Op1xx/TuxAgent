"""Заготовка audit-лога: JSONL с действиями инструментов."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path


@dataclass(slots=True)
class AuditRecord:
    """Одно выполненное действие агента."""

    tool: str
    argv: list[str]
    result: str
    returncode: int | None = None
    confirmed: bool = False
    timestamp: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )


class AuditLogger:
    """Пишет записи в ``<data_dir>/audit/audit.jsonl``."""

    def __init__(self, data_dir: str | Path) -> None:
        self.path = Path(data_dir).expanduser() / "audit" / "audit.jsonl"

    def write(self, record: AuditRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

    def read(self) -> list[dict]:
        if not self.path.is_file():
            return []
        with self.path.open(encoding="utf-8") as stream:
            return [json.loads(line) for line in stream if line.strip()]
