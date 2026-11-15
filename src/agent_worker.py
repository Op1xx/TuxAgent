"""Выполнение одной фазы агентного цикла в отдельном потоке.

GUI не должен блокироваться ни на вызове модели, ни на выполнении инструмента.
Каждая фаза (``start`` или ``resume``) запускается отдельной задачей: между
ними главный поток успевает показать диалог подтверждения.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QObject, QThread, pyqtSignal


class AgentTask(QThread):
    """Выполнить функцию в фоне и вернуть результат сигналом."""

    finished_turn = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        work: Callable[[], Any],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._work = work

    def run(self) -> None:
        try:
            result = self._work()
        except Exception as error:  # ошибка уходит в GUI, а не теряется
            self.failed.emit(str(error))
            return
        self.finished_turn.emit(result)
