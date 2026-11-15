"""Тесты типизированных инструментов, риска и audit-журнала."""

from __future__ import annotations

from pathlib import Path

import pytest

from audit import AuditLogger
from package_managers import Command, CommandResult, PackageManagerError, PackageSearchResult
from safety import (
    DEFAULT_PROTECTED_PATHS,
    ConfirmationRequiredError,
    PathPolicy,
    PolicyDeniedError,
)
from tools import (
    DeleteTool,
    FindFilesTool,
    ListDirTool,
    MkdirTool,
    MoveToTrashTool,
    PackageCleanCacheTool,
    PackageInstallTool,
    PackageRemoveTool,
    PackageSearchTool,
    PackageUpdateIndexTool,
    ReadInfoTool,
    ToolError,
    ToolRegistry,
    ToolValidationError,
    default_tools,
    validate_arguments,
)


class FakePackageManager:
    """Детерминированный пакетный менеджер без subprocess."""

    def __init__(self, installed: set[str] | None = None) -> None:
        self.installed = set(installed or ())
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.fail_install = False
        self.fail_remove = False
        self.fail_search = False

    def _command(self, name: str, *args: str, dry_run: bool = False) -> Command:
        return Command((name, *args), requires_root=True, dry_run=dry_run)

    def build_install(self, packages: list[str], dry_run: bool = False) -> Command:
        return self._command("fake-install", *packages, dry_run=dry_run)

    def build_remove(self, packages: list[str], dry_run: bool = False) -> Command:
        return self._command("fake-remove", *packages, dry_run=dry_run)

    def build_update_index(self, dry_run: bool = False) -> Command:
        return self._command("fake-update", dry_run=dry_run)

    def build_clean_cache(self, dry_run: bool = False) -> Command:
        return self._command("fake-clean", dry_run=dry_run)

    def build_is_installed(self, package: str) -> Command:
        return self._command("fake-is-installed", package)

    def build_search(self, query: str) -> Command:
        return self._command("fake-search", query)

    def install(self, packages: list[str], dry_run: bool = False) -> CommandResult:
        self.calls.append(("install", tuple(packages)))
        if self.fail_install:
            raise PackageManagerError("install failed")
        self.installed.update(packages)
        return CommandResult(("fake-install", *packages), 0)

    def remove(self, packages: list[str], dry_run: bool = False) -> CommandResult:
        self.calls.append(("remove", tuple(packages)))
        if self.fail_remove:
            raise PackageManagerError("remove failed")
        self.installed.difference_update(packages)
        return CommandResult(("fake-remove", *packages), 0)

    def update_index(self, dry_run: bool = False) -> CommandResult:
        self.calls.append(("update_index", ()))
        return CommandResult(("fake-update",), 0)

    def clean_cache(self, dry_run: bool = False) -> CommandResult:
        self.calls.append(("clean_cache", ()))
        return CommandResult(("fake-clean",), 0)

    def is_installed(self, package: str) -> bool:
        return package in self.installed

    def search(self, query: str) -> list[PackageSearchResult]:
        self.calls.append(("search", (query,)))
        if self.fail_search:
            raise PackageManagerError("search failed")
        return [PackageSearchResult("vlc", "медиаплеер")]


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def policy(home: Path) -> PathPolicy:
    return PathPolicy(
        allowed_roots=(home,),
        protected_paths=DEFAULT_PROTECTED_PATHS,
    )


@pytest.fixture
def audit(tmp_path: Path) -> AuditLogger:
    return AuditLogger(tmp_path / "data")


@pytest.fixture
def manager() -> FakePackageManager:
    return FakePackageManager()


def test_validate_arguments_applies_defaults_and_rejects_unknown() -> None:
    schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "recursive": {"type": "boolean", "default": True},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    assert validate_arguments(schema, {"path": "/tmp"}) == {
        "path": "/tmp",
        "recursive": True,
    }
    with pytest.raises(ToolValidationError):
        validate_arguments(schema, {})
    with pytest.raises(ToolValidationError):
        validate_arguments(schema, {"path": "/tmp", "extra": 1})
    with pytest.raises(ToolValidationError):
        validate_arguments(schema, {"path": 1})


def test_validate_arguments_checks_array_items() -> None:
    schema = {
        "type": "object",
        "properties": {
            "packages": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
            }
        },
        "required": ["packages"],
        "additionalProperties": False,
    }

    with pytest.raises(ToolValidationError):
        validate_arguments(schema, {"packages": ["vlc", 3]})


