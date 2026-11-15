"""Тесты интерфейса LLM и mock-бэкенда со сценариями из JSON."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm import (
    DEFAULT_SCENARIOS_PATH,
    BackendNotImplementedError,
    LLMError,
    Message,
    MockBackend,
    create_backend,
    load_script,
    parse_script,
)

SCENARIO_PAYLOAD = {
    "version": 1,
    "fallback": {"reply": "Сценарий не найден."},
    "scenarios": [
        {
            "name": "install",
            "match": "поставь (?P<package>[\\w.+-]+)",
            "steps": [
                {
                    "tool": "package_install",
                    "arguments": {"packages": ["{package}"]},
                },
                {"reply": "Пакет {package} установлен."},
            ],
        }
    ],
}


def _write(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "scenarios.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_shipped_scenarios_are_valid() -> None:
    script = load_script(DEFAULT_SCENARIOS_PATH)

    names = {scenario.name for scenario in script.scenarios}
    assert {"list-dir", "find-files", "install-package", "delete-file"} <= names
    assert all(scenario.steps for scenario in script.scenarios)
    assert script.fallback


def test_mock_backend_returns_steps_and_substitutes_groups(tmp_path: Path) -> None:
    backend = MockBackend(_write(tmp_path, SCENARIO_PAYLOAD))
    messages = [Message("user", "поставь firefox")]

    first = backend.complete(messages, [])
    second = backend.complete(messages, [])
    third = backend.complete(messages, [])

    assert first.tool_calls[0].name == "package_install"
    assert first.tool_calls[0].arguments == {"packages": ["firefox"]}
    assert second.content == "Пакет firefox установлен."
    assert second.tool_calls == ()
    assert third.content == "Готово."


def test_mock_backend_reset_starts_over(tmp_path: Path) -> None:
    backend = MockBackend(_write(tmp_path, SCENARIO_PAYLOAD))
    messages = [Message("user", "поставь firefox")]

    backend.complete(messages, [])
    backend.reset()
    again = backend.complete([Message("user", "поставь vlc")], [])

    assert again.tool_calls[0].arguments == {"packages": ["vlc"]}


def test_mock_backend_falls_back_when_nothing_matches(tmp_path: Path) -> None:
    backend = MockBackend(_write(tmp_path, SCENARIO_PAYLOAD))

    response = backend.complete([Message("user", "привет")], [])

    assert response.content == "Сценарий не найден."
    assert response.tool_calls == ()


def test_mock_backend_supports_numeric_groups(tmp_path: Path) -> None:
    payload = {
        "scenarios": [
            {
                "name": "two",
                "match": "(\\w+)-(\\w+)",
                "steps": [
                    {
                        "tool": "record",
                        "arguments": {"first": "{1}", "second": "{2}"},
                    }
                ],
            }
        ]
    }
    backend = MockBackend(_write(tmp_path, payload))

    response = backend.complete([Message("user", "a-b")], [])

    assert response.tool_calls[0].arguments == {"first": "a", "second": "b"}


def test_match_can_be_a_list_of_patterns(tmp_path: Path) -> None:
    payload = {
        "scenarios": [
            {
                "name": "multi",
                "match": ["покажи (?P<path>\\S+)", "что в (?P<path>\\S+)"],
                "steps": [{"tool": "list_dir", "arguments": {"path": "{path}"}}],
            }
        ]
    }
    backend = MockBackend(_write(tmp_path, payload))

    response = backend.complete([Message("user", "что в /tmp")], [])

    assert response.tool_calls[0].arguments == {"path": "/tmp"}


def test_script_errors_are_reported(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{broken", encoding="utf-8")
    with pytest.raises(LLMError, match="Некорректный JSON"):
        load_script(broken)

    with pytest.raises(LLMError, match="нет списка scenarios"):
        parse_script({"fallback": {"reply": "x"}})

    with pytest.raises(LLMError, match="нужен tool или непустой reply"):
        parse_script({"scenarios": [{"name": "x", "match": "x", "steps": [{}]}]})


def test_create_backend_selects_implementations(tmp_path: Path) -> None:
    backend = create_backend("mock", scenarios_path=_write(tmp_path, SCENARIO_PAYLOAD))

    assert isinstance(backend, MockBackend)
    with pytest.raises(BackendNotImplementedError, match="этап 5"):
        create_backend("llama_cpp")
    with pytest.raises(BackendNotImplementedError, match="этап 5"):
        create_backend("ollama")
    with pytest.raises(LLMError, match="Неизвестный"):
        create_backend("gpt")
