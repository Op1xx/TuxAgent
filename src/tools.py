"""Типизированные инструменты, доступные агенту.

Инструмент получает только JSON-аргументы, сам проверяет их схему и сам
классифицирует риск. Модель не может передать произвольную shell-команду:
пакетные операции собираются через :class:`PackageManager`, файловые — через
безопасные методы ``pathlib`` и ``shutil``.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import quote

from audit import AuditLogger, AuditRecord
from package_managers import (
    DryRunNotSupportedError,
    PackageManager,
    PackageManagerError,
)
from safety import (
    ConfirmationRequiredError,
    Operation,
    PathPolicy,
    PolicyDeniedError,
    RiskDecision,
    RiskLevel,
)

JsonObject = dict[str, Any]


class ToolError(RuntimeError):
    """Базовая ошибка инструмента."""


class ToolValidationError(ToolError, ValueError):
    """Аргументы не соответствуют JSON-схеме инструмента."""


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Результат выполнения инструмента."""

    ok: bool
    message: str
    data: JsonObject = field(default_factory=dict)
    argv: tuple[str, ...] = ()
    returncode: int | None = None


@dataclass(frozen=True, slots=True)
class ToolPlan:
    """План выполнения: аргументы, риск и предпросмотр."""

    tool: str
    arguments: JsonObject
    decision: RiskDecision
    summary: str
    reversible: bool = True
    preview: str = ""
    argv: tuple[str, ...] = ()

    @property
    def requires_confirmation(self) -> bool:
        """Нужно ли подтверждение пользователя."""
        return self.decision.requires_confirmation

    @property
    def denied(self) -> bool:
        """Запрещён ли план политикой."""
        return self.decision.denied


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return False


def validate_arguments(schema: Mapping[str, Any], values: Mapping[str, Any]) -> JsonObject:
    """Минимальная проверка JSON-схемы без внешних зависимостей.

    Поддерживаются ``type``, ``required``, ``properties``, ``additionalProperties``,
    ``enum``, ``minLength``, ``minimum``, ``maximum``, ``minItems``, ``maxItems`` и
    ``items``. Этого достаточно для инструментов этапа 3, а внешняя библиотека
    не добавляется в runtime ради одной проверки.
    """
    if not isinstance(values, Mapping):
        raise ToolValidationError("Аргументы инструмента должны быть объектом")

    properties = schema.get("properties", {})
    required = schema.get("required", ())
    additional = schema.get("additionalProperties", True)
    result = dict(values)

    missing = [key for key in required if key not in result]
    if missing:
        raise ToolValidationError(f"Не указаны обязательные аргументы: {', '.join(missing)}")

    if additional is False:
        unknown = sorted(set(result) - set(properties))
        if unknown:
            raise ToolValidationError(f"Неизвестные аргументы: {', '.join(unknown)}")

    for key, spec in properties.items():
        if key not in result:
            if "default" in spec:
                result[key] = spec["default"]
            continue
        value = result[key]
        expected_type = spec.get("type")
        if expected_type is not None and not _type_matches(value, expected_type):
            raise ToolValidationError(
                f"Аргумент {key!r} должен иметь тип {expected_type}, "
                f"получено {type(value).__name__}"
            )
        if "enum" in spec and value not in spec["enum"]:
            raise ToolValidationError(
                f"Аргумент {key!r} должен быть одним из: {', '.join(map(str, spec['enum']))}"
            )
        if expected_type == "string":
            if "minLength" in spec and len(value) < spec["minLength"]:
                raise ToolValidationError(f"Аргумент {key!r} слишком короткий")
            if "maxLength" in spec and len(value) > spec["maxLength"]:
                raise ToolValidationError(f"Аргумент {key!r} слишком длинный")
            if "\x00" in value:
                raise ToolValidationError(f"Аргумент {key!r} содержит NUL-символ")
        if expected_type in ("integer", "number"):
            if "minimum" in spec and value < spec["minimum"]:
                raise ToolValidationError(f"Аргумент {key!r} меньше допустимого минимума")
            if "maximum" in spec and value > spec["maximum"]:
                raise ToolValidationError(f"Аргумент {key!r} больше допустимого максимума")
        if expected_type == "array":
            if "minItems" in spec and len(value) < spec["minItems"]:
                raise ToolValidationError(f"Аргумент {key!r} содержит слишком мало элементов")
            if "maxItems" in spec and len(value) > spec["maxItems"]:
                raise ToolValidationError(f"Аргумент {key!r} содержит слишком много элементов")
            item_spec = spec.get("items")
            if isinstance(item_spec, Mapping) and item_spec.get("type"):
                for index, item in enumerate(value):
                    if not _type_matches(item, item_spec["type"]):
                        raise ToolValidationError(
                            f"Элемент {index} аргумента {key!r} должен иметь тип "
                            f"{item_spec['type']}"
                        )
    return result