def test_find_files_and_list_dir(policy: PathPolicy, home: Path) -> None:
    (home / "a.txt").write_text("a", encoding="utf-8")
    (home / "b.log").write_text("b", encoding="utf-8")
    sub = home / "sub"
    sub.mkdir()
    (sub / "c.txt").write_text("c", encoding="utf-8")
    (home / ".hidden.txt").write_text("h", encoding="utf-8")

    finder = FindFilesTool(policy)
    plan = finder.plan({"path": str(home), "pattern": "*.txt"})
    result = finder.execute(plan)

    assert result.ok
    assert result.data["count"] == 3
    assert {Path(item["name"]) for item in result.data["items"]} == {
        Path("a.txt"),
        Path(".hidden.txt"),
        Path("c.txt"),
    }

    shallow = finder.execute(
        finder.plan({"path": str(home), "pattern": "*.txt", "recursive": False})
    )
    assert shallow.data["count"] == 2

    lister = ListDirTool(policy)
    visible = lister.execute(lister.plan({"path": str(home)}))
    assert {Path(item["name"]) for item in visible.data["items"]} == {
        Path("a.txt"),
        Path("b.log"),
        Path("sub"),
    }

    hidden = lister.execute(lister.plan({"path": str(home), "include_hidden": True}))
    assert {Path(item["name"]) for item in hidden.data["items"]} == {
        Path("a.txt"),
        Path("b.log"),
        Path("sub"),
        Path(".hidden.txt"),
    }


