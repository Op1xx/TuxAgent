"""Политика безопасности путей и классификатор риска.

Правила:

* безопасные корни по умолчанию — домашний каталог и ``/tmp``;
* защищённые пути нельзя изменять и нельзя обходить рекурсивно;
* путь всегда приводится к ``realpath``, поэтому ``..`` и симлинки не обходят
  проверку;
* удаление и перемещение вне домашнего каталога запрещены, а внутри — требуют
  подтверждения.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

DEFAULT_PROTECTED_PATHS: tuple[Path, ...] = (
    Path("/"),
    Path("/etc"),
    Path("/usr"),
    Path("/boot"),
    Path("/dev"),
    Path("/proc"),
    Path("/sys"),
    Path("/var/lib"),
)


class RiskLevel(StrEnum):
    """Итог классификатора риска."""

    SAFE = "safe"
    CONFIRM = "confirm"
    DENY = "deny"


class Operation(StrEnum):
    """Операция, для которой проверяется путь."""

    READ = "read"
    LIST = "list"
    MKDIR = "mkdir"
    WRITE = "write"
    MOVE = "move"
    DELETE = "delete"


class PolicyError(RuntimeError):
    """Базовая ошибка политики."""


class PolicyDeniedError(PolicyError):
    """Операция запрещена политикой."""


class ConfirmationRequiredError(PolicyError):
    """Для операции требуется подтверждение пользователя."""


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """Решение классификатора: уровень, причина и разрешённый путь."""

    level: RiskLevel
    reason: str
    path: Path | None = None
    operation: Operation | None = None
    recursive: bool = False

    @property
    def requires_confirmation(self) -> bool:
        """Нужно ли спрашивать пользователя."""
        return self.level is RiskLevel.CONFIRM

    @property
    def denied(self) -> bool:
        """Запрещена ли операция."""
        return self.level is RiskLevel.DENY


def _is_under(path: Path, root: Path) -> bool:
    """Путь совпадает с корнем или лежит внутри него."""
    return path == root or root in path.parents


class PathPolicy:
    """Проверка путей и классификация риска для файловых инструментов."""

    def __init__(
        self,
        allowed_roots: tuple[Path, ...] | None = None,
        protected_paths: tuple[Path, ...] | None = None,
    ) -> None:
        roots = allowed_roots or (Path.home(), Path("/tmp"))
        protected = protected_paths or DEFAULT_PROTECTED_PATHS
        self.allowed_roots = tuple(self._normalize(root) for root in roots)
        self.protected_paths = tuple(self._normalize(root) for root in protected)

    @staticmethod
    def _normalize(path: Path | str) -> Path:
        candidate = Path(path).expanduser()
        try:
            return candidate.resolve(strict=False)
        except (OSError, RuntimeError) as error:
            raise PolicyError(f"Не удалось разрешить путь {candidate}: {error}") from error

    def resolve(self, path: str | Path) -> Path:
        """Привести путь к абсолютному ``realpath`` без требования существования."""
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        try:
            return candidate.resolve(strict=False)
        except (OSError, RuntimeError) as error:
            raise PolicyError(f"Не удалось разрешить путь {candidate}: {error}") from error

    def is_within_allowed(self, path: str | Path) -> bool:
        """Лежит ли путь внутри одного из разрешённых корней."""
        resolved = self.resolve(path)
        return any(_is_under(resolved, root) for root in self.allowed_roots)

    def is_protected(self, path: str | Path) -> bool:
        """Лежит ли путь внутри защищённого каталога.

        Корень ``/`` защищён только сам по себе: иначе любой абсолютный путь
        считался бы вложенным в защищённый каталог.
        """
        resolved = self.resolve(path)
        for root in self.protected_paths:
            if resolved == root:
                return True
            if root != Path("/") and _is_under(resolved, root):
                return True
        return False

    def is_allowed_root(self, path: str | Path) -> bool:
        """Совпадает ли путь с корнем, который нельзя удалить или переместить."""
        resolved = self.resolve(path)
        return any(resolved == root for root in self.allowed_roots)

    def check(
        self,
        path: str | Path,
        operation: Operation,
        *,
        recursive: bool = False,
    ) -> RiskDecision:
        """Классифицировать операцию над путём: safe / confirm / deny."""
        resolved = self.resolve(path)
        protected = self.is_protected(resolved)
        within_allowed = self.is_within_allowed(resolved)

        if operation in (Operation.READ, Operation.LIST):
            if recursive and protected:
                return RiskDecision(
                    RiskLevel.DENY,
                    f"Рекурсивные операции по защищённому пути {resolved} запрещены",
                    resolved,
                    operation,
                    recursive,
                )
            if recursive and not within_allowed:
                return RiskDecision(
                    RiskLevel.CONFIRM,
                    f"Рекурсивный поиск вне ~ и /tmp затронет {resolved}",
                    resolved,
                    operation,
                    recursive,
                )
            return RiskDecision(
                RiskLevel.SAFE,
                f"Чтение {resolved}",
                resolved,
                operation,
                recursive,
            )

        if operation in (Operation.MKDIR, Operation.WRITE):
            if protected:
                return RiskDecision(
                    RiskLevel.DENY,
                    f"Изменение защищённого пути {resolved} запрещено",
                    resolved,
                    operation,
                    recursive,
                )
            if not within_allowed:
                return RiskDecision(
                    RiskLevel.CONFIRM,
                    f"Запись вне ~ и /tmp изменит {resolved}",
                    resolved,
                    operation,
                    recursive,
                )
            return RiskDecision(
                RiskLevel.SAFE,
                f"Запись внутри разрешённого каталога: {resolved}",
                resolved,
                operation,
                recursive,
            )

        if operation in (Operation.MOVE, Operation.DELETE):
            if self.is_allowed_root(resolved):
                return RiskDecision(
                    RiskLevel.DENY,
                    f"Нельзя удалить или переместить корень {resolved}",
                    resolved,
                    operation,
                    recursive,
                )
            if protected:
                return RiskDecision(
                    RiskLevel.DENY,
                    f"Удаление или перемещение защищённого пути {resolved} запрещено",
                    resolved,
                    operation,
                    recursive,
                )
            if not within_allowed:
                return RiskDecision(
                    RiskLevel.DENY,
                    f"Удаление и перемещение вне ~ и /tmp запрещены: {resolved}",
                    resolved,
                    operation,
                    recursive,
                )
            return RiskDecision(
                RiskLevel.CONFIRM,
                f"Будет затронут пользовательский путь {resolved}",
                resolved,
                operation,
                recursive,
            )

        return RiskDecision(
            RiskLevel.DENY,
            f"Неизвестная операция: {operation}",
            resolved,
            operation,
            recursive,
        )

    def require_allowed(
        self,
        path: str | Path,
        operation: Operation,
        *,
        recursive: bool = False,
        confirmed: bool = False,
    ) -> RiskDecision:
        """Проверить путь и сразу выбросить ошибку для deny/confirm."""
        decision = self.check(path, operation, recursive=recursive)
        if decision.level is RiskLevel.DENY:
            raise PolicyDeniedError(decision.reason)
        if decision.level is RiskLevel.CONFIRM and not confirmed:
            raise ConfirmationRequiredError(decision.reason)
        return decision