def _absolute_no_follow(path: str | Path) -> Path:
    """Абсолютный путь с разрешённым родителем, но без перехода по конечному симлинку."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    if candidate.name in ("", ".", ".."):
        return candidate.resolve(strict=False)
    return candidate.parent.resolve(strict=False) / candidate.name


def _json_path(path: Path) -> str:
    return str(path)


def _entry_info(path: Path, *, is_dir: bool | None = None) -> JsonObject:
    try:
        stat_result = path.lstat()
    except OSError as error:
        return {"path": _json_path(path), "error": str(error)}
    if is_dir is None:
        is_dir = path.is_dir()
    kind = "dir" if is_dir else "file"
    if path.is_symlink():
        kind = "symlink"
    return {
        "path": _json_path(path),
        "name": path.name,
        "kind": kind,
        "size": stat_result.st_size,
        "mtime": datetime.fromtimestamp(stat_result.st_mtime, UTC).isoformat(
            timespec="seconds"
        ),
    }


class Tool(ABC):
    """Базовый инструмент с JSON-схемой и аудитом."""

    name: ClassVar[str]
    description: ClassVar[str]
    parameters: ClassVar[JsonObject]

    def __init__(self, policy: PathPolicy, audit: AuditLogger | None = None) -> None:
        self.policy = policy
        self.audit = audit

    def validate(self, arguments: Mapping[str, Any]) -> JsonObject:
        """Проверить аргументы по схеме инструмента."""
        return validate_arguments(self.parameters, arguments)

    @abstractmethod
    def plan(self, arguments: Mapping[str, Any]) -> ToolPlan:
        """Построить план без изменения системы."""

    @abstractmethod
    def _execute(self, arguments: JsonObject, *, confirmed: bool) -> ToolResult:
        """Выполнить уже проверенный план."""

    def execute(
        self,
        plan_or_arguments: ToolPlan | Mapping[str, Any],
        *,
        confirmed: bool = False,
    ) -> ToolResult:
        """Выполнить план после проверки риска и подтверждения."""
        plan = (
            plan_or_arguments
            if isinstance(plan_or_arguments, ToolPlan)
            else self.plan(plan_or_arguments)
        )
        if plan.tool != self.name:
            raise ToolError(
                f"План инструмента {plan.tool!r} нельзя выполнить через {self.name!r}"
            )
        if plan.decision.level is RiskLevel.DENY:
            raise PolicyDeniedError(plan.decision.reason)
        if plan.decision.requires_confirmation and not confirmed:
            raise ConfirmationRequiredError(plan.decision.reason)

        try:
            result = self._execute(plan.arguments, confirmed=confirmed)
        except Exception as error:
            self._write_audit(
                plan,
                ToolResult(False, f"Ошибка выполнения: {error}"),
                confirmed=confirmed,
            )
            raise
        self._write_audit(plan, result, confirmed=confirmed)
        return result

    def _write_audit(self, plan: ToolPlan, result: ToolResult, *, confirmed: bool) -> None:
        if self.audit is None:
            return
        argv = result.argv or plan.argv
        self.audit.write(
            AuditRecord(
                tool=self.name,
                argv=list(argv),
                result=result.message,
                returncode=result.returncode,
                confirmed=confirmed,
                risk=plan.decision.level.value,
                details={
                    "arguments": plan.arguments,
                    "path": (
                        _json_path(plan.decision.path)
                        if plan.decision.path is not None
                        else None
                    ),
                    "ok": result.ok,
                },
            )
        )


class _PathTool(Tool):
    """Общая часть файловых инструментов."""

    operation: ClassVar[Operation]

    def _decision(self, path: str | Path, *, recursive: bool = False) -> RiskDecision:
        return self.policy.check(path, self.operation, recursive=recursive)

    def _require(
        self, path: str | Path, *, recursive: bool = False, confirmed: bool
    ) -> Path:
        decision = self.policy.require_allowed(
            path,
            self.operation,
            recursive=recursive,
            confirmed=confirmed,
        )
        if decision.path is None:
            return self.policy.resolve(path)
        return decision.path


class FindFilesTool(_PathTool):
    name = "find_files"
    description = "Найти файлы и каталоги по glob-шаблону."
    operation = Operation.LIST
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "pattern": {"type": "string", "minLength": 1, "default": "*"},
            "recursive": {"type": "boolean", "default": True},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def plan(self, arguments: Mapping[str, Any]) -> ToolPlan:
        args = self.validate(arguments)
        resolved = self.policy.resolve(args["path"])
        pattern = args["pattern"]
        if "/" in pattern or "\\" in pattern or pattern in (".", ".."):
            raise ToolValidationError("Шаблон должен быть именем файла, а не путём")
        recursive = args["recursive"]
        decision = self._decision(resolved, recursive=recursive)
        mode = "рекурсивно" if recursive else "без рекурсии"
        return ToolPlan(
            self.name,
            args,
            decision,
            f"Найти «{args['pattern']}» в {resolved} ({mode})",
            reversible=True,
        )

    def _execute(self, arguments: JsonObject, *, confirmed: bool) -> ToolResult:
        path = self._require(
            arguments["path"],
            recursive=arguments["recursive"],
            confirmed=confirmed,
        )
        pattern = arguments["pattern"]
        if "/" in pattern or "\\" in pattern or pattern in (".", ".."):
            return ToolResult(False, "Шаблон должен быть именем файла, а не путём")
        if not path.exists() and not path.is_symlink():
            return ToolResult(False, f"Путь не существует: {path}")

        max_results = arguments["max_results"]
        items: list[JsonObject] = []
        truncated = False

        def add(entry: Path, is_dir: bool | None = None) -> bool:
            nonlocal truncated
            if not fnmatch.fnmatch(entry.name, pattern):
                return False
            if entry.is_symlink() and not self.policy.is_within_allowed(entry.resolve()):
                return False
            items.append(_entry_info(entry, is_dir=is_dir))
            if len(items) >= max_results:
                truncated = True
                return True
            return False

        if path.is_dir():
            if arguments["recursive"]:
                for root, dirs, files in os.walk(path, followlinks=False):
                    dirs[:] = sorted(
                        directory
                        for directory in dirs
                        if not (Path(root) / directory).is_symlink()
                    )
                    for name in dirs:
                        if add(Path(root) / name, is_dir=True):
                            break
                    for name in files:
                        if add(Path(root) / name, is_dir=False):
                            break
                    if truncated:
                        break
            else:
                for entry in sorted(path.iterdir(), key=lambda item: item.name):
                    if add(entry):
                        break
        elif not add(path, is_dir=False):
            items = []

        return ToolResult(
            True,
            f"Найдено: {len(items)}",
            {"items": items, "count": len(items), "truncated": truncated},
        )


class ListDirTool(_PathTool):
    name = "list_dir"
    description = "Показать содержимое каталога."
    operation = Operation.LIST
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "include_hidden": {"type": "boolean", "default": False},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def plan(self, arguments: Mapping[str, Any]) -> ToolPlan:
        args = self.validate(arguments)
        resolved = self.policy.resolve(args["path"])
        decision = self._decision(resolved)
        return ToolPlan(self.name, args, decision, f"Показать содержимое {resolved}")

    def _execute(self, arguments: JsonObject, *, confirmed: bool) -> ToolResult:
        path = self._require(arguments["path"], confirmed=confirmed)
        if not path.is_dir():
            return ToolResult(False, f"Это не каталог: {path}")
        items: list[JsonObject] = []
        for entry in sorted(path.iterdir(), key=lambda item: item.name):
            if not arguments["include_hidden"] and entry.name.startswith("."):
                continue
            if entry.is_symlink() and not self.policy.is_within_allowed(entry.resolve()):
                continue
            items.append(_entry_info(entry))
        return ToolResult(
            True,
            f"Элементов: {len(items)}",
            {"items": items, "count": len(items)},
        )


class ReadInfoTool(_PathTool):
    name = "read_info"
    description = "Прочитать метаданные файла или каталога без вывода содержимого."
    operation = Operation.READ
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "minLength": 1}},
        "required": ["path"],
        "additionalProperties": False,
    }

    def plan(self, arguments: Mapping[str, Any]) -> ToolPlan:
        args = self.validate(arguments)
        resolved = self.policy.resolve(args["path"])
        return ToolPlan(
            self.name,
            args,
            self._decision(resolved),
            f"Прочитать сведения о {resolved}",
        )

    def _execute(self, arguments: JsonObject, *, confirmed: bool) -> ToolResult:
        path = self._require(arguments["path"], confirmed=confirmed)
        try:
            info = path.lstat()
        except OSError as error:
            return ToolResult(False, f"Не удалось прочитать {path}: {error}")
        resolved = path.resolve(strict=False)
        kind = "symlink" if path.is_symlink() else ("dir" if path.is_dir() else "file")
        data = {
            "path": _json_path(path),
            "resolved_path": _json_path(resolved),
            "kind": kind,
            "size": info.st_size,
            "mode": oct(info.st_mode & 0o777),
            "mtime": datetime.fromtimestamp(info.st_mtime, UTC).isoformat(timespec="seconds"),
            "is_symlink": path.is_symlink(),
        }
        return ToolResult(True, f"Сведения о {path}", data)


class MkdirTool(_PathTool):
    name = "mkdir"
    description = "Создать каталог внутри разрешённой области."
    operation = Operation.MKDIR
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "parents": {"type": "boolean", "default": False},
            "exist_ok": {"type": "boolean", "default": False},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def plan(self, arguments: Mapping[str, Any]) -> ToolPlan:
        args = self.validate(arguments)
        resolved = self.policy.resolve(args["path"])
        return ToolPlan(
            self.name,
            args,
            self._decision(resolved),
            f"Создать каталог {resolved}",
            reversible=True,
        )

    def _execute(self, arguments: JsonObject, *, confirmed: bool) -> ToolResult:
        path = self._require(arguments["path"], confirmed=confirmed)
        existed = path.exists()
        try:
            path.mkdir(parents=arguments["parents"], exist_ok=arguments["exist_ok"])
        except FileExistsError:
            return ToolResult(False, f"Каталог уже существует: {path}")
        except OSError as error:
            return ToolResult(False, f"Не удалось создать {path}: {error}")
        if not path.is_dir():
            return ToolResult(False, f"Путь не стал каталогом: {path}")
        return ToolResult(
            True,
            f"Каталог создан: {path}" if not existed else f"Каталог уже существует: {path}",
            {"path": _json_path(path), "created": not existed},
        )


class TrashManager:
    """Минимальная реализация XDG Trash для обратимого удаления."""

    def __init__(self, trash_dir: str | Path | None = None) -> None:
        root = Path(trash_dir).expanduser() if trash_dir else Path.home() / ".local/share/Trash"
        self.files_dir = root / "files"
        self.info_dir = root / "info"

    def _unique_name(self, name: str) -> str:
        candidate = name
        index = 1
        while (
            (self.files_dir / candidate).exists()
            or (self.files_dir / candidate).is_symlink()
            or (self.info_dir / f"{candidate}.trashinfo").exists()
        ):
            candidate = f"{name}.{index}"
            index += 1
        return candidate

    def trash(self, source: Path) -> tuple[Path, Path]:
        """Переместить объект в корзину и вернуть (файл в корзине, .trashinfo)."""
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.info_dir.mkdir(parents=True, exist_ok=True)
        name = source.name or "item"
        name = self._unique_name(name)
        destination = self.files_dir / name
        info_path = self.info_dir / f"{name}.trashinfo"
        info = (
            "[Trash Info]\n"
            f"Path={quote(str(source), safe='/')}\n"
            f"DeletionDate={datetime.now(UTC).isoformat(timespec='seconds')}\n"
        )
        info_path.write_text(info, encoding="utf-8")
        try:
            shutil.move(str(source), str(destination))
        except Exception:
            info_path.unlink(missing_ok=True)
            raise
        return destination, info_path


class MoveToTrashTool(_PathTool):
    name = "move_to_trash"
    description = "Переместить файл или каталог в корзину (обратимое удаление)."
    operation = Operation.MOVE
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "minLength": 1}},
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        policy: PathPolicy,
        audit: AuditLogger | None = None,
        *,
        trash_dir: str | Path | None = None,
    ) -> None:
        super().__init__(policy, audit)
        self.trash_manager = TrashManager(trash_dir)

    def plan(self, arguments: Mapping[str, Any]) -> ToolPlan:
        args = self.validate(arguments)
        source = _absolute_no_follow(args["path"])
        return ToolPlan(
            self.name,
            args,
            self._decision(source),
            f"Переместить в корзину {source}",
            reversible=True,
        )

    def _execute(self, arguments: JsonObject, *, confirmed: bool) -> ToolResult:
        source = _absolute_no_follow(arguments["path"])
        self._require(source, confirmed=confirmed)
        if not source.exists() and not source.is_symlink():
            return ToolResult(False, f"Путь не существует: {source}")
        try:
            destination, info_path = self.trash_manager.trash(source)
        except OSError as error:
            return ToolResult(False, f"Не удалось переместить {source} в корзину: {error}")
        if source.exists() and not source.is_symlink():
            return ToolResult(False, f"Источник всё ещё существует: {source}")
        if not destination.exists() and not destination.is_symlink():
            return ToolResult(False, f"Объект не найден в корзине: {destination}")
        return ToolResult(
            True,
            f"Перемещено в корзину: {source}",
            {
                "source": _json_path(source),
                "trash_path": _json_path(destination),
                "trashinfo": _json_path(info_path),
            },
        )


class DeleteTool(_PathTool):
    name = "delete"
    description = "Безвозвратно удалить файл или каталог внутри домашней области."
    operation = Operation.DELETE
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "recursive": {"type": "boolean", "default": False},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def plan(self, arguments: Mapping[str, Any]) -> ToolPlan:
        args = self.validate(arguments)
        source = _absolute_no_follow(args["path"])
        return ToolPlan(
            self.name,
            args,
            self._decision(source, recursive=args["recursive"]),
            f"Безвозвратно удалить {source}",
            reversible=False,
        )

    def _execute(self, arguments: JsonObject, *, confirmed: bool) -> ToolResult:
        source = _absolute_no_follow(arguments["path"])
        self._require(
            source,
            recursive=arguments["recursive"],
            confirmed=confirmed,
        )
        if not source.exists() and not source.is_symlink():
            return ToolResult(False, f"Путь не существует: {source}")
        is_directory = source.is_dir() and not source.is_symlink()
        if is_directory and not arguments["recursive"]:
            return ToolResult(
                False,
                f"Каталог {source} требует recursive=true; без этого удаление запрещено",
            )
        try:
            if is_directory:
                shutil.rmtree(source)
            else:
                source.unlink()
        except OSError as error:
            return ToolResult(False, f"Не удалось удалить {source}: {error}")
        if source.exists() or source.is_symlink():
            return ToolResult(False, f"Путь всё ещё существует: {source}")
        return ToolResult(True, f"Удалено: {source}", {"path": _json_path(source)})


class PackageTool(Tool):
    """Общая часть инструментов поверх PackageManager."""

    def __init__(
        self,
        policy: PathPolicy,
        manager: PackageManager,
        audit: AuditLogger | None = None,
    ) -> None:
        super().__init__(policy, audit)
        self.manager = manager

    def _preview(self, builder: Any, *args: Any) -> tuple[str, tuple[str, ...]]:
        try:
            command = builder(*args, dry_run=True)
        except DryRunNotSupportedError as error:
            return f"Dry-run недоступен: {error}", ()
        except PackageManagerError as error:
            raise ToolValidationError(str(error)) from error
        return " ".join(command.argv), command.argv


class PackageSearchTool(PackageTool):
    name = "package_search"
    description = "Найти пакеты в настроенном пакетном менеджере."
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string", "minLength": 1}},
        "required": ["query"],
        "additionalProperties": False,
    }

    def plan(self, arguments: Mapping[str, Any]) -> ToolPlan:
        args = self.validate(arguments)
        return ToolPlan(
            self.name,
            args,
            RiskDecision(RiskLevel.SAFE, f"Поиск пакетов: {args['query']}"),
            f"Найти пакеты по запросу «{args['query']}»",
        )

    def _execute(self, arguments: JsonObject, *, confirmed: bool) -> ToolResult:
        try:
            found = self.manager.search(arguments["query"])
        except PackageManagerError as error:
            return ToolResult(False, f"Поиск не удался: {error}")
        items = [{"name": item.name, "summary": item.summary} for item in found]
        return ToolResult(True, f"Найдено пакетов: {len(items)}", {"items": items})


class PackageIsInstalledTool(PackageTool):
    name = "package_is_installed"
    description = "Проверить, установлен ли пакет."
    parameters = {
        "type": "object",
        "properties": {"package": {"type": "string", "minLength": 1}},
        "required": ["package"],
        "additionalProperties": False,
    }

    def plan(self, arguments: Mapping[str, Any]) -> ToolPlan:
        args = self.validate(arguments)
        return ToolPlan(
            self.name,
            args,
            RiskDecision(RiskLevel.SAFE, f"Проверка пакета: {args['package']}"),
            f"Проверить установку пакета {args['package']}",
        )

    def _execute(self, arguments: JsonObject, *, confirmed: bool) -> ToolResult:
        try:
            installed = self.manager.is_installed(arguments["package"])
        except PackageManagerError as error:
            return ToolResult(False, f"Проверка не удалась: {error}")
        return ToolResult(
            True,
            f"Пакет {'установлен' if installed else 'не установлен'}: {arguments['package']}",
            {"package": arguments["package"], "installed": installed},
        )


class PackageInstallTool(PackageTool):
    name = "package_install"
    description = "Установить пакеты через выбранный пакетный менеджер."
    parameters = {
        "type": "object",
        "properties": {
            "packages": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
                "maxItems": 50,
            }
        },
        "required": ["packages"],
        "additionalProperties": False,
    }

    def plan(self, arguments: Mapping[str, Any]) -> ToolPlan:
        args = self.validate(arguments)
        preview, argv = self._preview(self.manager.build_install, args["packages"])
        return ToolPlan(
            self.name,
            args,
            RiskDecision(RiskLevel.SAFE, f"Установка пакетов: {', '.join(args['packages'])}"),
            f"Установить пакеты: {', '.join(args['packages'])}",
            reversible=True,
            preview=preview,
            argv=argv,
        )

    def _execute(self, arguments: JsonObject, *, confirmed: bool) -> ToolResult:
        packages = arguments["packages"]
        try:
            result = self.manager.install(packages)
        except PackageManagerError as error:
            return ToolResult(False, f"Установка не удалась: {error}")
        if not result.success:
            detail = result.stderr.strip() or result.stdout.strip() or "нет вывода"
            return ToolResult(
                False,
                f"Установка не удалась: код {result.returncode}, {detail}",
                argv=result.argv,
                returncode=result.returncode,
            )
        try:
            missing = [package for package in packages if not self.manager.is_installed(package)]
        except PackageManagerError as error:
            return ToolResult(
                False,
                f"Команда выполнена, но проверка установки не удалась: {error}",
                argv=result.argv,
                returncode=result.returncode,
            )
        if missing:
            return ToolResult(
                False,
                f"Пакеты не подтверждены как установленные: {', '.join(missing)}",
                argv=result.argv,
                returncode=result.returncode,
            )
        return ToolResult(
            True,
            f"Установлено: {', '.join(packages)}",
            {"packages": packages},
            argv=result.argv,
            returncode=result.returncode,
        )


class PackageRemoveTool(PackageTool):
    name = "package_remove"
    description = "Удалить пакеты; требует подтверждения."
    parameters = {
        "type": "object",
        "properties": {
            "packages": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
                "maxItems": 50,
            }
        },
        "required": ["packages"],
        "additionalProperties": False,
    }

    def plan(self, arguments: Mapping[str, Any]) -> ToolPlan:
        args = self.validate(arguments)
        preview, argv = self._preview(self.manager.build_remove, args["packages"])
        return ToolPlan(
            self.name,
            args,
            RiskDecision(
                RiskLevel.CONFIRM,
                f"Удаление пакетов: {', '.join(args['packages'])}",
            ),
            f"Удалить пакеты: {', '.join(args['packages'])}",
            reversible=False,
            preview=preview,
            argv=argv,
        )

    def _execute(self, arguments: JsonObject, *, confirmed: bool) -> ToolResult:
        packages = arguments["packages"]
        try:
            result = self.manager.remove(packages)
        except PackageManagerError as error:
            return ToolResult(False, f"Удаление не удалось: {error}")
        if not result.success:
            detail = result.stderr.strip() or result.stdout.strip() or "нет вывода"
            return ToolResult(
                False,
                f"Удаление не удалось: код {result.returncode}, {detail}",
                argv=result.argv,
                returncode=result.returncode,
            )
        try:
            still_installed = [
                package for package in packages if self.manager.is_installed(package)
            ]
        except PackageManagerError as error:
            return ToolResult(
                False,
                f"Команда выполнена, но проверка удаления не удалась: {error}",
                argv=result.argv,
                returncode=result.returncode,
            )
        if still_installed:
            return ToolResult(
                False,
                f"Пакеты всё ещё установлены: {', '.join(still_installed)}",
                argv=result.argv,
                returncode=result.returncode,
            )
        return ToolResult(
            True,
            f"Удалено: {', '.join(packages)}",
            {"packages": packages},
            argv=result.argv,
            returncode=result.returncode,
        )


class PackageUpdateIndexTool(PackageTool):
    name = "package_update_index"
    description = "Обновить индекс пакетов."
    parameters = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def plan(self, arguments: Mapping[str, Any]) -> ToolPlan:
        args = self.validate(arguments)
        preview, argv = self._preview(self.manager.build_update_index)
        return ToolPlan(
            self.name,
            args,
            RiskDecision(RiskLevel.SAFE, "Обновление индекса пакетов"),
            "Обновить индекс пакетов",
            preview=preview,
            argv=argv,
        )

    def _execute(self, arguments: JsonObject, *, confirmed: bool) -> ToolResult:
        try:
            result = self.manager.update_index()
        except PackageManagerError as error:
            return ToolResult(False, f"Обновление индекса не удалось: {error}")
        if not result.success:
            detail = result.stderr.strip() or result.stdout.strip() or "нет вывода"
            return ToolResult(
                False,
                f"Обновление индекса не удалось: код {result.returncode}, {detail}",
                argv=result.argv,
                returncode=result.returncode,
            )
        return ToolResult(
            True,
            "Индекс пакетов обновлён",
            argv=result.argv,
            returncode=result.returncode,
        )


class PackageCleanCacheTool(PackageTool):
    name = "package_clean_cache"
    description = "Очистить кэш пакетного менеджера."
    parameters = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def plan(self, arguments: Mapping[str, Any]) -> ToolPlan:
        args = self.validate(arguments)
        preview, argv = self._preview(self.manager.build_clean_cache)
        return ToolPlan(
            self.name,
            args,
            RiskDecision(RiskLevel.SAFE, "Очистка кэша пакетов"),
            "Очистить кэш пакетного менеджера",
            preview=preview,
            argv=argv,
        )

    def _execute(self, arguments: JsonObject, *, confirmed: bool) -> ToolResult:
        try:
            result = self.manager.clean_cache()
        except PackageManagerError as error:
            return ToolResult(False, f"Очистка кэша не удалась: {error}")
        if not result.success:
            detail = result.stderr.strip() or result.stdout.strip() or "нет вывода"
            return ToolResult(
                False,
                f"Очистка кэша не удалась: код {result.returncode}, {detail}",
                argv=result.argv,
                returncode=result.returncode,
            )
        return ToolResult(
            True,
            "Кэш пакетов очищен",
            argv=result.argv,
            returncode=result.returncode,
        )


class ToolRegistry:
    """Реестр инструментов по имени."""

    def __init__(self, tools: Sequence[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ToolError(f"Инструмент уже зарегистрирован: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as error:
            raise ToolError(f"Неизвестный инструмент: {name}") from error

    def schemas(self) -> list[JsonObject]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for tool in self._tools.values()
        ]

    def plan(self, name: str, arguments: Mapping[str, Any]) -> ToolPlan:
        return self.get(name).plan(arguments)

    def execute(
        self,
        name: str,
        arguments: ToolPlan | Mapping[str, Any],
        *,
        confirmed: bool = False,
    ) -> ToolResult:
        tool = self.get(name)
        if isinstance(arguments, ToolPlan) and arguments.tool != tool.name:
            raise ToolError(
                f"План инструмента {arguments.tool!r} нельзя выполнить через {tool.name!r}"
            )
        return tool.execute(arguments, confirmed=confirmed)


def default_tools(
    *,
    policy: PathPolicy | None = None,
    manager: PackageManager | None = None,
    audit: AuditLogger | None = None,
    trash_dir: str | Path | None = None,
) -> list[Tool]:
    """Собрать стандартный набор инструментов этапа 3."""
    policy = policy or PathPolicy()
    tools: list[Tool] = [
        FindFilesTool(policy, audit),
        ListDirTool(policy, audit),
        ReadInfoTool(policy, audit),
        MkdirTool(policy, audit),
        MoveToTrashTool(policy, audit, trash_dir=trash_dir),
        DeleteTool(policy, audit),
    ]
    if manager is not None:
        tools.extend(
            [
                PackageSearchTool(policy, manager, audit),
                PackageIsInstalledTool(policy, manager, audit),
                PackageInstallTool(policy, manager, audit),
                PackageRemoveTool(policy, manager, audit),
                PackageUpdateIndexTool(policy, manager, audit),
                PackageCleanCacheTool(policy, manager, audit),
            ]
        )
    return tools
