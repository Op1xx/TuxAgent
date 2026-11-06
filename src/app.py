"""Точка входа приложения TuxAgent."""

from __future__ import annotations

import logging
import sys
from types import TracebackType

from PyQt6.QtGui import QFontDatabase
from PyQt6.QtWidgets import QApplication, QMessageBox

from config import (
    APP_NAME,
    FONTS_DIR,
    ORG_NAME,
    RESOURCES_DIR,
    AppSettings,
    ensure_runtime_dirs,
    load_settings,
)
from main_window import MainWindow
from version import __version__

logger = logging.getLogger(__name__)

THEME_FILES = {"dark": "main.qss"}


def _load_fonts() -> None:
    for font_file in sorted(FONTS_DIR.glob("*.ttf")):
        if QFontDatabase.addApplicationFont(str(font_file)) < 0:
            logger.warning("Не удалось загрузить шрифт: %s", font_file)


def apply_stylesheet(app: QApplication, settings: AppSettings) -> None:
    """Применить QSS выбранной темы ко всему приложению."""
    filename = THEME_FILES.get(settings.theme, f"{settings.theme}.qss")
    path = RESOURCES_DIR / filename
    if not path.is_file():
        logger.warning("Файл темы не найден: %s", path)
        return
    app.setStyleSheet(path.read_text(encoding="utf-8"))


def _install_excepthook() -> None:
    def handle(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: TracebackType | None,
    ) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logger.critical(
            "Необработанная ошибка",
            exc_info=(exc_type, exc_value, exc_traceback),
        )
        QMessageBox.critical(
            None,
            "TuxAgent — ошибка",
            f"Произошла непредвиденная ошибка:\n\n{exc_type.__name__}: {exc_value}",
        )

    sys.excepthook = handle


def create_application(
    argv: list[str] | None = None,
    settings: AppSettings | None = None,
) -> QApplication:
    """Создать QApplication, загрузить шрифты и стили."""
    existing = QApplication.instance()
    app = existing if isinstance(existing, QApplication) else QApplication(
        argv if argv is not None else sys.argv
    )
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setApplicationVersion(__version__)
    app.setDesktopFileName("tuxagent")
    app.setStyle("Fusion")
    _load_fonts()
    apply_stylesheet(app, settings or load_settings())
    _install_excepthook()
    return app


def main(argv: list[str] | None = None) -> int:
    """Запустить приложение."""
    settings = load_settings()
    ensure_runtime_dirs(settings)
    app = create_application(argv, settings)
    window = MainWindow(settings)
    window.show()
    return app.exec()
