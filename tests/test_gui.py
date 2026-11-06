"""Offscreen smoke-тесты GUI."""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFontDatabase
from PyQt6.QtWidgets import QApplication

from app import apply_stylesheet, create_application
from config import AppSettings
from main_window import MainWindow, TaskItem
from settings_dialog import SettingsDialog

pytestmark = pytest.mark.gui


@pytest.fixture
def settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        data_dir=str(tmp_path / "data"),
        models_dir=str(tmp_path / "models"),
    )


def test_application_and_stylesheet(qapp: QApplication, settings: AppSettings) -> None:
    app = create_application([], settings)

    assert app is qapp
    assert "#TuxAgentWindow" in app.styleSheet()
    assert "QDialog#SettingsDialog" in app.styleSheet()
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
    apply_stylesheet(qapp, settings)


def test_main_window_smoke(qtbot, qapp: QApplication, settings: AppSettings) -> None:
    apply_stylesheet(qapp, settings)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    window.show()

    assert window.minimumWidth() == 760

    assert window.isVisible()
    assert window.stack.currentIndex() == 0
    assert window.statusText.property("state") == "idle"

    window.promptInput.setPlainText("Проверка интерфейса")
    qtbot.mouseClick(window.promptSend, Qt.MouseButton.LeftButton)

    assert window.stack.currentIndex() == 1
    assert window.transcriptLayout.count() == 3

    window.set_status("error", "Тестовая ошибка")
    assert window.statusText.property("state") == "error"
    assert window.statusText.text() == "Тестовая ошибка"

    window.reset_session()
    assert window.stack.currentIndex() == 0
    assert window.transcriptLayout.count() == 1


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
    window = MainWindow(settings)
    qtbot.addWidget(window)
    window.show()

    assert len(window._chats) == 0
    assert window.stack.currentIndex() == 0

    window.promptInput.setPlainText("Проверить VLC")
    qtbot.mouseClick(window.promptSend, Qt.MouseButton.LeftButton)

    first_chat = window._active_chat()
    assert first_chat is not None
    assert first_chat.name == "Проверить VLC"
    assert len(first_chat.messages) == 2
    assert len(window._chat_items) == 1

    window.newChatBtn.click()
    assert len(window._chats) == 2
    assert window._active_chat().name == "Новый чат"
    assert len(window._chat_items) == 2

    window.promptInput.setPlainText("Собрать проект на Rust")
    qtbot.mouseClick(window.promptSend, Qt.MouseButton.LeftButton)
    assert window._chats[0].name == "Собрать проект на Rust"

    window._activate_chat(first_chat.id)
    assert window.stack.currentIndex() == 1
    assert window.transcriptLayout.count() == 3

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
    window = MainWindow(settings)
    qtbot.addWidget(window)
    window.show()

    window.promptInput.setPlainText("Первый чат")
    qtbot.mouseClick(window.promptSend, Qt.MouseButton.LeftButton)
    first_chat = window._active_chat()

    window.newChatBtn.click()
    window.promptInput.setPlainText("Второй чат")
    qtbot.mouseClick(window.promptSend, Qt.MouseButton.LeftButton)
    second_chat = window._active_chat()

    assert len(window._chats) == 2
    assert window._chat_items[0].chatDelete.toolTip() == "Удалить чат"

    window._remove_chat(second_chat.id)

    assert len(window._chats) == 1
    assert window._active_chat().id == first_chat.id
    assert window.stack.currentIndex() == 1
    assert window.transcriptLayout.count() == 3

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
