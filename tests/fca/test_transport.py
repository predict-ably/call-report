"""Tests for the injectable FCA transport interface (call_report.fca.transport)."""

from __future__ import annotations

from pathlib import Path

import pytest

from call_report.exceptions import DownloadError
from call_report.fca.transport import LocalDirectoryTransport
from call_report.periods import ReportingPeriod


def test_local_directory_transport_resolves_default_modern_dirname(
    tmp_path: Path,
) -> None:
    """The default dirname matches the modern-era FCA zip stem (e.g. '2026March')."""
    release_dir = tmp_path / "2026March"
    release_dir.mkdir()
    transport = LocalDirectoryTransport(data_dir=tmp_path)
    period = ReportingPeriod.from_period_end(value="2026-03-31")
    assert transport.resolve(period=period) == release_dir


def test_local_directory_transport_resolves_default_legacy_dirname(
    tmp_path: Path,
) -> None:
    """The default dirname matches the legacy-era FCA zip stem (e.g. 'Mar2003')."""
    release_dir = tmp_path / "Mar2003"
    release_dir.mkdir()
    transport = LocalDirectoryTransport(data_dir=tmp_path)
    period = ReportingPeriod.from_period_end(value="2003-03-31")
    assert transport.resolve(period=period) == release_dir


def test_local_directory_transport_missing_directory_raises_download_error(
    tmp_path: Path,
) -> None:
    """A period with no matching local directory raises a clear DownloadError."""
    transport = LocalDirectoryTransport(data_dir=tmp_path)
    period = ReportingPeriod.from_period_end(value="2026-03-31")
    with pytest.raises(DownloadError, match="2026March"):
        transport.resolve(period=period)


def test_local_directory_transport_custom_dirname_for(tmp_path: Path) -> None:
    """A custom dirname_for callable overrides the default naming convention."""
    release_dir = tmp_path / "custom-2026-q1"
    release_dir.mkdir()
    transport = LocalDirectoryTransport(
        data_dir=tmp_path,
        dirname_for=lambda period: f"custom-{period.year}-q{period.quarter.value}",
    )
    period = ReportingPeriod.from_period_end(value="2026-03-31")
    assert transport.resolve(period=period) == release_dir


def test_local_directory_transport_resolve_is_keyword_only(tmp_path: Path) -> None:
    """Resolve takes no positional arguments."""
    transport = LocalDirectoryTransport(data_dir=tmp_path)
    period = ReportingPeriod.from_period_end(value="2026-03-31")
    with pytest.raises(TypeError):
        transport.resolve(period)  # type: ignore[call-arg]


def test_local_directory_transport_is_keyword_only(tmp_path: Path) -> None:
    """LocalDirectoryTransport's constructor takes no positional arguments."""
    with pytest.raises(TypeError):
        LocalDirectoryTransport(tmp_path)  # type: ignore[call-arg]
