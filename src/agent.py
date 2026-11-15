"""Агентный цикл: планирование, выполнение шагов и проверка результата.

Цикл не зависит от Qt и конкретной модели: он получает :class:`LLMBackend`,
превращает tool calls в :class:`tools.ToolPlan`, проверяет риск, при
необходимости останавливается для подтверждения и возвращает события для GUI.

Подтверждение двухфазное: :meth:`Agent.start` возвращает ход с заполненным
``pending``, после ответа пользователя цикл продолжается через
:meth:`Agent.resume`. Так диалог живёт в главном потоке, а модель и инструменты
не блокируют интерфейс.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field

from llm import LLMBackend, LLMError, Message, ToolCall
from safety import RiskLevel
from tools import ToolError, ToolPlan, ToolRegistry, ToolResult

logger = logging.getLogger(__name__)

DEFAULT_MAX_STEPS = 8
DEFAULT_MAX_REPAIRS = 2
DEFAULT_TOOL_TIMEOUT = 60.0

# Изменяющие пакетные операции: при ручном профиле дистрибутива, который не
# совпадает с системой, их выполнять нельзя (см. AGENTS.md).
MUTATING_PACKAGE_TOOLS = frozenset(
    {
        "package_install",
        "package_remove",
        "package_update_index",
        "package_clean_cache",
    }
)


class AgentError(RuntimeError):
    """Нарушение порядка вызовов агентного цикла."""


class ToolTimeoutError(ToolError):
    """Инструмент не завершился за отведённое время."""


@dataclass(frozen=True, slots=True)
class PlanStep:
    """Шаг плана для панели задач."""

    title: str
    status: str = "pending"
    detail: str = ""


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """Событие цикла для GUI: статус, сообщение или обновление плана."""

    kind: str  # "status" | "message" | "tasks"
    text: str = ""
    state: str = ""
    message_kind: str = "info"
    tasks: tuple[PlanStep, ...] = ()


@dataclass(slots=True)
class AgentStepRecord:
    """Фактически выполненный (или отклонённый) шаг для отчёта."""

    tool: str
    summary: str
    ok: bool
    message: str


@dataclass(slots=True)
class AgentReport:
    """Итог запроса: что сделано, что не сделано и почему."""

    request: str
    answer: str = ""
    records: list[AgentStepRecord] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    cancelled: bool = False

    def as_text(self) -> str:
        """Собрать короткий отчёт на русском."""
        lines: list[str] = []
        done = [record for record in self.records if record.ok]
        failed = [record for record in self.records if not record.ok]
        if done:
            lines.append("Выполнено:")
            lines.extend(f"• {record.tool} — {record.message}" for record in done)
        if failed:
            lines.append("Не выполнено:")
            lines.extend(f"• {record.tool} — {record.message}" for record in failed)
        if self.problems:
            lines.append("Причина остановки:")
            lines.extend(f"• {problem}" for problem in self.problems)
        if self.cancelled:
            lines.append("Запрос отменён пользователем.")
        if not lines:
            lines.append("Действий не было: модель ответила без вызова инструментов.")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class AgentTurn:
    """Один ход цикла: события и либо ожидание подтверждения, либо итог."""

    events: tuple[AgentEvent, ...] = ()
    pending: ToolPlan | None = None
    report: AgentReport | None = None

    @property
    def done(self) -> bool:
        """Цикл завершён и отчёт готов."""
        return self.pending is None and self.report is not None


class ToolExecutor:
    """Выполнение инструмента с таймаутом.

    Таймаут не убивает уже запущенный поток: инструменты ограничены по времени
    на уровне ``CommandRunner``, а этот слой страхует цикл от зависания.
    """

    def __init__(self, timeout: float | None = DEFAULT_TOOL_TIMEOUT) -> None:
        self.timeout = timeout

    def run(
        self,
        registry: ToolRegistry,
        plan: ToolPlan,
        *,
        confirmed: bool,
    ) -> ToolResult:
        """Выполнить план, вернув результат или подняв ошибку таймаута."""
        if not self.timeout or self.timeout <= 0:
            return registry.execute(plan.tool, plan, confirmed=confirmed)

        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tuxagent-tool")
        future = pool.submit(registry.execute, plan.tool, plan, confirmed=confirmed)
        try:
            return future.result(timeout=self.timeout)
        except FutureTimeout as error:
            raise ToolTimeoutError(
                f"{plan.tool} не завершился за {self.timeout:g} с"
            ) from error
        finally:
            pool.shutdown(wait=False)


def _render_call(call: ToolCall) -> str:
    return json.dumps(
        {"tool": call.name, "arguments": call.arguments},
        ensure_ascii=False,
        default=str,
    )


def _result_message(call: ToolCall, result: ToolResult) -> str:
    return json.dumps(
        {
            "tool": call.name,
            "ok": result.ok,
            "message": result.message,
            "data": result.data,
        },
        ensure_ascii=False,
        default=str,
    )


class Agent:
    """Планировщик и исполнитель: модель предлагает, код проверяет и делает."""

    def __init__(
        self,
        backend: LLMBackend,
        registry: ToolRegistry,
        *,
        max_steps: int = DEFAULT_MAX_STEPS,
        max_repairs: int = DEFAULT_MAX_REPAIRS,
        tool_timeout: float | None = DEFAULT_TOOL_TIMEOUT,
        package_ops_allowed: bool = True,
        executor: ToolExecutor | None = None,
    ) -> None:
        self.backend = backend
        self.registry = registry
        self.max_steps = max(1, max_steps)
        self.max_repairs = max(0, max_repairs)
        self.package_ops_allowed = package_ops_allowed
        self._executor = executor or ToolExecutor(tool_timeout)
        self._messages: list[Message] = []
        self._events: list[AgentEvent] = []
        self._plan_steps: list[PlanStep] = []
        self._report = AgentReport(request="")
        self._pending: ToolPlan | None = None
        self._pending_index: int | None = None
        self._repairs = 0
        self._started = False
        self._finished = False
        self._cancelled = False

    @property
    def finished(self) -> bool:
        """Цикл дошёл до отчёта."""
        return self._finished

    def cancel(self) -> None:
        """Попросить цикл остановиться перед следующим шагом."""
        self._cancelled = True

    def start(self, request: str) -> AgentTurn:
        """Начать запрос и выполнить шаги до подтверждения или итога."""
        if self._started:
            raise AgentError("Агент уже запущен: создайте новый экземпляр")
        text = request.strip()
        if not text:
            raise AgentError("Пустой запрос")
        self._started = True
        self.backend.reset()
        self._messages = [Message("user", text)]
        self._report = AgentReport(request=text)
        return self._advance()

    def resume(self, confirmed: bool) -> AgentTurn:
        """Продолжить цикл после ответа пользователя на подтверждение."""
        if self._pending is None:
            raise AgentError("Нет действия, ожидающего подтверждения")
        plan = self._pending
        index = self._pending_index if self._pending_index is not None else 0
        self._pending = None
        self._pending_index = None

        if not confirmed:
            self._report.cancelled = True
            self._report.answer = "Действие отменено пользователем."
            self._plan_steps[index] = PlanStep(plan.summary, "error", "Отменено пользователем")
            self._events.append(AgentEvent("tasks", tasks=tuple(self._plan_steps)))
            return self._finish()

        self._events.append(AgentEvent("status", text="Выполняю…", state="running"))
        result = self._execute(plan, confirmed=True)
        self._complete_step(index, plan, result)
        call = ToolCall(plan.tool, dict(plan.arguments))
        self._messages.append(Message("tool", _result_message(call, result)))
        return self._advance()

    def _advance(self) -> AgentTurn:
        while True:
            if self._cancelled:
                self._report.cancelled = True
                self._report.answer = "Запрос отменён."
                return self._finish()

            self._events.append(AgentEvent("status", text="Думаю…", state="thinking"))
            try:
                response = self.backend.complete(self._messages, self.registry.schemas())
            except LLMError as error:
                self._report.problems.append(f"Ошибка модели: {error}")
                return self._finish()

            if not response.tool_calls:
                self._report.answer = response.content.strip() or "Готово."
                return self._finish()

            for call in response.tool_calls:
                if self._cancelled:
                    self._report.cancelled = True
                    self._report.answer = "Запрос отменён."
                    return self._finish()
                self._messages.append(Message("assistant", _render_call(call)))
                control = self._handle_call(call)
                if control == "pause":
                    return self._turn(pending=self._pending)
                if control == "finished":
                    return self._finish()

    def _handle_call(self, call: ToolCall) -> str:
        if len(self._report.records) >= self.max_steps:
            self._report.problems.append(f"Достигнут лимит шагов ({self.max_steps})")
            return "finished"

        if call.name in MUTATING_PACKAGE_TOOLS and not self.package_ops_allowed:
            message = (
                "Профиль дистрибутива не совпадает с реальной системой: "
                "изменяющие пакетные операции заблокированы"
            )
            result = ToolResult(False, message)
            self._plan_steps.append(PlanStep(f"{call.name}: {message}", "error", message))
            self._report.records.append(AgentStepRecord(call.name, message, False, message))
            self._messages.append(Message("tool", _result_message(call, result)))
            self._events.append(AgentEvent("tasks", tasks=tuple(self._plan_steps)))
            self._events.append(
                AgentEvent("message", text=f"{call.name}: {message}", message_kind="thought")
            )
            return "continue"

        try:
            plan = self.registry.plan(call.name, call.arguments)
        except ToolError as error:
            return self._repair(call, error)

        index = len(self._plan_steps)
        self._plan_steps.append(PlanStep(plan.summary, "running"))
        self._events.append(AgentEvent("tasks", tasks=tuple(self._plan_steps)))

        if plan.denied:
            result = ToolResult(False, plan.decision.reason)
            self._complete_step(index, plan, result)
            self._messages.append(Message("tool", _result_message(call, result)))
            return "continue"

        if plan.requires_confirmation:
            self._pending = plan
            self._pending_index = index
            self._events.append(AgentEvent("status", text="Жду подтверждения", state="waiting"))
            return "pause"

        self._events.append(AgentEvent("status", text="Выполняю…", state="running"))
        result = self._execute(plan, confirmed=False)
        self._complete_step(index, plan, result)
        self._messages.append(Message("tool", _result_message(call, result)))
        return "continue"

    def _repair(self, call: ToolCall, error: Exception) -> str:
        self._repairs += 1
        self._events.append(
            AgentEvent("message", text=f"Tool call отклонён: {error}", message_kind="info")
        )
        if self._repairs > self.max_repairs:
            self._report.problems.append(
                f"Не удалось исправить tool call «{call.name}»: {error}"
            )
            return "finished"
        self._messages.append(
            Message(
                "user",
                f"Tool call «{call.name}» отклонён: {error}. "
                "Исправь аргументы и вызови инструмент снова.",
            )
        )
        return "continue"

    def _execute(self, plan: ToolPlan, *, confirmed: bool) -> ToolResult:
        attempts = 2 if plan.decision.level is RiskLevel.SAFE else 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                return self._executor.run(self.registry, plan, confirmed=confirmed)
            except Exception as error:  # ошибка уходит в отчёт, а не в GUI
                last_error = error
                logger.warning(
                    "Инструмент %s, попытка %d: %s", plan.tool, attempt + 1, error
                )
        return ToolResult(False, f"Ошибка выполнения {plan.tool}: {last_error}")

    def _complete_step(self, index: int, plan: ToolPlan, result: ToolResult) -> None:
        self._report.records.append(
            AgentStepRecord(plan.tool, plan.summary, result.ok, result.message)
        )
        status = "done" if result.ok else "error"
        self._plan_steps[index] = PlanStep(plan.summary, status, result.message)
        self._events.append(AgentEvent("tasks", tasks=tuple(self._plan_steps)))
        self._events.append(
            AgentEvent("message", text=f"{plan.tool}: {result.message}", message_kind="thought")
        )

    def _turn(
        self,
        *,
        pending: ToolPlan | None = None,
        report: AgentReport | None = None,
    ) -> AgentTurn:
        events = tuple(self._events)
        self._events.clear()
        return AgentTurn(events=events, pending=pending, report=report)

    def _finish(self) -> AgentTurn:
        report = self._report
        if report.answer:
            self._events.append(
                AgentEvent("message", text=report.answer, message_kind="answer")
            )
        self._events.append(
            AgentEvent("message", text=report.as_text(), message_kind="info")
        )
        if report.cancelled:
            state, title = "done", "Отменено"
        elif report.problems:
            state, title = "error", "Ошибка"
        else:
            state, title = "done", "Готово"
        self._events.append(AgentEvent("status", text=title, state=state))
        self._finished = True
        return self._turn(report=report)
