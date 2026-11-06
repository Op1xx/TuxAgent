"""Контроллер главного окна. Разметка хранится в ``ui/main_window.ui``."""

from __future__ import annotations

from PyQt6 import uic
from PyQt6.QtCore import QEvent, QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QKeyEvent, QMouseEvent
from PyQt6.QtWidgets import QDialog, QFrame, QLabel, QTextEdit, QWidget

from config import UI_DIR, AppSettings, ensure_runtime_dirs, load_settings, save_settings
from settings_dialog import SettingsDialog


class MainWindow(QWidget):
    """Главное окно: ввод запроса, история, статус и настройки."""

    settings_changed = pyqtSignal(object)

    def __init__(self, settings: AppSettings | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings or load_settings()
        uic.loadUi(str(UI_DIR / "main_window.ui"), self)
        self._chat_frames = [
            frame for frame in (getattr(self, f"chat{index}") for index in range(1, 9))
            if isinstance(frame, QFrame)
        ]
        self._connect_signals()
        self.set_status("idle", "Ожидание запроса")

    def _connect_signals(self) -> None:
        self.promptSend.clicked.connect(lambda: self._submit(self.promptInput))
        self.followupSend.clicked.connect(lambda: self._submit(self.followupInput))
        self.promptInput.installEventFilter(self)
        self.followupInput.installEventFilter(self)
        self.btnMin.clicked.connect(self.showMinimized)
        self.btnMax.clicked.connect(self._toggle_maximized)
        self.btnClose.clicked.connect(self.close)
        self.menuBtn.clicked.connect(self._toggle_sidebar)
        self.settingsBtn.clicked.connect(self.open_settings)
        self.newChatBtn.clicked.connect(self.reset_session)
        for frame in self._chat_frames:
            frame.installEventFilter(self)
            self._repolish(frame)

    def _toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _toggle_sidebar(self) -> None:
        self.sidebar.setVisible(not self.sidebar.isVisible())

    def set_status(self, state: str, text: str) -> None:
        """Обновить статус: idle, thinking, running, waiting, error, done."""
        self.statusText.setText(text)
        for widget in (self.statusDot, self.statusText):
            widget.setProperty("state", state)
            self._repolish(widget)

    @staticmethod
    def _repolish(widget: QWidget) -> None:
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)

    def _submit(self, field: QTextEdit) -> None:
        text = field.toPlainText().strip()
        if not text:
            return
        field.clear()
        self.stack.setCurrentIndex(1)
        self._append_message("command", f"› {text}")
        self._append_message(
            "info",
            "Запрос принят интерфейсом. Агентный цикл будет подключён на этапе 4.",
        )
        self.set_status("idle", "Ожидание запроса")

    def _append_message(self, kind: str, text: str) -> None:
        label = QLabel(text, self.transcriptContent)
        label.setProperty("kind", kind)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout = self.transcriptLayout
        layout.insertWidget(max(0, layout.count() - 1), label)
        QTimer.singleShot(0, self._scroll_transcript_to_bottom)

    def _scroll_transcript_to_bottom(self) -> None:
        bar = self.transcript.verticalScrollBar()
        bar.setValue(bar.maximum())

    def reset_session(self) -> None:
        """Очистить текущий диалог и вернуться на стартовый экран."""
        layout = self.transcriptLayout
        while layout.count() > 1:
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.promptInput.clear()
        self.followupInput.clear()
        self.stack.setCurrentIndex(0)
        self._activate_chat(None)
        self.set_status("idle", "Ожидание запроса")

    def _activate_chat(self, active: QFrame | None) -> None:
        for frame in self._chat_frames:
            frame.setProperty("active", frame is active)
            self._repolish(frame)

    def open_settings(self) -> None:
        """Открыть диалог настроек и сохранить изменения."""
        dialog = SettingsDialog(self._settings, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_settings = dialog.result_settings()
            try:
                ensure_runtime_dirs(new_settings)
            except OSError as error:
                self.set_status("error", f"Не удалось создать каталог: {error}")
                return
            self._settings = new_settings
            save_settings(self._settings)
            self.settings_changed.emit(self._settings)
            self.set_status("done", "Настройки сохранены")
            QTimer.singleShot(2500, lambda: self.set_status("idle", "Ожидание запроса"))

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched in (self.promptInput, self.followupInput):
            if event.type() == QEvent.Type.KeyPress:
                key_event = event
                if isinstance(key_event, QKeyEvent):
                    enter = key_event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                    shift = bool(key_event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                    if enter and not shift:
                        self._submit(watched)
                        return True
        elif watched in self._chat_frames and event.type() == QEvent.Type.MouseButtonPress:
            mouse_event = event
            if (
                isinstance(mouse_event, QMouseEvent)
                and mouse_event.button() == Qt.MouseButton.LeftButton
            ):
                self._activate_chat(watched)
                self.stack.setCurrentIndex(1)
                return True
        return super().eventFilter(watched, event)
