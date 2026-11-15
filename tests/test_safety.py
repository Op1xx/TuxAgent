"""Тесты PathPolicy: разрешённые корни, защищённые пути, симлинки и обходы."""

from __future__ import annotations

from pathlib import Path

import pytest

from safety import (
    DEFAULT_PROTECTED_PATHS,
    ConfirmationRequiredError,
    Operation,
    PathPolicy,
    PolicyDeniedError,
    RiskLevel,
)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def policy(home: Path) -> PathPolicy:
    return PathPolicy(
        allowed_roots=(home, Path("/tmp")),
        protected_paths=DEFAULT_PROTECTED_PATHS,
    )


def test_home_operations_are_safe(policy: PathPolicy, home: Path) -> None:
    assert policy.check(home / "notes.txt", Operation.READ).level is RiskLevel.SAFE
    assert policy.check(home / "new", Operation.MKDIR).level is RiskLevel.SAFE
    assert policy.check(home / "notes.txt", Operation.WRITE).level is RiskLevel.SAFE


def test_tmp_is_allowed(policy: PathPolicy) -> None:
    decision = policy.check(Path("/tmp/tuxagent-test-file"), Operation.MKDIR)

    assert decision.level is RiskLevel.SAFE


def test_delete_and_move_in_home_require_confirmation(policy: PathPolicy, home: Path) -> None:
    delete = policy.check(home / "notes.txt", Operation.DELETE)
    move = policy.check(home / "notes.txt", Operation.MOVE)

    assert delete.level is RiskLevel.CONFIRM
    assert move.level is RiskLevel.CONFIRM
    assert delete.requires_confirmation


def test_delete_outside_home_is_denied(home: Path, tmp_path: Path) -> None:
    policy = PathPolicy(
        allowed_roots=(home,),
        protected_paths=DEFAULT_PROTECTED_PATHS,
    )
    outside = tmp_path / "outside" / "file.txt"

    decision = policy.check(outside, Operation.DELETE)

    assert decision.level is RiskLevel.DENY
    assert "вне" in decision.reason


def test_delete_allowed_root_is_denied(policy: PathPolicy, home: Path) -> None:
    decision = policy.check(home, Operation.DELETE)

    assert decision.level is RiskLevel.DENY
    assert "корень" in decision.reason


def test_protected_paths_cannot_be_changed(policy: PathPolicy) -> None:
    assert policy.check("/etc/passwd", Operation.WRITE).level is RiskLevel.DENY
    assert policy.check("/etc/passwd", Operation.DELETE).level is RiskLevel.DENY
    assert policy.check("/usr/bin", Operation.MKDIR).level is RiskLevel.DENY
    assert policy.check("/boot", Operation.MOVE).level is RiskLevel.DENY
    assert policy.check("/var/lib/dpkg", Operation.DELETE).level is RiskLevel.DENY


def test_recursive_protected_operations_are_denied(policy: PathPolicy) -> None:
    assert policy.check("/", Operation.LIST, recursive=True).level is RiskLevel.DENY
    assert policy.check("/etc", Operation.LIST, recursive=True).level is RiskLevel.DENY
    assert policy.check("/etc", Operation.READ, recursive=True).level is RiskLevel.DENY


def test_non_recursive_read_of_protected_file_is_safe(policy: PathPolicy) -> None:
    decision = policy.check("/etc/os-release", Operation.READ)

    assert decision.level is RiskLevel.SAFE


def test_recursive_read_outside_allowed_roots_requires_confirmation(
    home: Path, tmp_path: Path
) -> None:
    policy = PathPolicy(
        allowed_roots=(home,),
        protected_paths=DEFAULT_PROTECTED_PATHS,
    )
    decision = policy.check(tmp_path / "outside", Operation.LIST, recursive=True)

    assert decision.level is RiskLevel.CONFIRM


def test_parent_traversal_does_not_bypass_policy(home: Path) -> None:
    policy = PathPolicy(
        allowed_roots=(home,),
        protected_paths=DEFAULT_PROTECTED_PATHS,
    )
    escaped = home / "safe" / ".." / ".." / "outside" / "file.txt"

    decision = policy.check(escaped, Operation.DELETE)

    assert decision.level is RiskLevel.DENY
    assert not policy.is_within_allowed(escaped)


def test_symlink_escape_is_denied(home: Path, tmp_path: Path) -> None:
    policy = PathPolicy(
        allowed_roots=(home,),
        protected_paths=DEFAULT_PROTECTED_PATHS,
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    link = home / "link"
    link.symlink_to(outside, target_is_directory=True)

    decision = policy.check(link, Operation.DELETE)

    assert decision.level is RiskLevel.DENY
    assert decision.path == outside.resolve()


def test_unicode_spaces_and_dashes_are_literal(policy: PathPolicy, home: Path) -> None:
    path = home / "файл с пробелами -- -rf"

    assert policy.check(path, Operation.MKDIR).level is RiskLevel.SAFE
    assert policy.check(path, Operation.DELETE).level is RiskLevel.CONFIRM


def test_wildcard_is_literal(policy: PathPolicy, home: Path) -> None:
    path = home / "*.txt"

    decision = policy.check(path, Operation.MKDIR)

    assert decision.level is RiskLevel.SAFE
    assert decision.path == path


def test_relative_paths_are_resolved(policy: PathPolicy, home: Path, monkeypatch) -> None:
    monkeypatch.chdir(home)

    decision = policy.check("note.txt", Operation.MKDIR)

    assert decision.level is RiskLevel.SAFE
    assert decision.path == home / "note.txt"


def test_is_protected_does_not_treat_root_as_umbrella(policy: PathPolicy, home: Path) -> None:
    assert policy.is_protected("/")
    assert policy.is_protected("/etc/passwd")
    assert policy.is_protected("/var/lib/dpkg")
    assert not policy.is_protected("/etcfoo")
    assert not policy.is_protected("/var/libfoo")
    assert not policy.is_protected(home)


def test_require_allowed_raises_for_deny_and_confirm(policy: PathPolicy, home: Path) -> None:
    with pytest.raises(PolicyDeniedError):
        policy.require_allowed("/etc/passwd", Operation.DELETE)

    with pytest.raises(ConfirmationRequiredError):
        policy.require_allowed(home / "file", Operation.DELETE)

    decision = policy.require_allowed(home / "file", Operation.DELETE, confirmed=True)
    assert decision.level is RiskLevel.CONFIRM
