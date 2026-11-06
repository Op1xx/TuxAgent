"""Offscreen smoke-тесты GUI."""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFontDatabase
from PyQt6.QtWidgets import QApplication

from app import apply_stylesheet, create_application
from config import AppSettings
from main_window import MainWindow
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


def test_main_window_smoke(qtbot, qapp: QApplication, settings: AppSettings) -> None:
    apply_stylesheet(qapp, settings)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    window.show()

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

    result = dialog.result_settings()

    assert dialog.distroOverride.isEnabled()
    assert dialog.modelPath.isEnabled()
    assert result.distro_mode == "manual"
    assert result.distro_override == "arch"
    assert result.llm_backend == "llama_cpp"
    assert result.model_path == "/tmp/model.gguf"
    assert result.data_dir == "/tmp/tuxagent-data"
    assert result.models_dir == "/tmp/tuxagent-models"
