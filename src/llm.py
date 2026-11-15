"""Интерфейс LLM-бэкенда и реализации: mock, llama.cpp, Ollama.

Агентный цикл не зависит от конкретной модели: он получает :class:`LLMResponse`
с текстом и/или вызовами инструментов. Единственная реализация этого этапа —
:class:`MockBackend`: она отвечает по сценариям из JSON и нужна для тестов и
работы GUI без загруженной модели. Реальные бэкенды появятся на этапе 5.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

DEFAULT_SCENARIOS_PATH = (
    Path(__file__).resolve().parent.parent / "resources" / "mock_scenarios.json"
)

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*|\d+)\}")


class LLMError(RuntimeError):
    """Ошибка LLM-бэкенда."""


class BackendNotImplementedError(LLMError):
    """Бэкенд выбран в настройках, но ещё не реализован."""


@dataclass(frozen=True, slots=True)
class Message:
    """Одно сообщение диалога с моделью."""

    role: str  # "user" | "assistant" | "tool" | "system"
    content: str


@dataclass(frozen=True, slots=True)
class ToolCall:
    """Вызов инструмента, который предложила модель."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """Ответ модели: текст и/или список вызовов инструментов."""

    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()


@runtime_checkable
class LLMBackend(Protocol):
    """Минимальный интерфейс модели, достаточный агентному циклу."""

    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, Any]],
    ) -> LLMResponse:
        """Вернуть следующий ответ на историю сообщений и схемы инструментов."""

    def reset(self) -> None:
        """Начать новый запрос, забыв состояние предыдущего."""


@dataclass(frozen=True, slots=True)
class MockStep:
    """Один шаг сценария: либо текст ответа, либо вызов инструмента."""

    tool: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    reply: str = ""


@dataclass(frozen=True, slots=True)
class MockScenario:
    """Сценарий: шаблоны запроса и последовательность шагов."""

    name: str
    patterns: tuple[re.Pattern[str], ...]
    steps: tuple[MockStep, ...]

    def match(self, request: str) -> re.Match[str] | None:
        """Найти первое совпадение любого шаблона сценария."""
        for pattern in self.patterns:
            found = pattern.search(request)
            if found is not None:
                return found
        return None


@dataclass(frozen=True, slots=True)
class MockScript:
    """Разобранный файл сценариев mock-бэкенда."""

    scenarios: tuple[MockScenario, ...]
    fallback: str


def _parse_step(raw: Any, scenario: str, index: int) -> MockStep:
    if not isinstance(raw, dict):
        raise LLMError(f"Сценарий «{scenario}», шаг {index}: ожидался объект")
    if "tool" in raw:
        arguments = raw.get("arguments", {})
        if not isinstance(arguments, dict):
            raise LLMError(f"Сценарий «{scenario}», шаг {index}: arguments должен быть объектом")
        return MockStep(tool=str(raw["tool"]), arguments=dict(arguments))
    reply = raw.get("reply")
    if not isinstance(reply, str) or not reply:
        raise LLMError(f"Сценарий «{scenario}», шаг {index}: нужен tool или непустой reply")
    return MockStep(reply=reply)


def _compile_patterns(raw: Any, scenario: str) -> tuple[re.Pattern[str], ...]:
    patterns = [raw] if isinstance(raw, str) else raw
    if not isinstance(patterns, list) or not patterns:
        raise LLMError(f"Сценарий «{scenario}»: match должен быть строкой или списком строк")
    compiled: list[re.Pattern[str]] = []
    for item in patterns:
        if not isinstance(item, str) or not item:
            raise LLMError(f"Сценарий «{scenario}»: пустой шаблон match")
        try:
            compiled.append(re.compile(item, re.IGNORECASE))
        except re.error as error:
            raise LLMError(f"Сценарий «{scenario}»: некорректный шаблон: {error}") from error
    return tuple(compiled)


def parse_script(payload: Any) -> MockScript:
    """Проверить и собрать сценарии из разобранного JSON."""
    if not isinstance(payload, dict):
        raise LLMError("Файл сценариев mock должен содержать объект")
    raw_scenarios = payload.get("scenarios")
    if not isinstance(raw_scenarios, list) or not raw_scenarios:
        raise LLMError("В файле сценариев mock нет списка scenarios")

    scenarios: list[MockScenario] = []
    for position, raw in enumerate(raw_scenarios, start=1):
        if not isinstance(raw, dict):
            raise LLMError(f"Сценарий #{position}: ожидался объект")
        name = str(raw.get("name") or f"scenario-{position}")
        patterns = _compile_patterns(raw.get("match"), name)
        raw_steps = raw.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise LLMError(f"Сценарий «{name}»: нужен непустой список steps")
        steps = tuple(_parse_step(item, name, index) for index, item in enumerate(raw_steps, 1))
        scenarios.append(MockScenario(name, patterns, steps))

    fallback = payload.get("fallback")
    reply = fallback.get("reply") if isinstance(fallback, dict) else None
    if not isinstance(reply, str) or not reply:
        reply = "Mock-бэкенд: подходящий сценарий не найден."
    return MockScript(tuple(scenarios), reply)


def load_script(path: Path) -> MockScript:
    """Прочитать сценарии mock-бэкенда из JSON-файла."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise LLMError(f"Не удалось прочитать сценарии mock ({path}): {error}") from error
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise LLMError(f"Некорректный JSON сценариев mock ({path}): {error}") from error
    return parse_script(payload)


