"""Тесты агентного цикла: шаги, подтверждение, отчёты и ограничения."""

from __future__ import annotations

import re
import time
from typing import Any

import pytest

from agent import Agent, AgentError, AgentTurn
from llm import Message, MockBackend, MockScenario, MockScript, MockStep
from package_managers import Command, CommandResult
from safety import PathPolicy, RiskDecision, RiskLevel
from tools import (
    DeleteTool,
    FindFilesTool,
    PackageInstallTool,
    PackageIsInstalledTool,
    Tool,
    ToolPlan,
    ToolRegistry,
    ToolResult,
)


def script(*steps: MockStep, match: str = ".*") -> MockBackend:
    scenario = MockScenario("test", (re.compile(match),), tuple(steps))
    return MockBackend(script=MockScript((scenario,), fallback="fallback"))


class RecordingTool(Tool):
    """Инструмент для тестов: записывает вызовы и умеет падать и тормозить."""

    name = "record"
    description = "Тестовый инструмент."
    parameters = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        policy: PathPolicy,
        *,
        safe: bool = True,
        fail_times: int = 0,
        delay: float = 0.0,
    ) -> None:
        super().__init__(policy)
        self.calls: list[str] = []
        self.safe = safe
        self.fail_times = fail_times
        self.delay = delay

    def plan(self, arguments: Any) -> ToolPlan:
        args = self.validate(arguments)
        level = RiskLevel.SAFE if self.safe else RiskLevel.CONFIRM
        return ToolPlan(
            self.name,
            args,
            RiskDecision(level, "Тестовое решение"),
            f"Записать {args['value']}",
        )

    def _execute(self, arguments: dict[str, Any], *, confirmed: bool) -> ToolResult:
        self.calls.append(arguments["value"])
        if self.delay:
            time.sleep(self.delay)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("сбой инструмента")
        return ToolResult(True, f"Записано: {arguments['value']}")


class PackageInstallStub(RecordingTool):
    name = "package_install"


def make_agent(
    backend: MockBackend,
    tool: Tool | None = None,
    **kwargs: Any,
) -> Agent:
    registry = ToolRegistry([tool] if tool is not None else [])
    return Agent(backend, registry, **kwargs)


def control_kinds(turn: AgentTurn) -> list[str]:
    return [event.kind for event in turn.events]


def test_agent_runs_safe_tool_and_builds_report() -> None:
    tool = RecordingTool(PathPolicy())
    backend = script(
        MockStep(tool="record", arguments={"value": "x"}),
        MockStep(reply="Готово"),
    )
    agent = make_agent(backend, tool)

    turn = agent.start("сделай x")

    assert turn.done
    assert {"status", "tasks", "message"} <= set(control_kinds(turn))
    assert any(event.message_kind == "answer" for event in turn.events)
    assert tool.calls == ["x"]
    report = turn.report
    assert report is not None
    assert report.answer == "Готово"
    assert report.records[0].ok
    assert "Выполнено" in report.as_text()
    assert "Действий не было" not in report.as_text()


def test_agent_pauses_for_confirmation_and_resumes() -> None:
    tool = RecordingTool(PathPolicy(), safe=False)
    backend = script(
        MockStep(tool="record", arguments={"value": "y"}),
        MockStep(reply="Готово"),
    )
    agent = make_agent(backend, tool)

    first = agent.start("сделай y")
    assert first.pending is not None
    assert not first.done
    assert tool.calls == []
    assert any(event.state == "waiting" for event in first.events)

    second = agent.resume(True)
    assert second.done
    assert tool.calls == ["y"]
    assert second.report is not None
    assert second.report.records[0].ok


def test_agent_cancels_confirmation() -> None:
    tool = RecordingTool(PathPolicy(), safe=False)
    backend = script(MockStep(tool="record", arguments={"value": "z"}))
    agent = make_agent(backend, tool)

    first = agent.start("сделай z")
    assert first.pending is not None

    second = agent.resume(False)

    assert second.done
    assert tool.calls == []
    assert second.report is not None
    assert second.report.cancelled
    assert "отменён" in second.report.as_text()


def test_agent_repairs_unknown_tool() -> None:
    tool = RecordingTool(PathPolicy())
    backend = script(
        MockStep(tool="rm_rf", arguments={}),
        MockStep(tool="record", arguments={"value": "ok"}),
        MockStep(reply="Готово"),
    )
    agent = make_agent(backend, tool)

    turn = agent.start("сделай что-нибудь")

    assert turn.done
    assert tool.calls == ["ok"]
    assert any("Tool call отклонён" in event.text for event in turn.events)
    assert turn.report is not None
    assert not turn.report.problems


