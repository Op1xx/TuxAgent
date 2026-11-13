"""Общий интерфейс и бэкенды пакетных менеджеров: apt, dnf, pacman, zypper, ALT.

Главные правила модуля:

* команды собираются как argv, а не как строка для shell;
* имена пакетов и поисковые запросы валидируются до подстановки;
* изменяющие операции требуют root и выполняются через ``pkexec``;
* dry-run либо использует подтверждённый флаг, либо честно отказывает.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import ClassVar

from distro import DistroFamily, DistroInfo, DistroResolution, resolve_distro

Which = Callable[[str], str | None]
EnvVars = tuple[tuple[str, str], ...]

_DEB_PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]*(?::[a-z0-9-]+)?$")
_RPM_PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._-]*$")
_PACMAN_PACKAGE_RE = re.compile(r"^[a-z0-9@_+][a-z0-9@_+.-]*$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_MAX_PACKAGE_NAME = 200
_MAX_PACKAGES = 100
_MAX_QUERY = 200


class PackageManagerError(RuntimeError):
    """Базовая ошибка пакетного бэкенда."""


class UnsupportedDistroError(PackageManagerError):
    """Для семейства дистрибутива нет бэкенда."""


class PackageManagerNotFoundError(PackageManagerError):
    """Нужная утилита пакетного менеджера не найдена в PATH."""


class InvalidPackageNameError(PackageManagerError, ValueError):
    """Имя пакета или поисковый запрос не прошли валидацию."""


class RootPrivilegesError(PackageManagerError):
    """Не удалось поднять привилегии через pkexec."""


class CommandExecutionError(PackageManagerError):
    """Процесс не удалось запустить."""


class CommandTimeoutError(PackageManagerError):
    """Процесс превысил таймаут."""


class DryRunNotSupportedError(PackageManagerError):
    """Бэкенд не умеет доказуемо безопасный dry-run для этой операции."""


@dataclass(frozen=True, slots=True)
class Command:
    """Готовая команда: argv, необходимость root и флаг dry-run."""

    argv: tuple[str, ...]
    requires_root: bool = False
    dry_run: bool = False
    env: EnvVars = ()

    def __post_init__(self) -> None:
        if not self.argv:
            raise ValueError("argv не может быть пустым")
        for part in self.argv:
            if not isinstance(part, str) or not part:
                raise ValueError("каждый элемент argv должен быть непустой строкой")
            if "\x00" in part:
                raise ValueError("NUL-символ в argv недопустим")


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Результат выполнения команды в виде dataclass, а не сырого stdout."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    dry_run: bool = False

    @property
    def success(self) -> bool:
        """Команда завершилась с кодом 0."""
        return self.returncode == 0


@dataclass(frozen=True, slots=True)
class PackageSearchResult:
    """Одна строка результата поиска пакета."""

    name: str
    summary: str = ""


class CommandRunner:
    """Выполнение :class:`Command` без shell.

    Вынесен в отдельный класс, чтобы тесты подменяли выполнение, а сборка
    argv оставалась детерминированной.
    """

    def __init__(
        self,
        *,
        is_root: bool | None = None,
        which: Which = shutil.which,
        pkexec: str = "pkexec",
        timeout: float = 120.0,
    ) -> None:
        self.is_root = os.geteuid() == 0 if is_root is None else is_root
        self._which = which
        self._pkexec = pkexec
        self._timeout = timeout

    def build_argv(self, command: Command, is_root: bool | None = None) -> tuple[str, ...]:
        """Собрать фактический argv с ``pkexec``, если нужен root."""
        root = self.is_root if is_root is None else is_root
        if not command.requires_root or root:
            return command.argv

        pkexec = self._which(self._pkexec)
        if pkexec is None:
            raise RootPrivilegesError(
                "Для root-операции нужен pkexec/polkit, но pkexec не найден в PATH"
            )
        argv = [pkexec]
        if command.env:
            argv.append("env")
            argv.extend(f"{key}={value}" for key, value in command.env)
        argv.extend(command.argv)
        return tuple(argv)

    def run(self, command: Command, timeout: float | None = None) -> CommandResult:
        """Выполнить команду; shell не используется ни при каких условиях."""
        argv = self.build_argv(command)
        env = os.environ.copy()
        env.update(dict(command.env))
        try:
            completed = subprocess.run(
                argv,
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                timeout=self._timeout if timeout is None else timeout,
                env=env,
            )
        except FileNotFoundError as error:
            raise PackageManagerNotFoundError(
                f"Утилита не найдена: {argv[0]}"
            ) from error
        except subprocess.TimeoutExpired as error:
            raise CommandTimeoutError(
                f"Команда не завершилась за отведённое время: {argv[0]}"
            ) from error
        except OSError as error:
            raise CommandExecutionError(f"Не удалось запустить {argv[0]}: {error}") from error
        return CommandResult(
            argv=argv,
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            dry_run=command.dry_run,
        )


class PackageManager(ABC):
    """Общий интерфейс пакетного менеджера."""

    family: ClassVar[DistroFamily]
    name: ClassVar[str]
    executable: ClassVar[str]
    required_executables: ClassVar[tuple[str, ...]] = ()
    package_re: ClassVar[re.Pattern[str]] = _RPM_PACKAGE_RE
    not_installed_returncodes: ClassVar[tuple[int, ...]] = (1,)

    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        which: Which = shutil.which,
    ) -> None:
        self._which = which
        self.runner = runner or CommandRunner(which=which)

    @property
    def tools(self) -> tuple[str, ...]:
        """Все утилиты, нужные бэкенду."""
        return self.required_executables or (self.executable,)

    def is_available(self) -> bool:
        """Все необходимые утилиты найдены в PATH."""
        return all(self._which(tool) is not None for tool in self.tools)

    def _require_available(self) -> None:
        missing = [tool for tool in self.tools if self._which(tool) is None]
        if missing:
            raise PackageManagerNotFoundError(
                f"Для {self.name} не найдены утилиты: {', '.join(missing)}"
            )

    def _execute(self, command: Command) -> CommandResult:
        self._require_available()
        return self.runner.run(command)

    def _command(
        self,
        argv: Sequence[str],
        *,
        requires_root: bool,
        dry_run: bool = False,
        env: EnvVars = (),
    ) -> Command:
        return Command(
            argv=tuple(argv),
            requires_root=requires_root and not dry_run,
            dry_run=dry_run,
            env=env,
        )

    @staticmethod
    def _unsupported_dry_run(manager: str, operation: str) -> DryRunNotSupportedError:
        return DryRunNotSupportedError(
            f"Dry-run для {manager} ({operation}) не подтверждён: "
            "безопасный флаг неизвестен, выполнение отменено"
        )

    def validate_packages(self, packages: Sequence[str]) -> tuple[str, ...]:
        """Проверить имена пакетов до подстановки в argv."""
        items = tuple(packages)
        if not items:
            raise InvalidPackageNameError("Не указано имя пакета")
        if len(items) > _MAX_PACKAGES:
            raise InvalidPackageNameError(
                f"Слишком много пакетов за один раз: {len(items)} > {_MAX_PACKAGES}"
            )
        for package in items:
            if not isinstance(package, str) or not package:
                raise InvalidPackageNameError("Имя пакета не может быть пустым")
            if len(package) > _MAX_PACKAGE_NAME:
                raise InvalidPackageNameError(
                    f"Слишком длинное имя пакета: {package[:40]}…"
                )
            if self.package_re.fullmatch(package) is None:
                raise InvalidPackageNameError(
                    f"Недопустимое имя пакета для {self.name}: {package!r}"
                )
        return items

    @staticmethod
    def validate_query(query: str) -> str:
        """Проверить поисковый запрос: без управления и без ведущего дефиса."""
        if not query or not query.strip():
            raise InvalidPackageNameError("Поисковый запрос не может быть пустым")
        if len(query) > _MAX_QUERY:
            raise InvalidPackageNameError("Слишком длинный поисковый запрос")
        if query.startswith("-"):
            raise InvalidPackageNameError(
                "Поисковый запрос не может начинаться с «-»: это похоже на флаг"
            )
        if _CONTROL_RE.search(query):
            raise InvalidPackageNameError("Управляющие символы в запросе недопустимы")
        return query

    @abstractmethod
    def build_install(self, packages: Sequence[str], dry_run: bool = False) -> Command:
        """Собрать команду установки."""

    @abstractmethod
    def build_remove(self, packages: Sequence[str], dry_run: bool = False) -> Command:
        """Собрать команду удаления."""

    @abstractmethod
    def build_update_index(self, dry_run: bool = False) -> Command:
        """Собрать команду обновления индекса пакетов."""

    @abstractmethod
    def build_clean_cache(self, dry_run: bool = False) -> Command:
        """Собрать команду очистки кэша пакетов."""

    @abstractmethod
    def build_is_installed(self, package: str) -> Command:
        """Собрать команду проверки установленного пакета."""

    @abstractmethod
    def build_search(self, query: str) -> Command:
        """Собрать команду поиска пакета."""

    @abstractmethod
    def parse_search(self, output: str) -> list[PackageSearchResult]:
        """Разобрать вывод поиска в типизированный список."""

    def install(self, packages: Sequence[str], dry_run: bool = False) -> CommandResult:
        """Установить пакеты (или показать план при dry-run)."""
        return self._execute(self.build_install(packages, dry_run))

    def remove(self, packages: Sequence[str], dry_run: bool = False) -> CommandResult:
        """Удалить пакеты (или показать план при dry-run)."""
        return self._execute(self.build_remove(packages, dry_run))

    def update_index(self, dry_run: bool = False) -> CommandResult:
        """Обновить индекс пакетов."""
        return self._execute(self.build_update_index(dry_run))

    def clean_cache(self, dry_run: bool = False) -> CommandResult:
        """Очистить кэш пакетного менеджера."""
        return self._execute(self.build_clean_cache(dry_run))

    def is_installed(self, package: str) -> bool:
        """Проверить, установлен ли пакет, по фактическому коду возврата."""
        validated = self.validate_packages((package,))[0]
        result = self._execute(self.build_is_installed(validated))
        if result.returncode == 0:
            return True
        if result.returncode in self.not_installed_returncodes:
            return False
        detail = result.stderr.strip() or result.stdout.strip() or "нет вывода"
        raise PackageManagerError(
            f"Не удалось проверить пакет {validated}: код {result.returncode}, {detail}"
        )

    def search(self, query: str) -> list[PackageSearchResult]:
        """Найти пакеты; возвращает разобранный список, а не сырой stdout."""
        validated = self.validate_query(query)
        result = self._execute(self.build_search(validated))
        if not result.success:
            detail = result.stderr.strip() or result.stdout.strip() or "нет вывода"
            raise PackageManagerError(
                f"Поиск не удался ({self.name}): код {result.returncode}, {detail}"
            )
        return self.parse_search(result.stdout)


class AptPackageManager(PackageManager):
    """apt для Debian, Ubuntu и Astra Linux."""

    family = DistroFamily.DEBIAN
    name = "apt"
    executable = "apt-get"
    required_executables = ("apt-get", "apt-cache", "dpkg-query")
    package_re = _DEB_PACKAGE_RE
    _env: ClassVar[EnvVars] = (("DEBIAN_FRONTEND", "noninteractive"),)

    def _apt_argv(self, operation: str, args: Sequence[str], dry_run: bool) -> tuple[str, ...]:
        if dry_run:
            return ("apt-get", "--simulate", operation, *args)
        return ("apt-get", operation, *args)

    def build_install(self, packages: Sequence[str], dry_run: bool = False) -> Command:
        items = self.validate_packages(packages)
        argv = self._apt_argv("install", ("--yes", *items), dry_run)
        return self._command(argv, requires_root=True, dry_run=dry_run, env=self._env)

    def build_remove(self, packages: Sequence[str], dry_run: bool = False) -> Command:
        items = self.validate_packages(packages)
        argv = self._apt_argv("remove", ("--yes", *items), dry_run)
        return self._command(argv, requires_root=True, dry_run=dry_run, env=self._env)

    def build_update_index(self, dry_run: bool = False) -> Command:
        argv = self._apt_argv("update", (), dry_run)
        return self._command(argv, requires_root=True, dry_run=dry_run, env=self._env)

    def build_clean_cache(self, dry_run: bool = False) -> Command:
        argv = self._apt_argv("clean", (), dry_run)
        return self._command(argv, requires_root=True, dry_run=dry_run, env=self._env)

    def build_is_installed(self, package: str) -> Command:
        item = self.validate_packages((package,))[0]
        return self._command(
            ("dpkg-query", "-W", "-f=${Status}", item),
            requires_root=False,
        )

    def build_search(self, query: str) -> Command:
        item = self.validate_query(query)
        return self._command(
            ("apt-cache", "search", "--names-only", item),
            requires_root=False,
        )

    def parse_search(self, output: str) -> list[PackageSearchResult]:
        results: list[PackageSearchResult] = []
        for line in output.splitlines():
            name, separator, summary = line.partition(" - ")
            if separator and name.strip() and not name.startswith((" ", "\t")):
                results.append(PackageSearchResult(name.strip(), summary.strip()))
        return results


class AltAptPackageManager(AptPackageManager):
    """ALT Linux: нативный apt, но запрос установленных пакетов идёт через rpm."""

    family = DistroFamily.ALT
    name = "ALT apt"
    required_executables = ("apt-get", "apt-cache", "rpm")
    package_re = _RPM_PACKAGE_RE

    def build_is_installed(self, package: str) -> Command:
        item = self.validate_packages((package,))[0]
        return self._command(("rpm", "-q", item), requires_root=False)


class DnfPackageManager(PackageManager):
    """dnf для Fedora и современной ROSA."""

    family = DistroFamily.FEDORA
    name = "dnf"
    executable = "dnf"
    required_executables = ("dnf", "rpm")
    package_re = _RPM_PACKAGE_RE

    @staticmethod
    def _transaction_argv(
        operation: str, args: Sequence[str], dry_run: bool
    ) -> tuple[str, ...]:
        if dry_run:
            return ("dnf", operation, "--assumeyes", "--setopt=tsflags=test", *args)
        return ("dnf", operation, "--assumeyes", *args)

    def build_install(self, packages: Sequence[str], dry_run: bool = False) -> Command:
        items = self.validate_packages(packages)
        return self._command(
            self._transaction_argv("install", items, dry_run),
            requires_root=True,
            dry_run=dry_run,
        )

    def build_remove(self, packages: Sequence[str], dry_run: bool = False) -> Command:
        items = self.validate_packages(packages)
        return self._command(
            self._transaction_argv("remove", items, dry_run),
            requires_root=True,
            dry_run=dry_run,
        )

    def build_update_index(self, dry_run: bool = False) -> Command:
        if dry_run:
            raise self._unsupported_dry_run(self.name, "update_index")
        return self._command(("dnf", "makecache"), requires_root=True)

    def build_clean_cache(self, dry_run: bool = False) -> Command:
        if dry_run:
            raise self._unsupported_dry_run(self.name, "clean_cache")
        return self._command(("dnf", "clean", "all"), requires_root=True)

    def build_is_installed(self, package: str) -> Command:
        item = self.validate_packages((package,))[0]
        return self._command(("rpm", "-q", item), requires_root=False)

    def build_search(self, query: str) -> Command:
        item = self.validate_query(query)
        return self._command(("dnf", "--quiet", "search", item), requires_root=False)

    def parse_search(self, output: str) -> list[PackageSearchResult]:
        results: list[PackageSearchResult] = []
        for line in output.splitlines():
            if line.startswith(("=", " ", "\t")) or " : " not in line:
                continue
            name, summary = line.split(" : ", 1)
            package = name.strip().split()[0]
            for suffix in (".x86_64", ".noarch", ".i686", ".aarch64"):
                if package.endswith(suffix):
                    package = package[: -len(suffix)]
                    break
            if package:
                results.append(PackageSearchResult(package, summary.strip()))
        return results


class PacmanPackageManager(PackageManager):
    """pacman для Arch Linux и производных."""

    family = DistroFamily.ARCH
    name = "pacman"
    executable = "pacman"
    package_re = _PACMAN_PACKAGE_RE

    def build_install(self, packages: Sequence[str], dry_run: bool = False) -> Command:
        items = self.validate_packages(packages)
        flag = "--print" if dry_run else "--noconfirm"
        argv = ("pacman", "-S", flag, *items)
        return self._command(argv, requires_root=True, dry_run=dry_run)

    def build_remove(self, packages: Sequence[str], dry_run: bool = False) -> Command:
        items = self.validate_packages(packages)
        flag = "--print" if dry_run else "--noconfirm"
        argv = ("pacman", "-R", flag, *items)
        return self._command(argv, requires_root=True, dry_run=dry_run)

    def build_update_index(self, dry_run: bool = False) -> Command:
        if dry_run:
            raise self._unsupported_dry_run(self.name, "update_index")
        return self._command(("pacman", "-Sy"), requires_root=True)

    def build_clean_cache(self, dry_run: bool = False) -> Command:
        if dry_run:
            raise self._unsupported_dry_run(self.name, "clean_cache")
        return self._command(("pacman", "-Sc", "--noconfirm"), requires_root=True)

    def build_is_installed(self, package: str) -> Command:
        item = self.validate_packages((package,))[0]
        return self._command(("pacman", "-Q", item), requires_root=False)

    def build_search(self, query: str) -> Command:
        item = self.validate_query(query)
        return self._command(("pacman", "-Ss", item), requires_root=False)

    def parse_search(self, output: str) -> list[PackageSearchResult]:
        results: list[PackageSearchResult] = []
        pending: PackageSearchResult | None = None
        for line in output.splitlines():
            if not line.strip():
                continue
            if line[0] in (" ", "\t"):
                if pending is not None and not pending.summary:
                    pending = PackageSearchResult(pending.name, line.strip())
                    results[-1] = pending
                continue
            repository_name = line.split()[0]
            if "/" not in repository_name:
                continue
            _, _, package = repository_name.partition("/")
            pending = PackageSearchResult(package)
            results.append(pending)
        return results


class ZypperPackageManager(PackageManager):
    """zypper для openSUSE."""

    family = DistroFamily.SUSE
    name = "zypper"
    executable = "zypper"
    required_executables = ("zypper", "rpm")
    package_re = _RPM_PACKAGE_RE

    @staticmethod
    def _zypper_argv(
        operation: str, args: Sequence[str], dry_run: bool
    ) -> tuple[str, ...]:
        if dry_run:
            return ("zypper", "--non-interactive", "--dry-run", operation, *args)
        return ("zypper", "--non-interactive", operation, *args)

    def build_install(self, packages: Sequence[str], dry_run: bool = False) -> Command:
        items = self.validate_packages(packages)
        return self._command(
            self._zypper_argv("install", items, dry_run),
            requires_root=True,
            dry_run=dry_run,
        )

    def build_remove(self, packages: Sequence[str], dry_run: bool = False) -> Command:
        items = self.validate_packages(packages)
        return self._command(
            self._zypper_argv("remove", items, dry_run),
            requires_root=True,
            dry_run=dry_run,
        )

    def build_update_index(self, dry_run: bool = False) -> Command:
        return self._command(
            self._zypper_argv("refresh", (), dry_run),
            requires_root=True,
            dry_run=dry_run,
        )

    def build_clean_cache(self, dry_run: bool = False) -> Command:
        return self._command(
            self._zypper_argv("clean", ("--all",), dry_run),
            requires_root=True,
            dry_run=dry_run,
        )

    def build_is_installed(self, package: str) -> Command:
        item = self.validate_packages((package,))[0]
        return self._command(("rpm", "-q", item), requires_root=False)

    def build_search(self, query: str) -> Command:
        item = self.validate_query(query)
        return self._command(
            ("zypper", "--non-interactive", "search", item),
            requires_root=False,
        )

    def parse_search(self, output: str) -> list[PackageSearchResult]:
        results: list[PackageSearchResult] = []
        for line in output.splitlines():
            if "|" not in line or line.lstrip().startswith("---"):
                continue
            columns = [column.strip() for column in line.split("|")]
            if len(columns) < 3 or columns[1] in ("", "Name"):
                continue
            results.append(
                PackageSearchResult(columns[1], columns[2] if len(columns) > 2 else "")
            )
        return results


class EpmPackageManager(PackageManager):
    """Опциональный EPM для ALT Linux.

    Команды собраны по документированному CLI EPM, но на живой системе не
    проверялись. Dry-run не поддерживается, пока флаг не будет подтверждён.
    """

    family = DistroFamily.ALT
    name = "EPM"
    executable = "epm"
    required_executables = ("epm", "rpm")
    package_re = _RPM_PACKAGE_RE

    def build_install(self, packages: Sequence[str], dry_run: bool = False) -> Command:
        if dry_run:
            raise self._unsupported_dry_run(self.name, "install")
        items = self.validate_packages(packages)
        return self._command(("epm", "install", "-y", *items), requires_root=True)

    def build_remove(self, packages: Sequence[str], dry_run: bool = False) -> Command:
        items = self.validate_packages(packages)
        # man eepm: --dry-run/--simulate поддержан для remove.
        prefix = ("epm", "--dry-run", "remove") if dry_run else ("epm", "remove")
        return self._command((*prefix, "-y", *items), requires_root=True, dry_run=dry_run)

    def build_update_index(self, dry_run: bool = False) -> Command:
        if dry_run:
            raise self._unsupported_dry_run(self.name, "update_index")
        return self._command(("epm", "update"), requires_root=True)

    def build_clean_cache(self, dry_run: bool = False) -> Command:
        if dry_run:
            raise self._unsupported_dry_run(self.name, "clean_cache")
        return self._command(("epm", "clean"), requires_root=True)

    def build_is_installed(self, package: str) -> Command:
        item = self.validate_packages((package,))[0]
        return self._command(("rpm", "-q", item), requires_root=False)

    def build_search(self, query: str) -> Command:
        item = self.validate_query(query)
        return self._command(("epm", "search", item), requires_root=False)

    def parse_search(self, output: str) -> list[PackageSearchResult]:
        results: list[PackageSearchResult] = []
        for line in output.splitlines():
            name, separator, summary = line.partition(" - ")
            if separator and name.strip() and not line.startswith((" ", "\t")):
                results.append(PackageSearchResult(name.strip(), summary.strip()))
        return results


MANAGER_BY_FAMILY: dict[DistroFamily, type[PackageManager]] = {
    DistroFamily.DEBIAN: AptPackageManager,
    DistroFamily.FEDORA: DnfPackageManager,
    DistroFamily.ARCH: PacmanPackageManager,
    DistroFamily.SUSE: ZypperPackageManager,
    DistroFamily.ALT: AltAptPackageManager,
}


def create_package_manager(
    distro: DistroInfo | DistroFamily | str,
    runner: CommandRunner | None = None,
    *,
    which: Which = shutil.which,
    prefer_epm: bool = False,
) -> PackageManager:
    """Выбрать и проверить бэкенд по дистрибутиву или семейству.

    Для ALT по умолчанию используется apt; EPM выбирается только явно
    (``prefer_epm=True``) или как фолбэк, если apt недоступен.
    """
    if isinstance(distro, DistroInfo):
        family = distro.family
    else:
        try:
            family = DistroFamily(distro)
        except ValueError as error:
            raise UnsupportedDistroError(
                f"Неизвестное семейство дистрибутива: {distro}"
            ) from error

    if family is DistroFamily.UNKNOWN:
        raise UnsupportedDistroError(
            "Дистрибутив не распознан; выберите профиль вручную в настройках"
        )

    if family is DistroFamily.ALT:
        candidates: list[type[PackageManager]] = []
        if prefer_epm:
            candidates.append(EpmPackageManager)
        candidates.append(AltAptPackageManager)
        if not prefer_epm:
            candidates.append(EpmPackageManager)
    else:
        manager_class = MANAGER_BY_FAMILY.get(family)
        if manager_class is None:
            raise UnsupportedDistroError(
                f"Для семейства «{family}» нет пакетного бэкенда"
            )
        candidates = [manager_class]

    for manager_class in candidates:
        manager = manager_class(runner, which=which)
        if manager.is_available():
            return manager

    expected = ", ".join(tool for tool in candidates[0].required_executables)
    raise PackageManagerNotFoundError(
        f"Для семейства «{family}» не найден пакетный менеджер "
        f"(проверены: {expected})"
    )


@dataclass(frozen=True, slots=True)
class PackageManagerSelection:
    """Выбранный бэкенд и предупреждение о ручном override."""

    manager: PackageManager
    resolution: DistroResolution

    @property
    def warning(self) -> str | None:
        """Предупреждение о расхождении профиля с реальной системой."""
        return self.resolution.warning


def create_package_manager_for_settings(
    detected: DistroInfo,
    mode: str,
    override: str | None,
    runner: CommandRunner | None = None,
    *,
    which: Which = shutil.which,
    prefer_epm: bool = False,
) -> PackageManagerSelection:
    """Применить настройки ``auto``/``manual`` и выбрать бэкенд.

    При расхождении ручного профиля с реальной системой бэкенд всё равно
    выбирается по профилю, но ``warning`` заполняется. Агентный слой должен
    использовать это предупреждение, чтобы не выполнять рискованные операции.
    """
    resolution = resolve_distro(detected, mode, override)
    manager = create_package_manager(
        resolution.selected_family,
        runner,
        which=which,
        prefer_epm=prefer_epm,
    )
    return PackageManagerSelection(manager=manager, resolution=resolution)