def _fill(value: Any, match: re.Match[str] | None) -> Any:
    """Подставить группы шаблона в аргументы: ``{1}`` или ``{name}``."""
    if match is None:
        return value
    if isinstance(value, str):

        def replace(placeholder: re.Match[str]) -> str:
            key = placeholder.group(1)
            if key.isdigit():
                try:
                    return match.group(int(key)) or ""
                except IndexError:
                    return placeholder.group(0)
            return match.groupdict().get(key) or ""

        return _PLACEHOLDER_RE.sub(replace, value)
    if isinstance(value, list):
        return [_fill(item, match) for item in value]
    if isinstance(value, dict):
        return {key: _fill(item, match) for key, item in value.items()}
    return value


def _last_user_message(messages: Sequence[Message]) -> str:
    for message in reversed(messages):
        if message.role == "user":
            return message.content
    return ""


class MockBackend:
    """Отвечает по сценариям из JSON: шаг за шагом, без модели.

    Сценарий выбирается по первому сообщению пользователя в запросе, дальше
    шаги выдаются по порядку. Это позволяет проверить цикл, подтверждения и
    отчёты детерминированно.
    """

    def __init__(
        self,
        scenarios_path: str | Path | None = None,
        *,
        script: MockScript | None = None,
    ) -> None:
        self.path = Path(scenarios_path) if scenarios_path else DEFAULT_SCENARIOS_PATH
        self.script = script if script is not None else load_script(self.path)
        self._steps: tuple[MockStep, ...] = ()
        self._index = 0
        self._match: re.Match[str] | None = None
        self._selected = False

    def reset(self) -> None:
        """Забыть выбранный сценарий и начать с первого шага."""
        self._steps = ()
        self._index = 0
        self._match = None
        self._selected = False

    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, Any]],
    ) -> LLMResponse:
        """Выдать очередной шаг выбранного сценария."""
        if not self._selected:
            self._select(_last_user_message(messages))
        if self._index < len(self._steps):
            step = self._steps[self._index]
            self._index += 1
            if step.tool is not None:
                return LLMResponse(
                    tool_calls=(
                        ToolCall(step.tool, _fill(step.arguments, self._match)),
                    )
                )
            return LLMResponse(content=_fill(step.reply, self._match))
        return LLMResponse(content="Готово.")

    def _select(self, request: str) -> None:
        self._selected = True
        for scenario in self.script.scenarios:
            match = scenario.match(request)
            if match is not None:
                logger.debug("MockBackend: выбран сценарий %s", scenario.name)
                self._steps = scenario.steps
                self._match = match
                return
        logger.debug("MockBackend: сценарий не найден, используется fallback")
        self._steps = (MockStep(reply=self.script.fallback),)


def create_backend(name: str, *, scenarios_path: str | Path | None = None) -> LLMBackend:
    """Создать LLM-бэкенд по имени из настроек."""
    if name == "mock":
        return MockBackend(scenarios_path)
    if name in ("llama_cpp", "ollama"):
        raise BackendNotImplementedError(
            f"Бэкенд «{name}» ещё не реализован: это этап 5. "
            "Выберите Mock в настройках."
        )
    raise LLMError(f"Неизвестный LLM-бэкенд: {name}")
