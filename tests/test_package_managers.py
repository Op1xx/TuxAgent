"""Тесты сборки argv, валидации и выбора пакетных бэкендов.

Реальные apt/dnf/pacman/zypper/epm не запускаются: тесты проверяют argv,
разбор вывода и поведение при отсутствии утилит.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

import pytest

from distro import DistroFamily, DistroInfo
from package_managers import (
    AltAptPackageManager,
    AptPackageManager,
    Command,
    CommandExecutionError,
    CommandResult,
    CommandRunner,
    DnfPackageManager,
    DryRunNotSupportedError,
    EpmPackageManager,
    InvalidPackageNameError,
    PackageManagerError,
    PackageManagerNotFoundError,
    PackageSearchResult,
    PacmanPackageManager,
    RootPrivilegesError,
    UnsupportedDistroError,
    ZypperPackageManager,
    create_package_manager,
    create_package_manager_for_settings,
)

MANAGERS = [
    AptPackageManager,
    AltAptPackageManager,
    DnfPackageManager,
    PacmanPackageManager,
    ZypperPackageManager,
    EpmPackageManager,
]


def available_which(name: str) -> str | None:
    return f"/usr/bin/{name}"


def make_manager(manager_class: type) -> object:
    return manager_class(which=available_which)


@dataclass
class FakeRunner:
    """Подменяет выполнение и запоминает команды."""

    results: list[CommandResult]

    def __post_init__(self) -> None:
        self.commands: list[Command] = []

    def run(self, command: Command, timeout: float | None = None) -> CommandResult:
        self.commands.append(command)
        if self.results:
            return self.results.pop(0)
        return CommandResult(command.argv, 0)


@pytest.mark.parametrize(
    ("manager_class", "expected"),
    [
        (AptPackageManager, ("apt-get", "install", "--yes", "vlc")),
        (AltAptPackageManager, ("apt-get", "install", "--yes", "vlc")),
        (DnfPackageManager, ("dnf", "install", "--assumeyes", "vlc")),
        (PacmanPackageManager, ("pacman", "-S", "--noconfirm", "vlc")),
        (ZypperPackageManager, ("zypper", "--non-interactive", "install", "vlc")),
        (EpmPackageManager, ("epm", "install", "-y", "vlc")),
    ],
)
def test_install_argv(manager_class: type, expected: tuple[str, ...]) -> None:
    command = make_manager(manager_class).build_install(["vlc"])

    assert command.argv == expected
    assert command.requires_root is True
    assert command.dry_run is False


@pytest.mark.parametrize(
    ("manager_class", "expected"),
    [
        (AptPackageManager, ("apt-get", "remove", "--yes", "vlc")),
        (AltAptPackageManager, ("apt-get", "remove", "--yes", "vlc")),
        (DnfPackageManager, ("dnf", "remove", "--assumeyes", "vlc")),
        (PacmanPackageManager, ("pacman", "-R", "--noconfirm", "vlc")),
        (ZypperPackageManager, ("zypper", "--non-interactive", "remove", "vlc")),
        (EpmPackageManager, ("epm", "remove", "-y", "vlc")),
    ],
)
def test_remove_argv(manager_class: type, expected: tuple[str, ...]) -> None:
    assert make_manager(manager_class).build_remove(["vlc"]).argv == expected


@pytest.mark.parametrize(
    ("manager_class", "expected"),
    [
        (AptPackageManager, ("apt-get", "--simulate", "install", "--yes", "vlc")),
        (
            AltAptPackageManager,
            ("apt-get", "--simulate", "install", "--yes", "vlc"),
        ),
        (
            DnfPackageManager,
            ("dnf", "install", "--assumeyes", "--setopt=tsflags=test", "vlc"),
        ),
        (PacmanPackageManager, ("pacman", "-S", "--print", "vlc")),
        (
            ZypperPackageManager,
            ("zypper", "--non-interactive", "--dry-run", "install", "vlc"),
        ),
    ],
)
def test_install_dry_run_argv(manager_class: type, expected: tuple[str, ...]) -> None:
    command = make_manager(manager_class).build_install(["vlc"], dry_run=True)

    assert command.argv == expected
    assert command.dry_run is True
    assert command.requires_root is False


def test_epm_dry_run_is_refused() -> None:
    with pytest.raises(DryRunNotSupportedError, match="не подтверждён"):
        make_manager(EpmPackageManager).build_install(["vlc"], dry_run=True)


@pytest.mark.parametrize(
    ("manager_class", "expected"),
    [
        (AptPackageManager, ("apt-get", "--simulate", "remove", "--yes", "vlc")),
        (
            DnfPackageManager,
            ("dnf", "remove", "--assumeyes", "--setopt=tsflags=test", "vlc"),
        ),
        (PacmanPackageManager, ("pacman", "-R", "--print", "vlc")),
        (
            ZypperPackageManager,
            ("zypper", "--non-interactive", "--dry-run", "remove", "vlc"),
        ),
        (EpmPackageManager, ("epm", "--dry-run", "remove", "-y", "vlc")),
    ],
)
def test_remove_dry_run_argv(manager_class: type, expected: tuple[str, ...]) -> None:
    assert make_manager(manager_class).build_remove(["vlc"], dry_run=True).argv == expected


@pytest.mark.parametrize(
    ("manager_class", "expected"),
    [
        (AptPackageManager, ("apt-get", "update")),
        (AltAptPackageManager, ("apt-get", "update")),
        (DnfPackageManager, ("dnf", "makecache")),
        (PacmanPackageManager, ("pacman", "-Sy")),
        (ZypperPackageManager, ("zypper", "--non-interactive", "refresh")),
        (EpmPackageManager, ("epm", "update")),
    ],
)
def test_update_index_argv(manager_class: type, expected: tuple[str, ...]) -> None:
    command = make_manager(manager_class).build_update_index()

    assert command.argv == expected
    assert command.requires_root is True


@pytest.mark.parametrize(
    ("manager_class", "expected"),
    [
        (AptPackageManager, ("apt-get", "clean")),
        (AltAptPackageManager, ("apt-get", "clean")),
        (DnfPackageManager, ("dnf", "clean", "all")),
        (PacmanPackageManager, ("pacman", "-Sc", "--noconfirm")),
        (ZypperPackageManager, ("zypper", "--non-interactive", "clean", "--all")),
        (EpmPackageManager, ("epm", "clean")),
    ],
)
def test_clean_cache_argv(manager_class: type, expected: tuple[str, ...]) -> None:
    assert make_manager(manager_class).build_clean_cache().argv == expected


@pytest.mark.parametrize(
    "manager_class",
    [DnfPackageManager, PacmanPackageManager, EpmPackageManager],
)
def test_unconfirmed_dry_run_is_refused(manager_class: type) -> None:
    manager = make_manager(manager_class)

    for operation in (manager.build_update_index, manager.build_clean_cache):
        with pytest.raises(DryRunNotSupportedError):
            operation(dry_run=True)


@pytest.mark.parametrize(
    ("manager_class", "expected"),
    [
        (AptPackageManager, ("apt-get", "--simulate", "update")),
        (AltAptPackageManager, ("apt-get", "--simulate", "update")),
        (
            ZypperPackageManager,
            ("zypper", "--non-interactive", "--dry-run", "refresh"),
        ),
    ],
)
def test_supported_update_index_dry_run(
    manager_class: type, expected: tuple[str, ...]
) -> None:
    assert make_manager(manager_class).build_update_index(dry_run=True).argv == expected


@pytest.mark.parametrize(
    ("manager_class", "expected"),
    [
        (AptPackageManager, ("apt-get", "--simulate", "clean")),
        (
            ZypperPackageManager,
            ("zypper", "--non-interactive", "--dry-run", "clean", "--all"),
        ),
    ],
)
def test_supported_clean_cache_dry_run(
    manager_class: type, expected: tuple[str, ...]
) -> None:
    assert make_manager(manager_class).build_clean_cache(dry_run=True).argv == expected


@pytest.mark.parametrize(
    ("manager_class", "expected"),
    [
        (AptPackageManager, ("dpkg-query", "-W", "-f=${Status}", "vlc")),
        (AltAptPackageManager, ("rpm", "-q", "vlc")),
        (DnfPackageManager, ("rpm", "-q", "vlc")),
        (PacmanPackageManager, ("pacman", "-Q", "vlc")),
        (ZypperPackageManager, ("rpm", "-q", "vlc")),
        (EpmPackageManager, ("rpm", "-q", "vlc")),
    ],
)
def test_is_installed_argv(manager_class: type, expected: tuple[str, ...]) -> None:
    command = make_manager(manager_class).build_is_installed("vlc")

    assert command.argv == expected
    assert command.requires_root is False


@pytest.mark.parametrize(
    ("manager_class", "expected"),
    [
        (AptPackageManager, ("apt-cache", "search", "--names-only", "video")),
        (AltAptPackageManager, ("apt-cache", "search", "--names-only", "video")),
        (DnfPackageManager, ("dnf", "--quiet", "search", "video")),
        (PacmanPackageManager, ("pacman", "-Ss", "video")),
        (ZypperPackageManager, ("zypper", "--non-interactive", "search", "video")),
        (EpmPackageManager, ("epm", "search", "video")),
    ],
)
def test_search_argv(manager_class: type, expected: tuple[str, ...]) -> None:
    assert make_manager(manager_class).build_search("video").argv == expected


@pytest.mark.parametrize(
    "manager_class",
    [AptPackageManager, AltAptPackageManager, PacmanPackageManager],
)
def test_invalid_package_names_are_rejected(manager_class: type) -> None:
    manager = make_manager(manager_class)

    for package in ("", "-y", "vlc;rm -rf /", "../etc/passwd", "vlc player", "vlc\n"):
        with pytest.raises(InvalidPackageNameError):
            manager.build_install([package])


def test_debian_package_names_with_arch_and_plus() -> None:
    manager = make_manager(AptPackageManager)

    assert manager.build_install(["g++", "libc6:amd64"]).argv[-2:] == (
        "g++",
        "libc6:amd64",
    )


def test_rpm_package_names() -> None:
    manager = make_manager(DnfPackageManager)

    assert manager.build_install(["gcc-c++", "python3.11"]).argv[-2:] == (
        "gcc-c++",
        "python3.11",
    )


def test_alt_apt_uses_rpm_package_names() -> None:
    manager = make_manager(AltAptPackageManager)

    assert manager.build_install(["MyPackage"]).argv[-1] == "MyPackage"


@pytest.mark.parametrize("query", ["", "   ", "-S", "vlc\n--force"])
def test_invalid_search_query_is_rejected(query: str) -> None:
    with pytest.raises(InvalidPackageNameError):
        make_manager(PacmanPackageManager).build_search(query)


def test_search_query_with_spaces_is_one_argv_item() -> None:
    command = make_manager(PacmanPackageManager).build_search("video player")

    assert command.argv == ("pacman", "-Ss", "video player")


def test_is_installed_true_false_and_error() -> None:
    runner = FakeRunner(
        [
            CommandResult(("pacman", "-Q", "vlc"), 0),
            CommandResult(("pacman", "-Q", "vlc"), 1),
            CommandResult(("pacman", "-Q", "vlc"), 2, stderr="database error"),
        ]
    )
    manager = PacmanPackageManager(runner, which=available_which)

    assert manager.is_installed("vlc") is True
    assert manager.is_installed("vlc") is False
    with pytest.raises(PackageManagerError, match="database error"):
        manager.is_installed("vlc")


@pytest.mark.parametrize(
    ("manager_class", "output", "expected"),
    [
        (
            AptPackageManager,
            "vlc - multimedia player\nvlc-plugin - plugins\nnot a result\n",
            [
                PackageSearchResult("vlc", "multimedia player"),
                PackageSearchResult("vlc-plugin", "plugins"),
            ],
        ),
        (
            DnfPackageManager,
            "===== Name Exactly Matched: vlc =====\n"
            "vlc.x86_64 : multimedia player\n"
            "vlc-plugins.noarch : plugins\n",
            [
                PackageSearchResult("vlc", "multimedia player"),
                PackageSearchResult("vlc-plugins", "plugins"),
            ],
        ),
        (
            PacmanPackageManager,
            "extra/vlc 3.0.21-1\n"
            "    multimedia player\n"
            "core/bash 5.2-1\n"
            "    shell\n",
            [
                PackageSearchResult("vlc", "multimedia player"),
                PackageSearchResult("bash", "shell"),
            ],
        ),
        (
            ZypperPackageManager,
            "S  | Name | Summary | Type\n"
            "---+------+---------+------\n"
            "i+ | vlc | multimedia player | package\n",
            [PackageSearchResult("vlc", "multimedia player")],
        ),
        (
            EpmPackageManager,
            "vlc - multimedia player\n",
            [PackageSearchResult("vlc", "multimedia player")],
        ),
    ],
)
def test_parse_search(
    manager_class: type,
    output: str,
    expected: list[PackageSearchResult],
) -> None:
    assert make_manager(manager_class).parse_search(output) == expected


def test_search_returns_parsed_results() -> None:
    runner = FakeRunner([CommandResult(("pacman", "-Ss", "vlc"), 0, "extra/vlc 1\n    player\n")])
    manager = PacmanPackageManager(runner, which=available_which)

    assert manager.search("vlc") == [PackageSearchResult("vlc", "player")]
    assert runner.commands[0].argv == ("pacman", "-Ss", "vlc")


def test_search_failure_is_reported() -> None:
    runner = FakeRunner([CommandResult(("pacman", "-Ss", "vlc"), 1, stderr="error")])
    manager = PacmanPackageManager(runner, which=available_which)

    with pytest.raises(PackageManagerError, match="error"):
        manager.search("vlc")


def test_runner_adds_pkexec_and_env_without_shell(monkeypatch) -> None:
    calls: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        calls["argv"] = argv
        calls["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = CommandRunner(
        is_root=False,
        which=lambda name: "/usr/bin/pkexec" if name == "pkexec" else None,
    )
    command = Command(
        ("apt-get", "install", "--yes", "vlc"),
        requires_root=True,
        env=(("DEBIAN_FRONTEND", "noninteractive"),),
    )

    result = runner.run(command)

    assert result.success
    assert calls["argv"] == (
        "/usr/bin/pkexec",
        "env",
        "DEBIAN_FRONTEND=noninteractive",
        "apt-get",
        "install",
        "--yes",
        "vlc",
    )
    assert calls["kwargs"]["shell"] is False


def test_runner_does_not_use_pkexec_when_already_root() -> None:
    runner = CommandRunner(is_root=True, which=lambda _name: "/usr/bin/pkexec")
    command = Command(("apt-get", "update"), requires_root=True)

    assert runner.build_argv(command) == ("apt-get", "update")


def test_runner_requires_pkexec_for_root_command() -> None:
    runner = CommandRunner(is_root=False, which=lambda _name: None)

    with pytest.raises(RootPrivilegesError, match="pkexec"):
        runner.build_argv(Command(("apt-get", "update"), requires_root=True))


def test_runner_reports_missing_executable(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = CommandRunner(is_root=True)

    with pytest.raises(PackageManagerNotFoundError, match="Утилита не найдена"):
        runner.run(Command(("apt-get", "update")))


def test_runner_reports_os_error(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = CommandRunner(is_root=True)

    with pytest.raises(CommandExecutionError, match="boom"):
        runner.run(Command(("apt-get", "update")))


@pytest.mark.parametrize(
    ("family", "manager_class"),
    [
        (DistroFamily.DEBIAN, AptPackageManager),
        (DistroFamily.FEDORA, DnfPackageManager),
        (DistroFamily.ARCH, PacmanPackageManager),
        (DistroFamily.SUSE, ZypperPackageManager),
        (DistroFamily.ALT, AltAptPackageManager),
    ],
)
def test_factory_by_family(family: DistroFamily, manager_class: type) -> None:
    manager = create_package_manager(family, which=available_which)

    assert isinstance(manager, manager_class)


def test_factory_accepts_distro_info() -> None:
    info = DistroInfo(id="arch", family=DistroFamily.ARCH)

    assert isinstance(
        create_package_manager(info, which=available_which),
        PacmanPackageManager,
    )


def test_factory_unknown_distro() -> None:
    with pytest.raises(UnsupportedDistroError, match="не распознан"):
        create_package_manager(DistroFamily.UNKNOWN, which=available_which)


def test_factory_invalid_family_string() -> None:
    with pytest.raises(UnsupportedDistroError, match="Неизвестное семейство"):
        create_package_manager("beos", which=available_which)


def test_factory_missing_package_manager() -> None:
    with pytest.raises(PackageManagerNotFoundError, match="не найден"):
        create_package_manager(DistroFamily.ARCH, which=lambda _name: None)


def test_alt_uses_apt_by_default_even_if_epm_exists() -> None:
    manager = create_package_manager(DistroFamily.ALT, which=available_which)

    assert isinstance(manager, AltAptPackageManager)


def test_alt_prefers_epm_only_when_requested() -> None:
    manager = create_package_manager(
        DistroFamily.ALT,
        which=available_which,
        prefer_epm=True,
    )

    assert isinstance(manager, EpmPackageManager)


def test_alt_falls_back_to_epm_without_apt() -> None:
    def which(name: str) -> str | None:
        return "/usr/bin/epm" if name == "epm" else (
            "/usr/bin/rpm" if name == "rpm" else None
        )

    manager = create_package_manager(DistroFamily.ALT, which=which)

    assert isinstance(manager, EpmPackageManager)


def test_constructing_manager_does_not_run_commands() -> None:
    runner = FakeRunner([])

    manager = PacmanPackageManager(runner, which=available_which)

    assert runner.commands == []
    assert manager.is_available() is True


def test_settings_auto_ignores_manual_override() -> None:
    detected = DistroInfo(id="arch", family=DistroFamily.ARCH)

    selection = create_package_manager_for_settings(
        detected,
        "auto",
        "debian",
        which=available_which,
    )

    assert isinstance(selection.manager, PacmanPackageManager)
    assert selection.warning is None


def test_settings_manual_override_selects_another_backend_and_warns() -> None:
    detected = DistroInfo(id="arch", family=DistroFamily.ARCH)

    selection = create_package_manager_for_settings(
        detected,
        "manual",
        "debian",
        which=available_which,
    )

    assert isinstance(selection.manager, AptPackageManager)
    assert selection.warning is not None
    assert "Arch" in selection.warning


def test_settings_manual_same_family_has_no_warning() -> None:
    detected = DistroInfo(id="arch", family=DistroFamily.ARCH)

    selection = create_package_manager_for_settings(
        detected,
        "manual",
        "arch",
        which=available_which,
    )

    assert isinstance(selection.manager, PacmanPackageManager)
    assert selection.warning is None
