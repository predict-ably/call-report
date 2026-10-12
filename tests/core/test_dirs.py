"""Tests for the default download folder in ``call_report.core._dirs``."""

from __future__ import annotations

from pathlib import Path

import pytest

from call_report.core import default_download_dir


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the home folder at a temp folder and clear the variables read."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    for name in ("CALL_REPORT_DOWNLOAD_DIR", "LOCALAPPDATA", "XDG_DATA_HOME"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_the_environment_variable_wins(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CALL_REPORT_DOWNLOAD_DIR is used whatever the platform."""
    monkeypatch.setenv("CALL_REPORT_DOWNLOAD_DIR", str(home / "mine"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "xdg"))
    assert default_download_dir() == home / "mine"


def test_the_environment_variable_expands_the_home_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``~`` in CALL_REPORT_DOWNLOAD_DIR is expanded."""
    monkeypatch.setenv("CALL_REPORT_DOWNLOAD_DIR", "~/fca-data")
    assert default_download_dir() == Path("~/fca-data").expanduser()


def test_an_empty_environment_variable_is_ignored(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty CALL_REPORT_DOWNLOAD_DIR falls back to the platform folder."""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("CALL_REPORT_DOWNLOAD_DIR", "")
    assert default_download_dir() == home / ".local" / "share" / "call-report"


def test_windows_uses_local_app_data(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r"""Windows keeps downloads in %LOCALAPPDATA%\call-report."""
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(home / "Local"))
    assert default_download_dir() == home / "Local" / "call-report"


def test_windows_without_local_app_data(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r"""Without LOCALAPPDATA, Windows falls back to AppData\Local under home."""
    monkeypatch.setattr("sys.platform", "win32")
    assert default_download_dir() == home / "AppData" / "Local" / "call-report"


def test_macos_uses_application_support(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On macOS, downloads go in ~/Library/Application Support/call-report."""
    monkeypatch.setattr("sys.platform", "darwin")
    expected = home / "Library" / "Application Support" / "call-report"
    assert default_download_dir() == expected


def test_other_platforms_honor_xdg_data_home(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Elsewhere, an absolute XDG_DATA_HOME is used."""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "xdg"))
    assert default_download_dir() == home / "xdg" / "call-report"


def test_other_platforms_ignore_a_relative_xdg_data_home(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative XDG_DATA_HOME is invalid, so ~/.local/share is used."""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", "relative/path")
    assert default_download_dir() == home / ".local" / "share" / "call-report"


def test_other_platforms_default_to_local_share(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without XDG_DATA_HOME, ~/.local/share is used."""
    monkeypatch.setattr("sys.platform", "linux")
    assert default_download_dir() == home / ".local" / "share" / "call-report"


def test_the_folder_is_not_created(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Asking for the folder does not create it."""
    monkeypatch.setattr("sys.platform", "linux")
    assert not default_download_dir().exists()
