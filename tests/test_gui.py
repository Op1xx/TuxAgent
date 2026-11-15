"""Offscreen smoke-тесты GUI."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFontDatabase
from PyQt6.QtWidgets import QApplication, QDialog

from agent import Agent
from app import apply_stylesheet, create_application
from config import AppSettings
from confirm_dialog import ConfirmDialog
from distro import DistroFamily, DistroInfo
from llm import MockBackend, MockScenario, MockScript, MockStep
from main_window import MainWindow, TaskItem, build_agent
from safety import PathPolicy, RiskDecision, RiskLevel
from settings_dialog import SettingsDialog
from tools import DeleteTool, ListDirTool, Tool, ToolRegistry

pytestmark = pytest.mark.gui


@pytest.fixture
def settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        data_dir=str(tmp_path / "data"),
        models_dir=str(tmp_path / "models"),
    )


def agent_factory(
    *steps: MockStep,
    pattern: str = ".*",
    tool: Tool | None = None,
):
    """Собрать фабрику агента со сценарием вместо реальной модели."""

    def factory(_settings: AppSettings) -> Agent:
        script = MockScript(
            (
                MockScenario(
                    "gui",
                    (re.compile(pattern),),
                    tuple(steps) or (MockStep(reply="Готово."),),
                ),
            ),
            fallback="Готово.",
        )
        registry = ToolRegistry([tool] if tool is not None else [])
        return Agent(MockBackend(script=script), registry)

    return factory


def wait_for_agent(qtbot, window: MainWindow) -> None:
    """Дождаться, пока фоновая фаза цикла завершится."""
    qtbot.waitUntil(
        lambda: window.statusText.property("state") in {"done", "error"},
        timeout=5000,
    )


def test_application_and_stylesheet(qapp: QApplication, settings: AppSettings) -> None:
    app = create_application([], settings)

    assert app is qapp
    assert "#TuxAgentWindow" in app.styleSheet()
    assert "QDialog#SettingsDialog" in app.styleSheet()
    assert "QDialog#ConfirmDialog" in app.styleSheet()
    assert "IBM Plex Mono" in QFontDatabase.families()


def test_light_theme(qapp: QApplication, settings: AppSettings) -> None:
    light = AppSettings(
        data_dir=settings.data_dir,
        models_dir=settings.models_dir,
        theme="light",
    )

    apply_stylesheet(qapp, light)

    assert "#TuxAgentWindow { background: #f7f5fa; }" in qapp.styleSheet()
    assert "QFrame#tasksPanel" in qapp.styleSheet()
    assert "QDialog#ConfirmDialog" in qapp.styleSheet()
    apply_stylesheet(qapp, settings)


def test_main_window_smoke(qtbot, qapp: QApplication, settings: AppSettings) -> None:
    apply_stylesheet(qapp, settings)
    factory = agent_factory(MockStep(reply="Интерфейс работает."))
    window = MainWindow(settings, agent_factory=factory)
    qtbot.addWidget(window)
    window.show()

    assert window.minimumWidth() == 760

    assert window.isVisible()
    assert window.stack.currentIndex() == 0
    assert window.statusText.property("state") == "idle"

    window.promptInput.setPlainText("Проверка интерфейса")
    qtbot.mouseClick(window.promptSend, Qt.MouseButton.LeftButton)
    wait_for_agent(qtbot, window)

    assert window.stack.currentIndex() == 1
    assert window.transcriptLayout.count() >= 3
    assert window.statusText.property("state") == "done"
    assert window.promptSend.isEnabled()

    window.set_status("error", "Тестовая ошибка")
    assert window.statusText.property("state") == "error"
    assert window.statusText.text() == "Тестовая ошибка"

    window.reset_session()
    assert window.stack.currentIndex() == 0
    assert window.transcriptLayout.count() == 1


def test_confirm_dialog(qtbot, qapp: QApplication, settings: AppSettings) -> None:
    apply_stylesheet(qapp, settings)
    plan = SimpleNamespace(
        summary="Удалить пакет vlc",
        reversible=False,
        decision=RiskDecision(RiskLevel.CONFIRM, "Удаление пакета необратимо"),
        preview="pacman -R --noconfirm vlc",
    )
    dialog = ConfirmDialog.for_plan(plan)
    qtbot.addWidget(dialog)

    assert dialog.confirmTitle.text() == "Требуется подтверждение"
    assert "Удалить пакет vlc" in dialog.targetLabel.text()
    assert "Нет" in dialog.reversibilityLabel.text()
    assert "необратимо" in dialog.riskLabel.text()
    assert dialog.previewLabel.isVisibleTo(dialog)

    dialog.executeBtn.click()
    assert dialog.result() == QDialog.DialogCode.Accepted

    dialog2 = ConfirmDialog.for_plan(plan)
    qtbot.addWidget(dialog2)
    dialog2.cancelBtn.click()
    assert dialog2.result() == QDialog.DialogCode.Rejected


def test_confirm_dialog_ask_skips_safe_and_denied_plans(
    qtbot, qapp: QApplication, settings: AppSettings
) -> None:
    apply_stylesheet(qapp, settings)
    safe = SimpleNamespace(
        summary="Установить пакет",
        reversible=True,
        decision=RiskDecision(RiskLevel.SAFE, "Установка разрешена"),
        preview="",
    )
    denied = SimpleNamespace(
        summary="Удалить /etc",
        reversible=False,
        decision=RiskDecision(RiskLevel.DENY, "Защищённый путь"),
        preview="",
    )

    assert ConfirmDialog.ask(safe) is True
    assert ConfirmDialog.ask(denied) is False


def test_settings_dialog(qtbot, qapp: QApplication, settings: AppSettings) -> None:
    apply_stylesheet(qapp, settings)
    dialog = SettingsDialog(settings)
    qtbot.addWidget(dialog)

    assert dialog.distroMode.currentData() == "auto"
    assert not dialog.distroOverride.isEnabled()
    assert dialog.themeCombo.currentData() == "dark"

    dialog.distroMode.setCurrentIndex(dialog.distroMode.findData("manual"))
    dialog.distroOverride.setCurrentIndex(dialog.distroOverride.findData("arch"))
    dialog.backendCombo.setCurrentIndex(dialog.backendCombo.findData("llama_cpp"))
    dialog.modelPath.setText("/tmp/model.gguf")
    dialog.dataDir.setText("/tmp/tuxagent-data")
    dialog.modelsDir.setText("/tmp/tuxagent-models")
    dialog.themeCombo.setCurrentIndex(dialog.themeCombo.findData("light"))

    result = dialog.result_settings()

    assert dialog.distroOverride.isEnabled()
    assert dialog.modelPath.isEnabled()
    assert result.distro_mode == "manual"
    assert result.distro_override == "arch"
    assert result.llm_backend == "llama_cpp"
    assert result.model_path == "/tmp/model.gguf"
    assert result.data_dir == "/tmp/tuxagent-data"
    assert result.models_dir == "/tmp/tuxagent-models"
    assert result.theme == "light"


def test_settings_dialog_shows_detected_system(
    qtbot, qapp: QApplication, settings: AppSettings
) -> None:
    apply_stylesheet(qapp, settings)
    dialog = SettingsDialog(settings)
    qtbot.addWidget(dialog)

    assert "Система:" in dialog.distroHint.text()
    assert "семейство:" in dialog.distroHint.text()


def test_settings_dialog_warns_about_distro_mismatch(
    qtbot, qapp: QApplication, settings: AppSettings, monkeypatch
) -> None:
    import settings_dialog as settings_dialog_module

    monkeypatch.setattr(
        settings_dialog_module,
        "detect_distro",
        lambda: DistroInfo(id="arch", family=DistroFamily.ARCH),
    )
    dialog = SettingsDialog(settings)
    qtbot.addWidget(dialog)

    dialog.distroMode.setCurrentIndex(dialog.distroMode.findData("manual"))
    dialog.distroOverride.setCurrentIndex(dialog.distroOverride.findData("debian"))

    assert "не совпадает" in dialog.distroHint.text()
    assert "Arch" in dialog.distroHint.text() or "arch" in dialog.distroHint.text()


def test_main_window_builds_distro_warning(
    qtbot, qapp: QApplication, settings: AppSettings, monkeypatch
) -> None:
    import main_window as main_window_module

    monkeypatch.setattr(
        main_window_module,
        "detect_distro",
        lambda: DistroInfo(id="arch", family=DistroFamily.ARCH),
    )
    manual = AppSettings(
        distro_mode="manual",
        distro_override="debian",
        data_dir=settings.data_dir,
        models_dir=settings.models_dir,
    )
    window = MainWindow(manual)
    qtbot.addWidget(window)

    warning = window._distro_warning()
    assert warning is not None
    assert "не подойти" in warning


def test_new_chat_button_creates_chat(qtbot, qapp: QApplication, settings: AppSettings) -> None:
    apply_stylesheet(qapp, settings)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    window.show()

    assert len(window._chats) == 0

    window.newChatBtn.click()

    assert len(window._chats) == 1
    assert window._chats[0].name == "Новый чат"
    assert window._active_chat() is window._chats[0]
    assert len(window._chat_items) == 1


def test_chats_are_created_and_persisted(
    qtbot, qapp: QApplication, settings: AppSettings
) -> None:
    apply_stylesheet(qapp, settings)
    factory = agent_factory(MockStep(reply="Готово."))
    window = MainWindow(settings, agent_factory=factory)
    qtbot.addWidget(window)
    window.show()

    assert len(window._chats) == 0
    assert window.stack.currentIndex() == 0

    window.promptInput.setPlainText("Проверить VLC")
    qtbot.mouseClick(window.promptSend, Qt.MouseButton.LeftButton)
    wait_for_agent(qtbot, window)

    first_chat = window._active_chat()
    assert first_chat is not None
    assert first_chat.name == "Проверить VLC"
    assert len(first_chat.messages) >= 2
    assert len(window._chat_items) == 1

    window.newChatBtn.click()
    assert len(window._chats) == 2
    assert window._active_chat().name == "Новый чат"
    assert len(window._chat_items) == 2

    window.promptInput.setPlainText("Собрать проект на Rust")
    qtbot.mouseClick(window.promptSend, Qt.MouseButton.LeftButton)
    wait_for_agent(qtbot, window)
    assert window._chats[0].name == "Собрать проект на Rust"

    window._activate_chat(first_chat.id)
    assert window.stack.currentIndex() == 1
    assert window.transcriptLayout.count() >= 3

    store_path = Path(settings.data_dir) / "chats.json"
    assert store_path.is_file()
    saved_names = [chat.name for chat in window._chats]
    window.close()

    reloaded = MainWindow(settings)
    qtbot.addWidget(reloaded)
    assert [chat.name for chat in reloaded._chats] == saved_names
    assert reloaded._active_chat().name == "Собрать проект на Rust"


def test_delete_chat(qtbot, qapp: QApplication, settings: AppSettings) -> None:
    apply_stylesheet(qapp, settings)
    factory = agent_factory(MockStep(reply="Готово."))
    window = MainWindow(settings, agent_factory=factory)
    qtbot.addWidget(window)
    window.show()

    window.promptInput.setPlainText("Первый чат")
    qtbot.mouseClick(window.promptSend, Qt.MouseButton.LeftButton)
    wait_for_agent(qtbot, window)
    first_chat = window._active_chat()

    window.newChatBtn.click()
    window.promptInput.setPlainText("Второй чат")
    qtbot.mouseClick(window.promptSend, Qt.MouseButton.LeftButton)
    wait_for_agent(qtbot, window)
    second_chat = window._active_chat()

    assert len(window._chats) == 2
    assert window._chat_items[0].chatDelete.toolTip() == "Удалить чат"

    window._remove_chat(second_chat.id)

    assert len(window._chats) == 1
    assert window._active_chat().id == first_chat.id
    assert window.stack.currentIndex() == 1
    assert window.transcriptLayout.count() >= 3

    window._remove_chat(first_chat.id)

    assert len(window._chats) == 0
    assert window._active_chat() is None
    assert len(window._chat_items) == 0
    assert window.stack.currentIndex() == 0
    assert not window.sidebar.isVisible()

    window.close()
    reloaded = MainWindow(settings)
    qtbot.addWidget(reloaded)
    assert len(reloaded._chats) == 0

    reloaded.promptInput.setPlainText("Первый чат после удаления")
    qtbot.mouseClick(reloaded.promptSend, Qt.MouseButton.LeftButton)

    assert len(reloaded._chats) == 1
    assert reloaded._active_chat().name == "Первый чат после удаления"


def test_tasks_panel(qtbot, qapp: QApplication, settings: AppSettings) -> None:
    apply_stylesheet(qapp, settings)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    window.resize(1200, 700)
    window.show()

    assert not window.tasksPanel.isVisible()

    window.tasksBtn.click()
    assert window.tasksPanel.isVisible()
    assert window.sidebar.isVisible()

    window.set_tasks(
        [
            TaskItem("Найти файл", "running", "~/Downloads"),
            TaskItem("Проверить результат", "done"),
        ]
    )

    assert len(window._task_widgets) == 2
    assert not window.tasksEmpty.isVisible()
    assert window._task_widgets[0].property("status") == "running"

    window.set_tasks([])
    assert window.tasksEmpty.isVisible()

    window.resize(900, 600)
    window.sidebar.setVisible(True)
    window.tasksBtn.click()
    assert window.tasksPanel.isVisible()
    assert not window.sidebar.isVisible()


def test_agent_runs_tool_and_shows_report(
    qtbot, qapp: QApplication, settings: AppSettings, tmp_path: Path
) -> None:
    apply_stylesheet(qapp, settings)
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "a.txt").write_text("x", encoding="utf-8")
    factory = agent_factory(
        MockStep(tool="list_dir", arguments={"path": str(folder)}),
        MockStep(reply="Список показан."),
        pattern="покажи папку",
        tool=ListDirTool(PathPolicy()),
    )
    window = MainWindow(settings, agent_factory=factory)
    qtbot.addWidget(window)
    window.show()

    window.promptInput.setPlainText("покажи папку")
    qtbot.mouseClick(window.promptSend, Qt.MouseButton.LeftButton)
    wait_for_agent(qtbot, window)

    chat = window._active_chat()
    assert chat is not None
    kinds = [message.kind for message in chat.messages]
    assert kinds[0] == "command"
    assert "thought" in kinds
    assert "answer" in kinds
    assert "Выполнено" in chat.messages[-1].text
    assert len(window._task_widgets) == 1
    assert window._task_widgets[0].property("status") == "done"


def test_agent_deletes_after_confirmation(
    qtbot,
    qapp: QApplication,
    settings: AppSettings,
    tmp_path: Path,
    monkeypatch,
) -> None:
    import main_window as main_window_module

    monkeypatch.setattr(
        main_window_module.ConfirmDialog,
        "ask",
        lambda plan, parent=None: True,
    )
    apply_stylesheet(qapp, settings)
    target = tmp_path / "old.txt"
    target.write_text("x", encoding="utf-8")
    factory = agent_factory(
        MockStep(tool="delete", arguments={"path": str(target)}),
        MockStep(reply="Файл удалён."),
        pattern="удали файл",
        tool=DeleteTool(PathPolicy()),
    )
    window = MainWindow(settings, agent_factory=factory)
    qtbot.addWidget(window)
    window.show()

    window.promptInput.setPlainText("удали файл")
    qtbot.mouseClick(window.promptSend, Qt.MouseButton.LeftButton)
    wait_for_agent(qtbot, window)

    assert not target.exists()
    chat = window._active_chat()
    assert chat is not None
    assert any(message.text == "Файл удалён." for message in chat.messages)
    assert window.statusText.property("state") == "done"


def test_agent_keeps_file_when_confirmation_cancelled(
    qtbot,
    qapp: QApplication,
    settings: AppSettings,
    tmp_path: Path,
    monkeypatch,
) -> None:
    import main_window as main_window_module

    monkeypatch.setattr(
        main_window_module.ConfirmDialog,
        "ask",
        lambda plan, parent=None: False,
    )
    apply_stylesheet(qapp, settings)
    target = tmp_path / "keep.txt"
    target.write_text("x", encoding="utf-8")
    factory = agent_factory(
        MockStep(tool="delete", arguments={"path": str(target)}),
        MockStep(reply="Отменено."),
        pattern="удали файл",
        tool=DeleteTool(PathPolicy()),
    )
    window = MainWindow(settings, agent_factory=factory)
    qtbot.addWidget(window)
    window.show()

    window.promptInput.setPlainText("удали файл")
    qtbot.mouseClick(window.promptSend, Qt.MouseButton.LeftButton)
    wait_for_agent(qtbot, window)

    assert target.exists()
    assert window.statusText.text() == "Отменено"
    chat = window._active_chat()
    assert chat is not None
    assert any("отменён" in message.text for message in chat.messages)


def test_unavailable_backend_is_reported(
    qtbot, qapp: QApplication, settings: AppSettings
) -> None:
    apply_stylesheet(qapp, settings)
    settings = AppSettings(
        llm_backend="llama_cpp",
        data_dir=settings.data_dir,
        models_dir=settings.models_dir,
    )
    window = MainWindow(settings)
    qtbot.addWidget(window)
    window.show()

    window.promptInput.setPlainText("Проверка бэкенда")
    qtbot.mouseClick(window.promptSend, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(
        lambda: window.statusText.property("state") == "error",
        timeout=3000,
    )

    chat = window._active_chat()
    assert chat is not None
    assert any("Агент недоступен" in message.text for message in chat.messages)
    assert window.promptSend.isEnabled()


def test_build_agent_blocks_package_ops_on_distro_mismatch(
    qtbot, settings: AppSettings, monkeypatch
) -> None:
    import main_window as main_window_module

    monkeypatch.setattr(
        main_window_module,
        "detect_distro",
        lambda: DistroInfo(id="arch", family=DistroFamily.ARCH),
    )
    manual = AppSettings(
        distro_mode="manual",
        distro_override="debian",
        data_dir=settings.data_dir,
        models_dir=settings.models_dir,
    )

    agent = build_agent(manual)

    assert agent.package_ops_allowed is False
