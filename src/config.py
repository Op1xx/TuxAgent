"""Настройки приложения: дистрибутив, модель, пути и тема."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from platformdirs import user_data_dir
from PyQt6.QtCore import QSettings

APP_NAME = "TuxAgent"
ORG_NAME = "TuxAgent"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UI_DIR = PROJECT_ROOT / "ui"
RESOURCES_DIR = PROJECT_ROOT / "resources"
FONTS_DIR = Path(__file__).resolve().parent / "fonts"

# Семейства дистрибутивов из ТЗ. Детект появится на этапе 2.
DISTRO_CHOICES: dict[str, str] = {
    "debian": "Debian / Ubuntu / Astra Linux",
    "fedora": "Fedora / ROSA",
    "arch": "Arch Linux",
    "suse": "openSUSE",
    "alt": "ALT Linux",
}

DISTRO_MODES: dict[str, str] = {
    "auto": "Автоопределение",
    "manual": "Указать вручную",
}

LLM_BACKENDS: dict[str, str] = {
    "mock": "Mock (без модели)",
    "llama_cpp": "llama.cpp",
    "ollama": "Ollama",
}

THEMES: dict[str, str] = {
    "dark": "Тёмная",
}


def default_data_dir() -> Path:
    """Каталог runtime-данных проекта (``data/`` рядом с ``src/``)."""
    return PROJECT_ROOT / "data"


def default_models_dir() -> Path:
    """Каталог моделей в XDG-хранилище пользователя."""
    return Path(user_data_dir("tuxagent")) / "models"


@dataclass(slots=True)
class AppSettings:
    """Пользовательские настройки со значениями по умолчанию."""

    distro_mode: str = "auto"
    distro_override: str = ""
    llm_backend: str = "mock"
    model_path: str = ""
    data_dir: str = field(default_factory=lambda: str(default_data_dir()))
    models_dir: str = field(default_factory=lambda: str(default_models_dir()))
    theme: str = "dark"


def _store(ini_path: Path | None = None) -> QSettings:
    if ini_path is not None:
        return QSettings(str(ini_path), QSettings.Format.IniFormat)
    return QSettings(ORG_NAME, APP_NAME)


def _choice(store: QSettings, key: str, default: str, choices: dict[str, str]) -> str:
    value = str(store.value(key, default))
    return value if value in choices else default


def _text(store: QSettings, key: str, default: str) -> str:
    return str(store.value(key, default))


def load_settings(ini_path: Path | None = None) -> AppSettings:
    """Прочитать настройки; при отсутствии файла вернуть значения по умолчанию."""
    defaults = AppSettings()
    store = _store(ini_path)
    settings = AppSettings(
        distro_mode=_choice(store, "distro/mode", defaults.distro_mode, DISTRO_MODES),
        distro_override=_choice(store, "distro/override", "", DISTRO_CHOICES),
        llm_backend=_choice(store, "llm/backend", defaults.llm_backend, LLM_BACKENDS),
        model_path=_text(store, "llm/model_path", defaults.model_path),
        data_dir=_text(store, "paths/data", defaults.data_dir),
        models_dir=_text(store, "paths/models", defaults.models_dir),
        theme=_choice(store, "ui/theme", defaults.theme, THEMES),
    )
    store.sync()
    return settings


def save_settings(settings: AppSettings, ini_path: Path | None = None) -> None:
    """Сохранить настройки в QSettings."""
    store = _store(ini_path)
    store.setValue("distro/mode", settings.distro_mode)
    store.setValue("distro/override", settings.distro_override)
    store.setValue("llm/backend", settings.llm_backend)
    store.setValue("llm/model_path", settings.model_path)
    store.setValue("paths/data", settings.data_dir)
    store.setValue("paths/models", settings.models_dir)
    store.setValue("ui/theme", settings.theme)
    store.sync()


def ensure_runtime_dirs(settings: AppSettings) -> None:
    """Создать каталоги данных и моделей, если их ещё нет."""
    Path(settings.data_dir).expanduser().mkdir(parents=True, exist_ok=True)
    Path(settings.models_dir).expanduser().mkdir(parents=True, exist_ok=True)
