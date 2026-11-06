"""Тесты настроек приложения."""

from __future__ import annotations

from pathlib import Path

from config import AppSettings, ensure_runtime_dirs, load_settings, save_settings


def test_defaults_when_file_is_missing(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "missing.ini")

    assert settings.distro_mode == "auto"
    assert settings.llm_backend == "mock"
    assert settings.theme == "dark"
    assert settings.data_dir
    assert settings.models_dir


def test_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "settings.ini"
    original = AppSettings(
        distro_mode="manual",
        distro_override="arch",
        llm_backend="llama_cpp",
        model_path="/models/qwen.gguf",
        data_dir=str(tmp_path / "data"),
        models_dir=str(tmp_path / "models"),
        theme="dark",
    )

    save_settings(original, path)
    loaded = load_settings(path)

    assert loaded == original


def test_invalid_values_fall_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "settings.ini"
    save_settings(AppSettings(), path)
    path.write_text(
        "[distro]\nmode=broken\noverride=beos\n[llm]\nbackend=magic\n[ui]\ntheme=neon\n",
        encoding="utf-8",
    )

    settings = load_settings(path)

    assert settings.distro_mode == "auto"
    assert settings.distro_override == ""
    assert settings.llm_backend == "mock"
    assert settings.theme == "dark"


def test_ensure_runtime_dirs(tmp_path: Path) -> None:
    settings = AppSettings(
        data_dir=str(tmp_path / "data"),
        models_dir=str(tmp_path / "models"),
    )

    ensure_runtime_dirs(settings)

    assert (tmp_path / "data").is_dir()
    assert (tmp_path / "models").is_dir()
