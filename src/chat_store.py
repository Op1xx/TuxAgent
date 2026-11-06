"""Хранилище истории чатов в ``<data_dir>/chats.json``."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

CHAT_FILE_VERSION = 1


def utc_now_iso() -> str:
    """Текущее время в ISO-формате UTC."""
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(slots=True)
class ChatMessage:
    """Одно сообщение в истории чата."""

    kind: str
    text: str


@dataclass(slots=True)
class ChatSession:
    """Чат: имя, сообщения и служебные метки времени."""

    name: str
    messages: list[ChatMessage] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    auto_named: bool = False


def format_chat_time(timestamp: str) -> str:
    """Показать время чата по-русски: «Только что», «5 мин назад», «2 д назад»."""
    try:
        moment = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)

    seconds = max(0, int((datetime.now(UTC) - moment).total_seconds()))
    if seconds < 60:
        return "Только что"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч назад"
    return f"{hours // 24} д назад"


class ChatStore:
    """Читает и атомарно сохраняет список чатов."""

    def __init__(self, data_dir: str | Path) -> None:
        self.path = Path(data_dir).expanduser() / "chats.json"

    def load(self) -> list[ChatSession]:
        """Загрузить чаты; при повреждённом файле вернуть пустой список."""
        if not self.path.is_file():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            logger.warning("Не удалось прочитать %s: %s", self.path, error)
            return []
        if not isinstance(payload, dict):
            logger.warning("Неверный формат %s", self.path)
            return []

        chats: list[ChatSession] = []
        for item in payload.get("chats", []):
            if not isinstance(item, dict):
                continue
            messages = [
                ChatMessage(
                    kind=str(message.get("kind", "info")),
                    text=str(message.get("text", "")),
                )
                for message in item.get("messages", [])
                if isinstance(message, dict)
            ]
            chats.append(
                ChatSession(
                    name=str(item.get("name") or "Новый чат"),
                    messages=messages,
                    created_at=str(item.get("created_at") or utc_now_iso()),
                    updated_at=str(item.get("updated_at") or utc_now_iso()),
                    id=str(item.get("id") or uuid.uuid4().hex),
                    auto_named=bool(item.get("auto_named", False)),
                )
            )
        return chats

    def save(self, chats: Sequence[ChatSession]) -> None:
        """Сохранить чаты атомарно, чтобы не оставить обрезанный JSON."""
        payload = {
            "version": CHAT_FILE_VERSION,
            "chats": [asdict(chat) for chat in chats],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)