def test_agent_stops_after_repair_limit() -> None:
    backend = script(
        MockStep(tool="rm_rf", arguments={}),
        MockStep(tool="rm_rf", arguments={}),
        MockStep(tool="rm_rf", arguments={}),
    )
    agent = make_agent(backend, None, max_repairs=1)

    turn = agent.start("сделай что-нибудь")

    assert turn.done
    assert turn.report is not None
    assert any("Не удалось исправить" in problem for problem in turn.report.problems)


def test_agent_blocks_mutating_package_tools_on_distro_mismatch() -> None:
    tool = PackageInstallStub(PathPolicy())
    backend = script(
        MockStep(tool="package_install", arguments={"value": "firefox"}),
        MockStep(reply="Готово"),
    )
    agent = make_agent(backend, tool, package_ops_allowed=False)

    turn = agent.start("поставь firefox")

    assert turn.done
    assert tool.calls == []
    assert turn.report is not None
    assert any("Профиль дистрибутива" in record.message for record in turn.report.records)
    assert not turn.report.records[0].ok


def test_agent_stops_at_step_limit() -> None:
    tool = RecordingTool(PathPolicy())
    steps = [
        MockStep(tool="record", arguments={"value": f"v{index}"}) for index in range(5)
    ]
    backend = script(*steps)
    agent = make_agent(backend, tool, max_steps=2)

    turn = agent.start("сделай много")

    assert turn.done
    assert len(tool.calls) == 2
    assert turn.report is not None
    assert any("лимит шагов" in problem for problem in turn.report.problems)


def test_agent_retries_safe_tool_once() -> None:
    tool = RecordingTool(PathPolicy(), fail_times=1)
    backend = script(
        MockStep(tool="record", arguments={"value": "retry"}),
        MockStep(reply="Готово"),
    )
    agent = make_agent(backend, tool)

    turn = agent.start("сделай retry")

    assert turn.done
    assert tool.calls == ["retry", "retry"]
    assert turn.report is not None
    assert turn.report.records[0].ok


def test_agent_does_not_retry_confirmed_tool() -> None:
    tool = RecordingTool(PathPolicy(), safe=False, fail_times=1)
    backend = script(MockStep(tool="record", arguments={"value": "once"}))
    agent = make_agent(backend, tool)

    first = agent.start("сделай once")
    assert first.pending is not None
    second = agent.resume(True)

    assert tool.calls == ["once"]
    assert second.report is not None
    assert not second.report.records[0].ok


def test_agent_reports_tool_timeout() -> None:
    tool = RecordingTool(PathPolicy(), delay=0.3)
    backend = script(
        MockStep(tool="record", arguments={"value": "slow"}),
        MockStep(reply="Готово"),
    )
    agent = make_agent(backend, tool, tool_timeout=0.05)

    turn = agent.start("сделай медленно")

    assert turn.done
    assert turn.report is not None
    assert "не завершился" in turn.report.records[0].message
    assert not turn.report.records[0].ok


def test_agent_records_denied_path_without_executing() -> None:
    registry = ToolRegistry([DeleteTool(PathPolicy())])
    backend = script(
        MockStep(tool="delete", arguments={"path": "/etc/passwd"}),
        MockStep(reply="Нельзя"),
    )
    agent = Agent(backend, registry)

    turn = agent.start("удали /etc/passwd")

    assert turn.done
    assert turn.report is not None
    assert not turn.report.records[0].ok
    assert "запрещено" in turn.report.records[0].message


def test_agent_rejects_second_start() -> None:
    agent = make_agent(script(MockStep(reply="Готово")))

    agent.start("первый")

    with pytest.raises(AgentError, match="уже запущен"):
        agent.start("второй")


def test_agent_resumes_only_after_confirmation() -> None:
    agent = make_agent(script(MockStep(reply="Готово")))

    with pytest.raises(AgentError, match="Нет действия"):
        agent.resume(True)


