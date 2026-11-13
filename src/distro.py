"""Определение дистрибутива Linux по ``/etc/os-release``.

Модуль не выполняет команд и не зависит от пакетных менеджеров: он только
читает файл, разбирает поля и определяет семейство. Этого достаточно, чтобы
остальной код выбирал бэкенд по семейству, а не по точному имени дистрибутива.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

OS_RELEASE_PATH = Path("/etc/os-release")

logger = logging.getLogger(__name__)

_LINE_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$")


class DistroFamily(StrEnum):
    """Семейство дистрибутива, определяющее пакетный менеджер."""

    DEBIAN = "debian"
    FEDORA = "fedora"
    ARCH = "arch"
    SUSE = "suse"
    ALT = "alt"
    UNKNOWN = "unknown"


FAMILY_LABELS: dict[DistroFamily, str] = {
    DistroFamily.DEBIAN: "Debian / Ubuntu / Astra Linux",
    DistroFamily.FEDORA: "Fedora / ROSA",
    DistroFamily.ARCH: "Arch Linux",
    DistroFamily.SUSE: "openSUSE",
    DistroFamily.ALT: "ALT Linux",
    DistroFamily.UNKNOWN: "Неизвестный дистрибутив",
}

# Точные ID из /etc/os-release. Для российских дистрибутивов ID может
# отличаться от названия семейства, поэтому таблица важнее эвристик.
_FAMILY_BY_ID: dict[str, DistroFamily] = {
    "debian": DistroFamily.DEBIAN,
    "ubuntu": DistroFamily.DEBIAN,
    "linuxmint": DistroFamily.DEBIAN,
    "pop": DistroFamily.DEBIAN,
    "raspbian": DistroFamily.DEBIAN,
    "kali": DistroFamily.DEBIAN,
    "devuan": DistroFamily.DEBIAN,
    "astra": DistroFamily.DEBIAN,
    "fedora": DistroFamily.FEDORA,
    "rhel": DistroFamily.FEDORA,
    "centos": DistroFamily.FEDORA,
    "rocky": DistroFamily.FEDORA,
    "almalinux": DistroFamily.FEDORA,
    "ol": DistroFamily.FEDORA,
    "rosa": DistroFamily.FEDORA,
    "mandriva": DistroFamily.FEDORA,
    "arch": DistroFamily.ARCH,
    "manjaro": DistroFamily.ARCH,
    "endeavouros": DistroFamily.ARCH,
    "artix": DistroFamily.ARCH,
    "cachyos": DistroFamily.ARCH,
    "opensuse": DistroFamily.SUSE,
    "opensuse-leap": DistroFamily.SUSE,
    "opensuse-tumbleweed": DistroFamily.SUSE,
    "suse": DistroFamily.SUSE,
    "sles": DistroFamily.SUSE,
    "sled": DistroFamily.SUSE,
    "altlinux": DistroFamily.ALT,
    "alt": DistroFamily.ALT,
}


class DistroDetectionError(RuntimeError):
    """Не удалось прочитать или разобрать ``/etc/os-release``."""


@dataclass(frozen=True, slots=True)
class DistroInfo:
    """Разобранные поля ``/etc/os-release`` и вычисленное семейство."""

    id: str
    family: DistroFamily
    id_like: tuple[str, ...] = ()
    version_id: str = ""
    pretty_name: str = ""
    source: Path | None = None

    @property
    def display_name(self) -> str:
        """Человекочитаемое имя для интерфейса и отчётов."""
        if self.pretty_name:
            return self.pretty_name
        if self.id:
            return self.id
        return "Неизвестный дистрибутив"

    @property
    def family_label(self) -> str:
        """Название семейства на русском."""
        return FAMILY_LABELS[self.family]


@dataclass(frozen=True, slots=True)
class DistroResolution:
    """Результат применения ручного override из настроек."""

    detected: DistroInfo
    selected_family: DistroFamily
    override: str | None = None
    warning: str | None = None


def _unquote(value: str) -> str:
    """Снять кавычки с значения по правилам os-release."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        quote = value[0]
        inner = value[1:-1]
        if quote == "'":
            return inner
        # В двойных кавычках os-release допускает экранирование.
        return re.sub(r"\\([\\\"$`])", r"\1", inner)
    return value


