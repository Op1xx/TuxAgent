"""Контроллер диалога подтверждения опасного действия."""

from __future__ import annotations

from typing import Any

from PyQt6 import uic
from PyQt6.QtWidgets import QDialog, QWidget

from config import UI_DIR


class ConfirmDialog(QDialog):
    """Показывает, что будет затронуто, и спрашивает подтверждение."""

    def __init__(
        self,
        *,
        target: str,
        reversible: bool,
        reason: str = "",
        preview: str = "",
        title: str = "Требуется подтверждение",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        uic.loadUi(str(UI_DIR / "confirm_dialog.ui"), self)
        self.confirmTitle.setText(title)
        self.targetLabel.setText(target or "—")
        if reversible:
            self.reversibilityLabel.setText(
                "Да. Действие обратимо: изменения можно отменить или восстановить."
            )
        else:
            self.reversibilityLabel.setText(
                "Нет. Изменения необратимы: восстановить данные автоматически не получится."
            )
        self.riskLabel.setText(reason or "Операция изменяет состояние системы.")
        self.previewLabel.setText(preview or "Предпросмотр команды недоступен.")
        self.previewLabel.setVisible(bool(preview))
        self.btnClose.clicked.connect(self.reject)
        self.cancelBtn.clicked.connect(self.reject)
        self.executeBtn.clicked.connect(self.accept)

    @classmethod
    def for_plan(cls, plan: Any, parent: QWidget | None = None) -> ConfirmDialog:
        """Создать диалог из :class:`tools.ToolPlan` без жёсткой зависимости от типа."""
        return cls(
            target=plan.summary,
            reversible=plan.reversible,
            reason=plan.decision.reason,
            preview=plan.preview,
            parent=parent,
        )

    @classmethod
    def ask(cls, plan: Any, parent: QWidget | None = None) -> bool:
        """Вернуть True, если действие разрешено пользователем."""
        if plan.decision.denied:
            return False
        if not plan.decision.requires_confirmation:
            return True
        dialog = cls.for_plan(plan, parent)
        return dialog.exec() == QDialog.DialogCode.Accepted