def test_backend_receives_tool_results() -> None:
    """После выполнения инструмента цикл отправляет результат обратно модели."""

    class InspectingBackend(MockBackend):
        def __init__(self) -> None:
            super().__init__(
                script=MockScript(
                    (
                        MockScenario(
                            "inspect",
                            (re.compile(".*"),),
                            (
                                MockStep(tool="record", arguments={"value": "v"}),
                                MockStep(reply="Готово"),
                            ),
                        ),
                    ),
                    fallback="fallback",
                )
            )
            self.seen: list[list[Message]] = []

        def complete(self, messages, tools):  # type: ignore[no-untyped-def]
            self.seen.append(list(messages))
            return super().complete(messages, tools)

    backend = InspectingBackend()
    tool = RecordingTool(PathPolicy())
    agent = make_agent(backend, tool)  # type: ignore[arg-type]

    agent.start("сделай v")

    last_messages = backend.seen[-1]
    assert last_messages[0].role == "user"
    assert any(
        message.role == "tool" and "Записано: v" in message.content
        for message in last_messages
    )


class FailingPackageManager:
    """Пакетный менеджер, который не может выполнить установку."""

    def build_install(self, packages, dry_run: bool = False):
        from package_managers import Command

        return Command(("fake-install", *packages), requires_root=True, dry_run=dry_run)

    def install(self, packages, dry_run: bool = False):
        from package_managers import PackageManagerError

        raise PackageManagerError("нет доступа к репозиторию")


def test_agent_reports_package_manager_failure() -> None:
    from tools import PackageInstallTool

    manager = FailingPackageManager()
    tool = PackageInstallTool(PathPolicy(), manager)  # type: ignore[arg-type]
    backend = script(
        MockStep(tool="package_install", arguments={"packages": ["firefox"]}),
        MockStep(reply="Не удалось."),
    )
    agent = make_agent(backend, tool)

    turn = agent.start("поставь firefox")

    assert turn.done
    assert turn.report is not None
    record = turn.report.records[0]
    assert not record.ok
    assert "Установка не удалась" in record.message
    assert "Не выполнено" in turn.report.as_text()


class FakePackageManager:
    """Пакетный менеджер без subprocess: запоминает установленные пакеты."""

    def __init__(self) -> None:
        self.installed: set[str] = set()
        self.calls: list[tuple[str, ...]] = []

    def build_install(self, packages: list[str], dry_run: bool = False) -> Command:
        return Command(("fake-install", *packages), requires_root=True, dry_run=dry_run)

    def is_installed(self, package: str) -> bool:
        return package in self.installed

    def install(self, packages: list[str], dry_run: bool = False) -> CommandResult:
        self.calls.append(tuple(packages))
        self.installed.update(packages)
        return CommandResult(("fake-install", *packages), 0)


def test_agent_installs_package_end_to_end() -> None:
    manager = FakePackageManager()
    policy = PathPolicy()
    registry = ToolRegistry(
        [
            PackageIsInstalledTool(policy, manager),
            PackageInstallTool(policy, manager),
        ]
    )
    backend = script(
        MockStep(tool="package_is_installed", arguments={"package": "firefox"}),
        MockStep(tool="package_install", arguments={"packages": ["firefox"]}),
        MockStep(reply="Пакет firefox установлен."),
    )
    agent = Agent(backend, registry)

    turn = agent.start("поставь firefox")

    assert turn.done
    assert manager.calls == [("firefox",)]
    assert "firefox" in manager.installed
    assert turn.report is not None
    assert [record.ok for record in turn.report.records] == [True, True]
    assert "Выполнено" in turn.report.as_text()
    assert turn.report.answer == "Пакет firefox установлен."


def test_agent_finds_files_end_to_end(tmp_path) -> None:
    (tmp_path / "report.pdf").write_text("x", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    backend = script(
        MockStep(
            tool="find_files",
            arguments={
                "path": str(tmp_path),
                "pattern": "*.pdf",
                "recursive": False,
            },
        ),
        MockStep(reply="Найден 1 файл."),
    )
    agent = make_agent(backend, FindFilesTool(PathPolicy()))

    turn = agent.start("найди *.pdf")

    assert turn.done
    assert turn.report is not None
    record = turn.report.records[0]
    assert record.ok
    assert record.message == "Найдено: 1"


def test_agent_repairs_invalid_arguments() -> None:
    tool = RecordingTool(PathPolicy())
    backend = script(
        MockStep(tool="record", arguments={}),
        MockStep(tool="record", arguments={"value": "fixed"}),
        MockStep(reply="Готово"),
    )
    agent = make_agent(backend, tool)

    turn = agent.start("сделай")

    assert turn.done
    assert tool.calls == ["fixed"]
    assert turn.report is not None
    assert turn.report.records[0].ok