def parse_os_release(text: str, source: Path | None = None) -> dict[str, str]:
    """Разобрать содержимое ``/etc/os-release`` в словарь.

    Пустые строки, комментарии и строки без ``KEY=VALUE`` игнорируются.
    Значения в одинарных и двойных кавычках разворачиваются, как принято в
    os-release; это не запуск shell и не подстановка переменных.
    """
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE_RE.match(line)
        if match is None:
            origin = f" ({source})" if source else ""
            logger.debug("Пропущена строка os-release%s: %r", origin, raw_line)
            continue
        values[match.group("key")] = _unquote(match.group("value").strip())
    return values


def detect_family(distro_id: str, id_like: tuple[str, ...] = ()) -> DistroFamily:
    """Определить семейство по ``ID`` и цепочке ``ID_LIKE``."""
    normalized_id = distro_id.strip().lower()
    if normalized_id in _FAMILY_BY_ID:
        return _FAMILY_BY_ID[normalized_id]
    for candidate in id_like:
        normalized = candidate.strip().lower()
        if normalized in _FAMILY_BY_ID:
            return _FAMILY_BY_ID[normalized]
    return DistroFamily.UNKNOWN


def parse_distro_info(text: str, source: Path | None = None) -> DistroInfo:
    """Собрать :class:`DistroInfo` из содержимого ``os-release``."""
    values = parse_os_release(text, source)
    distro_id = values.get("ID", "").strip().lower()
    id_like = tuple(part for part in values.get("ID_LIKE", "").split() if part)
    return DistroInfo(
        id=distro_id,
        family=detect_family(distro_id, id_like),
        id_like=id_like,
        version_id=values.get("VERSION_ID", ""),
        pretty_name=values.get("PRETTY_NAME", ""),
        source=source,
    )


def detect_distro(os_release_path: Path = OS_RELEASE_PATH) -> DistroInfo:
    """Прочитать ``os-release`` и определить текущий дистрибутив.

    :raises DistroDetectionError: файл отсутствует, недоступен или пуст.
    """
    try:
        text = os_release_path.read_text(encoding="utf-8")
    except OSError as error:
        raise DistroDetectionError(
            f"Не удалось прочитать {os_release_path}: {error}"
        ) from error

    info = parse_distro_info(text, os_release_path)
    if not info.id:
        raise DistroDetectionError(
            f"В {os_release_path} нет поля ID — дистрибутив не определён"
        )
    logger.info(
        "Определён дистрибутив: %s (ID=%s, семейство=%s)",
        info.display_name,
        info.id,
        info.family,
    )
    return info


def resolve_distro(
    detected: DistroInfo,
    mode: str,
    override: str | None,
) -> DistroResolution:
    """Применить режим настроек ``auto``/``manual`` к обнаруженной системе.

    Ручной выбор не меняет факт обнаружения: он только задаёт семейство для
    выбора бэкенда. При расхождении возвращается предупреждение, чтобы GUI
    мог показать его до выполнения рискованных операций.
    """
    selected = detected.family
    warning: str | None = None
    if mode == "manual" and override:
        try:
            selected = DistroFamily(override)
        except ValueError:
            selected = DistroFamily.UNKNOWN
            warning = (
                f"Неизвестный профиль дистрибутива «{override}». "
                f"Система определена как {detected.display_name} "
                f"({detected.family})."
            )
        else:
            if selected is DistroFamily.UNKNOWN:
                warning = (
                    "Выбран неизвестный профиль дистрибутива. "
                    f"Система определена как {detected.display_name}."
                )
            elif selected is not detected.family:
                warning = (
                    f"Выбран профиль «{FAMILY_LABELS[selected]}», но система "
                    f"определена как {detected.display_name} "
                    f"({FAMILY_LABELS[detected.family]}). "
                    "Команды пакетного менеджера могут не подойти этой системе."
                )
    return DistroResolution(
        detected=detected,
        selected_family=selected,
        override=override if mode == "manual" and override else None,
        warning=warning,
    )
