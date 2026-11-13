"""Тесты определения дистрибутива по /etc/os-release."""

from __future__ import annotations

from pathlib import Path

import pytest

from distro import (
    DistroDetectionError,
    DistroFamily,
    DistroInfo,
    detect_distro,
    detect_family,
    parse_distro_info,
    parse_os_release,
    resolve_distro,
)

FIXTURES = Path(__file__).parent / "fixtures" / "os-release"


@pytest.mark.parametrize(
    ("fixture", "distro_id", "family", "version_id"),
    [
        ("debian", "debian", DistroFamily.DEBIAN, "12"),
        ("ubuntu", "ubuntu", DistroFamily.DEBIAN, "24.04"),
        ("astra", "astra", DistroFamily.DEBIAN, "2.12"),
        ("fedora", "fedora", DistroFamily.FEDORA, "40"),
        ("rosa", "rosa", DistroFamily.FEDORA, "12.4"),
        ("arch", "arch", DistroFamily.ARCH, ""),
        ("opensuse-leap", "opensuse-leap", DistroFamily.SUSE, "15.6"),
        ("alt", "altlinux", DistroFamily.ALT, "10.2"),
    ],
)
def test_detect_fixture(
    fixture: str,
    distro_id: str,
    family: DistroFamily,
    version_id: str,
) -> None:
    info = detect_distro(FIXTURES / fixture)

    assert info.id == distro_id
    assert info.family is family
    assert info.version_id == version_id
    assert info.pretty_name
    assert info.source == FIXTURES / fixture


def test_id_like_is_used_when_id_is_unknown() -> None:
    info = parse_distro_info(
        'PRETTY_NAME="Some Ubuntu derivative"\n'
        "ID=some-derivative\n"
        "ID_LIKE=\"ubuntu debian\"\n"
    )

    assert info.family is DistroFamily.DEBIAN
    assert info.id_like == ("ubuntu", "debian")


def test_unknown_distro_is_not_guessed() -> None:
    info = detect_distro(FIXTURES / "unknown")

    assert info.id == "beos"
    assert info.family is DistroFamily.UNKNOWN
    assert info.family_label == "Неизвестный дистрибутив"


def test_detect_family_unknown() -> None:
    assert detect_family("beos") is DistroFamily.UNKNOWN
    assert detect_family("", ("beos",)) is DistroFamily.UNKNOWN


def test_parser_handles_quotes_comments_and_empty_values() -> None:
    values = parse_os_release(
        "# комментарий\n"
        'NAME="ALT Workstation"\n'
        "ID=altlinux\n"
        'ID_LIKE="alt altlinux"\n'
        'PRETTY_NAME="ALT \\"Workstation\\" 10.2"\n'
        "VERSION_CODENAME=''\n"
        "BROKEN LINE\n"
    )

    assert values["NAME"] == "ALT Workstation"
    assert values["ID"] == "altlinux"
    assert values["ID_LIKE"] == "alt altlinux"
    assert values["PRETTY_NAME"] == 'ALT "Workstation" 10.2'
    assert values["VERSION_CODENAME"] == ""
    assert "BROKEN" not in values


def test_missing_os_release(tmp_path: Path) -> None:
    with pytest.raises(DistroDetectionError, match="Не удалось прочитать"):
        detect_distro(tmp_path / "os-release")


def test_os_release_without_id(tmp_path: Path) -> None:
    path = tmp_path / "os-release"
    path.write_text('PRETTY_NAME="No ID"\n', encoding="utf-8")

    with pytest.raises(DistroDetectionError, match="нет поля ID"):
        detect_distro(path)


def test_display_name_falls_back_to_id() -> None:
    info = DistroInfo(id="arch", family=DistroFamily.ARCH)

    assert info.display_name == "arch"
    assert DistroInfo(id="", family=DistroFamily.UNKNOWN).display_name == (
        "Неизвестный дистрибутив"
    )


def _arch_info() -> DistroInfo:
    return detect_distro(FIXTURES / "arch")


def test_resolve_auto_uses_detected_family() -> None:
    resolution = resolve_distro(_arch_info(), "auto", "debian")

    assert resolution.selected_family is DistroFamily.ARCH
    assert resolution.override is None
    assert resolution.warning is None


def test_resolve_manual_same_family_has_no_warning() -> None:
    resolution = resolve_distro(_arch_info(), "manual", "arch")

    assert resolution.selected_family is DistroFamily.ARCH
    assert resolution.override == "arch"
    assert resolution.warning is None


def test_resolve_manual_mismatch_warns() -> None:
    resolution = resolve_distro(_arch_info(), "manual", "debian")

    assert resolution.selected_family is DistroFamily.DEBIAN
    assert resolution.warning is not None
    assert "Arch Linux" in resolution.warning
    assert "Debian" in resolution.warning


def test_resolve_invalid_override_warns() -> None:
    resolution = resolve_distro(_arch_info(), "manual", "beos")

    assert resolution.selected_family is DistroFamily.UNKNOWN
    assert resolution.warning is not None
    assert "beos" in resolution.warning