def test_find_files_does_not_follow_escaping_symlink(
    policy: PathPolicy, home: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (home / "leak.txt").symlink_to(outside)

    finder = FindFilesTool(policy)
    result = finder.execute(finder.plan({"path": str(home), "pattern": "*.txt"}))

    assert all(Path(item["path"]) != home / "leak.txt" for item in result.data["items"])


def test_recursive_find_in_protected_path_is_denied(policy: PathPolicy) -> None:
    plan = FindFilesTool(policy).plan({"path": "/etc", "recursive": True})

    assert plan.denied
    with pytest.raises(PolicyDeniedError):
        FindFilesTool(policy).execute(plan)


def test_read_info_and_mkdir(policy: PathPolicy, home: Path) -> None:
    file = home / "notes.txt"
    file.write_text("hello", encoding="utf-8")

    info = ReadInfoTool(policy).execute(ReadInfoTool(policy).plan({"path": str(file)}))
    assert info.ok
    assert info.data["kind"] == "file"
    assert info.data["size"] == 5

    maker = MkdirTool(policy)
    result = maker.execute(maker.plan({"path": str(home / "a" / "b"), "parents": True}))
    assert result.ok
    assert (home / "a" / "b").is_dir()

    again = maker.execute(maker.plan({"path": str(home / "a" / "b")}))
    assert not again.ok
    assert "уже существует" in again.message


def test_mkdir_outside_allowed_requires_confirmation(policy: PathPolicy, tmp_path: Path) -> None:
    maker = MkdirTool(policy)
    plan = maker.plan({"path": str(tmp_path / "outside")})

    assert plan.requires_confirmation
    with pytest.raises(ConfirmationRequiredError):
        maker.execute(plan)

    result = maker.execute(plan, confirmed=True)
    assert result.ok
    assert (tmp_path / "outside").is_dir()


def test_delete_requires_confirmation_and_verifies(policy: PathPolicy, home: Path) -> None:
    file = home / "notes.txt"
    file.write_text("hello", encoding="utf-8")
    deleter = DeleteTool(policy)
    plan = deleter.plan({"path": str(file)})

    assert plan.requires_confirmation
    assert not plan.reversible
    with pytest.raises(ConfirmationRequiredError):
        deleter.execute(plan)

    result = deleter.execute(plan, confirmed=True)
    assert result.ok
    assert not file.exists()


def test_shell_metacharacters_are_literal_paths(policy: PathPolicy, home: Path) -> None:
    path = home / "file; $(touch pwned) `echo x` -- --help.txt"
    maker = MkdirTool(policy)

    result = maker.execute(maker.plan({"path": str(path)}))

    assert result.ok
    assert path.is_dir()


def test_delete_directory_requires_recursive(policy: PathPolicy, home: Path) -> None:
    directory = home / "dir"
    directory.mkdir()
    (directory / "file").write_text("x", encoding="utf-8")
    deleter = DeleteTool(policy)

    not_recursive = deleter.execute(deleter.plan({"path": str(directory)}), confirmed=True)
    assert not not_recursive.ok
    assert directory.exists()

    recursive = deleter.execute(
        deleter.plan({"path": str(directory), "recursive": True}),
        confirmed=True,
    )
    assert recursive.ok
    assert not directory.exists()


def test_root_recursive_delete_is_denied(policy: PathPolicy) -> None:
    deleter = DeleteTool(policy)
    plan = deleter.plan({"path": "/", "recursive": True})

    assert plan.denied
    with pytest.raises(PolicyDeniedError):
        deleter.execute(plan, confirmed=True)


def test_delete_outside_home_and_symlink_escape_are_denied(
    policy: PathPolicy, home: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    deleter = DeleteTool(policy)
    plan = deleter.plan({"path": str(outside)})

    assert plan.denied
    with pytest.raises(PolicyDeniedError):
        deleter.execute(plan)

    link = home / "link.txt"
    link.symlink_to(outside)
    link_plan = deleter.plan({"path": str(link)})
    assert link_plan.denied
    assert link.exists()


def test_delete_symlink_inside_home_removes_link_not_target(
    policy: PathPolicy, home: Path
) -> None:
    target = home / "target.txt"
    target.write_text("x", encoding="utf-8")
    link = home / "link.txt"
    link.symlink_to(target)
    deleter = DeleteTool(policy)

    result = deleter.execute(deleter.plan({"path": str(link)}), confirmed=True)

    assert result.ok
    assert not link.is_symlink()
    assert target.exists()


def test_move_to_trash_is_reversible_and_requires_confirmation(
    policy: PathPolicy, home: Path, tmp_path: Path
) -> None:
    file = home / "notes.txt"
    file.write_text("hello", encoding="utf-8")
    tool = MoveToTrashTool(policy, trash_dir=tmp_path / "trash")
    plan = tool.plan({"path": str(file)})

    assert plan.requires_confirmation
    assert plan.reversible
    with pytest.raises(ConfirmationRequiredError):
        tool.execute(plan)

    result = tool.execute(plan, confirmed=True)

    assert result.ok
    assert not file.exists()
    trashed = Path(result.data["trash_path"])
    assert trashed.is_file()
    assert trashed.read_text(encoding="utf-8") == "hello"
    assert Path(result.data["trashinfo"]).is_file()


def test_audit_records_safe_and_confirmed_actions(
    policy: PathPolicy, home: Path, audit: AuditLogger
) -> None:
    maker = MkdirTool(policy, audit)
    maker.execute(maker.plan({"path": str(home / "dir")}))

    file = home / "delete-me.txt"
    file.write_text("x", encoding="utf-8")
    deleter = DeleteTool(policy, audit)
    deleter.execute(deleter.plan({"path": str(file)}), confirmed=True)

    records = audit.read()
    assert len(records) == 2
    assert records[0]["tool"] == "mkdir"
    assert records[0]["risk"] == "safe"
    assert records[0]["confirmed"] is False
    assert records[0]["details"]["ok"] is True
    assert records[0]["argv"] == []
    assert records[0]["returncode"] is None
    assert records[0]["timestamp"]
    assert records[1]["tool"] == "delete"
    assert records[1]["risk"] == "confirm"
    assert records[1]["confirmed"] is True
    assert records[1]["result"].startswith("Удалено")


def test_registry_reports_schemas_and_rejects_unknown(policy: PathPolicy) -> None:
    registry = ToolRegistry(default_tools(policy=policy))

    names = {schema["name"] for schema in registry.schemas()}
    assert {"find_files", "list_dir", "read_info", "mkdir", "move_to_trash", "delete"} <= names

    with pytest.raises(ToolError, match="Неизвестный инструмент"):
        registry.get("rm_rf")

    plan = registry.plan("find_files", {"path": "."})
    with pytest.raises(ToolError, match="нельзя выполнить"):
        registry.execute("delete", plan)


def test_package_tools_plan_and_execute(
    policy: PathPolicy, audit: AuditLogger, manager: FakePackageManager
) -> None:
    installer = PackageInstallTool(policy, manager, audit)
    install_plan = installer.plan({"packages": ["vlc"]})

    assert not install_plan.requires_confirmation
    assert install_plan.preview == "fake-install vlc"
    install_result = installer.execute(install_plan)
    assert install_result.ok
    assert "vlc" in manager.installed

    remover = PackageRemoveTool(policy, manager, audit)
    remove_plan = remover.plan({"packages": ["vlc"]})
    assert remove_plan.requires_confirmation
    with pytest.raises(ConfirmationRequiredError):
        remover.execute(remove_plan)

    remove_result = remover.execute(remove_plan, confirmed=True)
    assert remove_result.ok
    assert "vlc" not in manager.installed

    searcher = PackageSearchTool(policy, manager, audit)
    search_result = searcher.execute(searcher.plan({"query": "vlc"}))
    assert search_result.ok
    assert search_result.data["items"][0]["name"] == "vlc"

    updater = PackageUpdateIndexTool(policy, manager, audit)
    assert updater.execute(updater.plan({})).ok
    cleaner = PackageCleanCacheTool(policy, manager, audit)
    assert cleaner.execute(cleaner.plan({})).ok

    records = audit.read()
    assert [record["tool"] for record in records] == [
        "package_install",
        "package_remove",
        "package_search",
        "package_update_index",
        "package_clean_cache",
    ]
    assert records[0]["argv"] == ["fake-install", "vlc"]
    assert records[0]["returncode"] == 0
    assert records[0]["confirmed"] is False
    assert records[1]["confirmed"] is True
    assert records[1]["risk"] == "confirm"


def test_package_tool_reports_manager_failure(
    policy: PathPolicy, manager: FakePackageManager
) -> None:
    manager.fail_install = True
    installer = PackageInstallTool(policy, manager)
    result = installer.execute(installer.plan({"packages": ["vlc"]}))

    assert not result.ok
    assert "install failed" in result.message

    manager.fail_search = True
    searcher = PackageSearchTool(policy, manager)
    result = searcher.execute(searcher.plan({"query": "vlc"}))
    assert not result.ok
    assert "search failed" in result.message
