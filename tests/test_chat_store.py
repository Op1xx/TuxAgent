"""Тесты хранилища чатов и форматирования времени."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from chat_store import ChatMessage, ChatSession, ChatStore, format_chat_time


def test_chat_store_roundtrip(tmp_path: Path) -> None:
    store = ChatStore(tmp_path)
    chat = ChatSession(
        name="Проверка VLC",
        messages=[
            ChatMessage(kind="command", text="› Проверь VLC"),
            ChatMessage(kind="info", text="Запрос принят"),
        ],
    )

    store.save([chat])
    loaded = store.load()

    assert store.path == tmp_path / "chats.json"
    assert len(loaded) == 1
    assert loaded[0].id == chat.id
    assert loaded[0].name == "Проверка VLC"
    assert loaded[0].created_at == chat.created_at
    assert loaded[0].messages == chat.messages


def test_chat_store_returns_empty_for_broken_json(tmp_path: Path) -> None:
    store = ChatStore(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{не json", encoding="utf-8")

    assert store.load() == []


def test_format_chat_time() -> None:
    now = datetime.now(UTC)
    old = (now - timedelta(days=2)).isoformat(timespec="seconds")

    assert format_chat_time(now.isoformat(timespec="seconds")) == "Только что"
    assert format_chat_time(old) == "2 д назад"
    assert format_chat_time("не дата") == ""
