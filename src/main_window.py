"""Контроллер главного окна. Разметка хранится в ``ui/main_window.ui``."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from PyQt6 import uic
from PyQt6.QtCore import QEvent, QObject, QPoint, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QContextMenuEvent, QKeyEvent, QMouseEvent, QResizeEvent
from PyQt6.QtWidgets import QDialog, QFrame, QLabel, QMenu, QMessageBox, QTextEdit, QWidget

from chat_store import ChatMessage, ChatSession, ChatStore, format_chat_time, utc_now_iso
from config import UI_DIR, AppSettings, ensure_runtime_dirs, load_settings, save_settings
from settings_dialog import SettingsDialog

logger = logging.getLogger(__name__)

TASK_MARKERS = {
    "pending": "○",
    "running": "●",
    "done": "✓",
    "error": "×",
}
PANEL_BREAKPOINT = 1080
AUTO_CHAT_NAME_LIMIT = 40


@dataclass(slots=True)
class TaskItem:
    """Один пункт плана, который показывает агент."""

    title: str
    status: str = "pending"
    detail: str = ""


class MainWindow(QWidget):
    """Главное окно: чаты, ввод запроса, задачи, статус и настройки."""

    settings_changed = pyqtSignal(object)

    def __init__(self, settings: AppSettings | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings or load_settings()
        uic.loadUi(str(UI_DIR / "main_window.ui"), self)
        self._store = ChatStore(self._settings.data_dir)
        self._chats = self._store.load()
        self._active_chat_id: str | None = None
        self._chat_items: list[QFrame] = []
        self._task_widgets: list[QFrame] = []
        self._connect_signals()
        self._update_panel_buttons()
        self.set_status("idle", "Ожидание запроса")
        self._restore_chats()

    def _connect_signals(self) -> None:
        self.promptSend.clicked.connect(lambda: self._submit(self.promptInput))
        self.followupSend.clicked.connect(lambda: self._submit(self.followupInput))
        self.promptInput.installEventFilter(self)
        self.followupInput.installEventFilter(self)
        self.btnMin.clicked.connect(self.showMinimized)
        self.btnMax.clicked.connect(self._toggle_maximized)
        self.btnClose.clicked.connect(self.close)
        self.menuBtn.clicked.connect(self._toggle_sidebar)
        self.tasksBtn.clicked.connect(self._toggle_tasks)
        self.settingsBtn.clicked.connect(self.open_settings)
        self.newChatBtn.clicked.connect(self._new_chat)

    def _toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _toggle_sidebar(self) -> None:
        show = not self.sidebar.isVisible()
        if show and self.tasksPanel.isVisible() and self.width() < PANEL_BREAKPOINT:
            self.tasksPanel.setVisible(False)
        self.sidebar.setVisible(show)
        self._update_panel_buttons()

    def _toggle_tasks(self) -> None:
        show = not self.tasksPanel.isVisible()
        if show and self.sidebar.isVisible() and self.width() < PANEL_BREAKPOINT:
            self.sidebar.setVisible(False)
        self.tasksPanel.setVisible(show)
        self._update_panel_buttons()

    def _update_panel_buttons(self) -> None:
        for button, visible in (
            (self.tasksBtn, self.tasksPanel.isVisible()),
            (self.menuBtn, self.sidebar.isVisible()),
        ):
            button.setProperty("active", visible)
            self._repolish(button)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if (
            self.width() < PANEL_BREAKPOINT
            and self.tasksPanel.isVisible()
            and self.sidebar.isVisible()
        ):
            self.tasksPanel.setVisible(False)
            self._update_panel_buttons()

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

    def set_task_context(self, chat_name: str, context: str) -> None:
        """Показать имя чата и контекст текущей задачи."""
        self.tasksChat.setText(chat_name)
        self.tasksContext.setText(context or "Контекст запроса появится здесь.")

    def set_tasks(self, tasks: Sequence[TaskItem]) -> None:
        """Обновить план задач, который показывает панель слева."""
        for widget in self._task_widgets:
            self.tasksListLayout.removeWidget(widget)
            widget.hide()
            widget.deleteLater()
        self._task_widgets.clear()
        self.tasksEmpty.setVisible(not tasks)

        for task in tasks:
            item = uic.loadUi(str(UI_DIR / "task_item.ui"))
            item.setProperty("status", task.status)
            item.taskMarker.setText(TASK_MARKERS.get(task.status, "○"))
            item.taskTitle.setText(task.title)
            item.taskDetail.setText(task.detail)
            item.taskDetail.setVisible(bool(task.detail))
            self._repolish(item)
            self.tasksListLayout.insertWidget(
                max(0, self.tasksListLayout.count() - 1),
                item,
            )
            self._task_widgets.append(item)

        if tasks:
            if self.sidebar.isVisible() and self.width() < PANEL_BREAKPOINT:
                self.sidebar.setVisible(False)
            self.tasksPanel.setVisible(True)
        self._update_panel_buttons()

    def _active_chat(self) -> ChatSession | None:
        for chat in self._chats:
            if chat.id == self._active_chat_id:
                return chat
        return None

    def _chat_by_id(self, chat_id: str) -> ChatSession | None:
        for chat in self._chats:
            if chat.id == chat_id:
                return chat
        return None

    def _create_chat(self) -> ChatSession:
        chat = ChatSession(name="Новый чат")
        self._chats.insert(0, chat)
        self._active_chat_id = chat.id
        return chat

    def _new_chat(self) -> None:
        """Создать новый чат и открыть его."""
        chat = self._create_chat()
        self._save_chats()
        self._rebuild_chat_list()
        self._activate_chat(chat.id)
        self.promptInput.setFocus()

    def _restore_chats(self) -> None:
        if not self._chats:
            self._clear_transcript()
            self.stack.setCurrentIndex(0)
            self.set_task_context("Новый чат", "")
            return
        self._chats.sort(key=lambda chat: chat.updated_at, reverse=True)
        self._rebuild_chat_list()
        self._activate_chat(self._chats[0].id)

    def _rebuild_chat_list(self) -> None:
        for item in self._chat_items:
            self.chatListLayout.removeWidget(item)
            item.hide()
            item.deleteLater()
        self._chat_items.clear()

        for chat in self._chats:
            item = uic.loadUi(str(UI_DIR / "chat_item.ui"))
            active = chat.id == self._active_chat_id
            item.setProperty("chatId", chat.id)
            item.setProperty("active", active)
            item.chatName.setText(chat.name)
            item.chatName.setToolTip(chat.name)
            item.chatTime.setText(format_chat_time(chat.updated_at))
            item.chatDot.setVisible(active)
            item.chatDelete.clicked.connect(
                lambda _checked=False, chat_id=chat.id: self._delete_chat(chat_id)
            )
            item.installEventFilter(self)
            for child in item.findChildren(QLabel):
                child.installEventFilter(self)
            self._repolish(item)
            self.chatListLayout.insertWidget(
                max(0, self.chatListLayout.count() - 1),
                item,
            )
            self._chat_items.append(item)

    @staticmethod
    def _chat_frame_for(watched: QObject) -> QFrame | None:
        current: QObject | None = watched
        while current is not None:
            if isinstance(current, QFrame) and current.property("chatId"):
                return current
            current = current.parent()
        return None

    def _delete_chat(self, chat_id: str) -> None:
        """Спросить подтверждение и удалить чат."""
        chat = self._chat_by_id(chat_id)
        if chat is None:
            return
        box = QMessageBox(self)
        box.setWindowTitle("Удалить чат")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(f"Удалить чат «{chat.name}»?\nИсторию сообщений восстановить нельзя.")
        delete_button = box.addButton("Удалить", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is delete_button:
            self._remove_chat(chat_id)

    def _remove_chat(self, chat_id: str) -> None:
        """Удалить чат без подтверждения; после последнего колонка закрывается."""
        chat = self._chat_by_id(chat_id)
        if chat is None:
            return
        index = self._chats.index(chat)
        was_active = chat.id == self._active_chat_id
        self._chats.pop(index)

        if not self._chats:
            self._active_chat_id = None
            saved = self._save_chats()
            self._rebuild_chat_list()
            self._clear_transcript()
            self.stack.setCurrentIndex(0)
            self.set_tasks([])
            self.set_task_context("Новый чат", "")
            self.sidebar.setVisible(False)
            self._update_panel_buttons()
            if saved:
                self.set_status("done", "Чат удалён")
                QTimer.singleShot(2000, lambda: self.set_status("idle", "Ожидание запроса"))
            return

        if was_active:
            self._active_chat_id = self._chats[min(index, len(self._chats) - 1)].id

        saved = self._save_chats()
        self._rebuild_chat_list()
        active = self._active_chat()
        if active is not None:
            self._render_transcript(active)
            self.stack.setCurrentIndex(1 if active.messages else 0)
            self.set_task_context(active.name, self._task_preview(active))
        if saved:
            self.set_status("done", "Чат удалён")
            QTimer.singleShot(2000, lambda: self.set_status("idle", "Ожидание запроса"))

    def _show_chat_menu(self, chat_id: str, global_pos: QPoint) -> None:
        menu = QMenu(self)
        delete_action = menu.addAction("Удалить чат")
        if menu.exec(global_pos) == delete_action:
            self._delete_chat(chat_id)

    def _activate_chat(self, chat_id: str) -> None:
        self._active_chat_id = chat_id
        self._rebuild_chat_list()
        chat = self._chat_by_id(chat_id)
        if chat is None:
            self._clear_transcript()
            self.stack.setCurrentIndex(0)
            self.set_task_context("Новый чат", "")
            return
        self._render_transcript(chat)
        self.stack.setCurrentIndex(1 if chat.messages else 0)
        self.set_task_context(chat.name, self._task_preview(chat))

    @staticmethod
    def _task_preview(chat: ChatSession) -> str:
        for message in reversed(chat.messages):
            if message.kind == "command":
                text = message.text.removeprefix("› ").strip()
                return text if len(text) <= 160 else f"{text[:157].rstrip()}…"
        return ""

    def _render_transcript(self, chat: ChatSession) -> None:
        self._clear_transcript()
        for message in chat.messages:
            self._append_message(message.kind, message.text)

    def _clear_transcript(self) -> None:
        layout = self.transcriptLayout
        while layout.count() > 1:
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _save_chats(self) -> bool:
        try:
            self._store.save(self._chats)
        except OSError as error:
            logger.warning("Не удалось сохранить чаты: %s", error)
            self.set_status("error", f"Не удалось сохранить чаты: {error}")
            return False
        return True

    def _submit(self, field: QTextEdit) -> None:
        text = field.toPlainText().strip()
        if not text:
            return
        field.clear()

        chat = self._active_chat()
        if chat is None:
            chat = self._create_chat()
            self._rebuild_chat_list()

        # ЗАГЛУШКА: пока имя чата — первые 40 символов запроса.
        # На этапе агентного цикла LLM будет сама придумывать короткое название.
        if not chat.auto_named:
            clean = " ".join(text.split())
            truncated = clean[:AUTO_CHAT_NAME_LIMIT]
            chat.name = truncated + ("…" if len(clean) > AUTO_CHAT_NAME_LIMIT else "")
            chat.auto_named = True

        chat.updated_at = utc_now_iso()
        self._chats.remove(chat)
        self._chats.insert(0, chat)
        command = ChatMessage(kind="command", text=f"› {text}")
        info = ChatMessage(
            kind="info",
            text="Запрос принят интерфейсом. Агентный цикл будет подключён на этапе 4.",
        )
        chat.messages.extend((command, info))
        saved = self._save_chats()
        self._rebuild_chat_list()

        self.stack.setCurrentIndex(1)
        self.set_task_context(chat.name, self._task_preview(chat))
        self._append_message(command.kind, command.text)
        self._append_message(info.kind, info.text)
        if saved:
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
        """Очистить сообщения активного чата и вернуться на стартовый экран."""
        chat = self._active_chat()
        saved = True
        if chat is not None:
            chat.messages.clear()
            chat.updated_at = utc_now_iso()
            saved = self._save_chats()
            self._rebuild_chat_list()
        self._clear_transcript()
        self.promptInput.clear()
        self.followupInput.clear()
        self.stack.setCurrentIndex(0)
        self.set_tasks([])
        self.set_task_context(chat.name if chat is not None else "Новый чат", "")
        if saved:
            self.set_status("idle", "Ожидание запроса")

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
            old_data_dir = self._settings.data_dir
            self._settings = new_settings
            save_settings(self._settings)
            saved = True
            if new_settings.data_dir != old_data_dir:
                self._store = ChatStore(new_settings.data_dir)
                saved = self._save_chats()
            self.settings_changed.emit(self._settings)
            if saved:
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
        elif event.type() == QEvent.Type.MouseButtonPress:
            mouse_event = event
            frame = self._chat_frame_for(watched)
            if (
                frame is not None
                and isinstance(mouse_event, QMouseEvent)
                and mouse_event.button() == Qt.MouseButton.LeftButton
            ):
                self._activate_chat(str(frame.property("chatId")))
                return True
        elif event.type() == QEvent.Type.ContextMenu and isinstance(event, QContextMenuEvent):
            frame = self._chat_frame_for(watched)
            if frame is not None:
                self._show_chat_menu(str(frame.property("chatId")), event.globalPos())
                return True
        return super().eventFilter(watched, event)
