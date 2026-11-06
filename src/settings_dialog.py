"""Контроллер диалога настроек. Разметка — в ``ui/settings_dialog.ui``."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PyQt6 import uic
from PyQt6.QtWidgets import QComboBox, QDialog, QFileDialog, QLineEdit, QWidget

from config import (
    DISTRO_CHOICES,
    DISTRO_MODES,
    LLM_BACKENDS,
    THEMES,
    UI_DIR,
    AppSettings,
    default_models_dir,
)


class SettingsDialog(QDialog):
    """Диалог настроек: дистрибутив, модель, пути и тема."""

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        uic.loadUi(str(UI_DIR / "settings_dialog.ui"), self)
        self._fill_combos()
        self._load_values()
        self._connect_signals()

    def _fill_combos(self) -> None:
        for key, label in DISTRO_MODES.items():
            self.distroMode.addItem(label, key)
        for key, label in DISTRO_CHOICES.items():
            self.distroOverride.addItem(label, key)
        for key, label in LLM_BACKENDS.items():
            self.backendCombo.addItem(label, key)
        for key, label in THEMES.items():
            self.themeCombo.addItem(label, key)

    def _load_values(self) -> None:
        self._select(self.distroMode, self._settings.distro_mode)
        self._select(self.distroOverride, self._settings.distro_override or "debian")
        self._select(self.backendCombo, self._settings.llm_backend)
        self._select(self.themeCombo, self._settings.theme)
        self.modelPath.setText(self._settings.model_path)
        self.dataDir.setText(self._settings.data_dir)
        self.modelsDir.setText(self._settings.models_dir)
        self._sync_enabled()

    @staticmethod
    def _select(combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _connect_signals(self) -> None:
        self.btnClose.clicked.connect(self.reject)
        self.cancelBtn.clicked.connect(self.reject)
        self.saveBtn.clicked.connect(self.accept)
        self.distroMode.currentIndexChanged.connect(self._sync_enabled)
        self.backendCombo.currentIndexChanged.connect(self._sync_enabled)
        self.browseModelBtn.clicked.connect(self._browse_model)
        self.browseDataBtn.clicked.connect(lambda: self._browse_dir(self.dataDir))
        self.browseModelsBtn.clicked.connect(lambda: self._browse_dir(self.modelsDir))

    def _sync_enabled(self) -> None:
        manual = self.distroMode.currentData() == "manual"
        self.distroOverride.setEnabled(manual)
        self.distroOverrideLabel.setEnabled(manual)
        needs_file = self.backendCombo.currentData() == "llama_cpp"
        self.modelPath.setEnabled(needs_file)
        self.browseModelBtn.setEnabled(needs_file)

    def _browse_model(self) -> None:
        start = self.modelPath.text().strip()
        if not start:
            start = str(Path(self._settings.models_dir).expanduser())
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Выбрать модель",
            start,
            "GGUF (*.gguf);;Все файлы (*)",
        )
        if path:
            self.modelPath.setText(path)

    def _browse_dir(self, field: QLineEdit) -> None:
        start = field.text().strip()
        if not start:
            start = str(Path(self._settings.models_dir).expanduser())
        path = QFileDialog.getExistingDirectory(self, "Выбрать каталог", start)
        if path:
            field.setText(path)

    def result_settings(self) -> AppSettings:
        """Настройки, введённые пользователем."""
        manual = self.distroMode.currentData() == "manual"
        return replace(
            self._settings,
            distro_mode=self.distroMode.currentData(),
            distro_override=self.distroOverride.currentData() if manual else "",
            llm_backend=self.backendCombo.currentData(),
            model_path=self.modelPath.text().strip(),
            data_dir=self.dataDir.text().strip() or self._settings.data_dir,
            models_dir=self.modelsDir.text().strip() or str(default_models_dir()),
            theme=self.themeCombo.currentData(),
        )
