"""Tests for the injectable FCA transport interface (call_report.fca.transport)."""

from __future__ import annotations

import gc
import zipfile
from pathlib import Path

import pytest

from call_report.core import ReportingPeriod
from call_report.exceptions import DownloadError
from call_report.fca.transport import LocalDirectoryTransport, PackagedArchiveTransport

REPO_DATA_ROOT = Path(__file__).resolve().parents[2] / "data" / "fca-call-report"


def _zip_release(release_dir: Path, *, archive_root: Path) -> Path:
    """Zip a hand-built release directory's files into archive_root/<name>.zip."""
    archive_path = archive_root / f"{release_dir.name}.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for file in release_dir.iterdir():
            archive.write(file, arcname=file.name)
    return archive_path


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


# ---------------------------------------------------------------------------
# PackagedArchiveTransport -- standard location (this repo's data/ directory)
# ---------------------------------------------------------------------------


def test_packaged_archive_transport_default_root_is_repo_data_directory() -> None:
    """The default archive_root points at this repo's checked-in data directory."""
    transport = PackagedArchiveTransport()
    assert transport.archive_root == REPO_DATA_ROOT


@pytest.mark.skipif(
    not REPO_DATA_ROOT.is_dir(), reason="No packaged FCA archive checked out."
)
def test_packaged_archive_transport_resolves_a_real_shipped_release() -> None:
    """resolve() extracts a real, standard-location release shipped in data/."""
    transport = PackagedArchiveTransport()
    period = ReportingPeriod.from_period_end(value="2026-03-31")
    resolved = transport.resolve(period=period)
    assert resolved.is_dir()
    assert any(resolved.iterdir())


# ---------------------------------------------------------------------------
# PackagedArchiveTransport -- custom location
# ---------------------------------------------------------------------------


def test_packaged_archive_transport_resolves_custom_archive_root(
    tmp_path: Path, release_2026q1: Path
) -> None:
    """A custom archive_root resolves and extracts a hand-built release zip."""
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    _zip_release(release_2026q1, archive_root=archive_root)

    transport = PackagedArchiveTransport(archive_root=archive_root)
    period = ReportingPeriod.from_period_end(value="2026-03-31")
    resolved = transport.resolve(period=period)

    assert resolved != release_2026q1
    assert resolved.is_dir()
    original = {p.name: p.read_bytes() for p in release_2026q1.iterdir()}
    extracted = {p.name: p.read_bytes() for p in resolved.iterdir()}
    assert extracted == original


def test_packaged_archive_transport_resolves_legacy_naming(
    tmp_path: Path, release_2003q1: Path
) -> None:
    """A legacy-era (pre-2015, no underscore) release zip resolves correctly."""
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    _zip_release(release_2003q1, archive_root=archive_root)

    transport = PackagedArchiveTransport(archive_root=archive_root)
    period = ReportingPeriod.from_period_end(value="2003-03-31")
    resolved = transport.resolve(period=period)
    assert (resolved / "D_RC.TXT").is_file()


def test_packaged_archive_transport_caches_extraction_per_period(
    tmp_path: Path, release_2026q1: Path
) -> None:
    """Resolving the same period twice reuses the first extraction."""
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    _zip_release(release_2026q1, archive_root=archive_root)

    transport = PackagedArchiveTransport(archive_root=archive_root)
    period = ReportingPeriod.from_period_end(value="2026-03-31")
    first = transport.resolve(period=period)
    second = transport.resolve(period=period)
    assert first == second


def test_packaged_archive_transport_reuses_extract_root_across_periods(
    tmp_path: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """Resolving a second, different period reuses the same extraction tempdir."""
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    _zip_release(release_2025q3, archive_root=archive_root)
    _zip_release(release_2025q4, archive_root=archive_root)

    transport = PackagedArchiveTransport(archive_root=archive_root)
    q3 = ReportingPeriod.from_period_end(value="2025-09-30")
    q4 = ReportingPeriod.from_period_end(value="2025-12-31")
    first = transport.resolve(period=q3)
    second = transport.resolve(period=q4)

    assert first.parent == second.parent
    assert first != second


def test_packaged_archive_transport_missing_archive_raises_download_error(
    tmp_path: Path,
) -> None:
    """A period with no matching zip in archive_root raises a clear DownloadError."""
    transport = PackagedArchiveTransport(archive_root=tmp_path)
    period = ReportingPeriod.from_period_end(value="2026-03-31")
    with pytest.raises(DownloadError, match="2026March"):
        transport.resolve(period=period)


def test_packaged_archive_transport_custom_dirname_for(tmp_path: Path) -> None:
    """A custom dirname_for callable overrides the default zip-naming convention."""
    with zipfile.ZipFile(tmp_path / "custom-2026-q1.zip", "w") as archive:
        archive.writestr("D_RC.TXT", "placeholder layout")

    transport = PackagedArchiveTransport(
        archive_root=tmp_path,
        dirname_for=lambda period: f"custom-{period.year}-q{period.quarter.value}",
    )
    period = ReportingPeriod.from_period_end(value="2026-03-31")
    resolved = transport.resolve(period=period)
    assert (resolved / "D_RC.TXT").is_file()


def test_packaged_archive_transport_resolve_is_keyword_only(tmp_path: Path) -> None:
    """Resolve takes no positional arguments."""
    transport = PackagedArchiveTransport(archive_root=tmp_path)
    period = ReportingPeriod.from_period_end(value="2026-03-31")
    with pytest.raises(TypeError):
        transport.resolve(period)  # type: ignore[call-arg]


def test_packaged_archive_transport_is_keyword_only(tmp_path: Path) -> None:
    """PackagedArchiveTransport's constructor takes no positional arguments."""
    with pytest.raises(TypeError):
        PackagedArchiveTransport(tmp_path)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# PackagedArchiveTransport -- deterministic cleanup
# ---------------------------------------------------------------------------


def test_close_removes_the_extraction_directory(
    tmp_path: Path, release_2026q1: Path
) -> None:
    """close() deletes the temporary directory resolve() extracted into."""
    archive_root = tmp_path / "archives"
    archive_root.mkdir()
    _zip_release(release_2026q1, archive_root=archive_root)

    transport = PackagedArchiveTransport(archive_root=archive_root)
    resolved = transport.resolve(
        period=ReportingPeriod.from_period_end(value="2026-03-31")
    )
    assert resolved.is_dir()

    transport.close()
    assert not resolved.exists()


def test_close_is_idempotent(tmp_path: Path, release_2026q1: Path) -> None:
    """Calling close() a second time is a no-op rather than an error."""
    archive_root = tmp_path / "archives"
    archive_root.mkdir()
    _zip_release(release_2026q1, archive_root=archive_root)

    transport = PackagedArchiveTransport(archive_root=archive_root)
    transport.resolve(period=ReportingPeriod.from_period_end(value="2026-03-31"))
    transport.close()
    transport.close()


def test_close_on_an_unused_transport_is_a_noop(tmp_path: Path) -> None:
    """A transport that never resolved anything has nothing to clean up."""
    PackagedArchiveTransport(archive_root=tmp_path).close()


def test_resolve_after_close_extracts_into_a_fresh_directory(
    tmp_path: Path, release_2026q1: Path
) -> None:
    """A closed transport stays usable and re-extracts on the next resolve()."""
    archive_root = tmp_path / "archives"
    archive_root.mkdir()
    _zip_release(release_2026q1, archive_root=archive_root)

    period = ReportingPeriod.from_period_end(value="2026-03-31")
    transport = PackagedArchiveTransport(archive_root=archive_root)
    first = transport.resolve(period=period)
    transport.close()

    second = transport.resolve(period=period)
    assert second.is_dir()
    assert second != first
    transport.close()


def test_context_manager_closes_on_normal_exit(
    tmp_path: Path, release_2026q1: Path
) -> None:
    """Leaving a `with` block removes the extraction directory."""
    archive_root = tmp_path / "archives"
    archive_root.mkdir()
    _zip_release(release_2026q1, archive_root=archive_root)

    with PackagedArchiveTransport(archive_root=archive_root) as transport:
        resolved = transport.resolve(
            period=ReportingPeriod.from_period_end(value="2026-03-31")
        )
        assert resolved.is_dir()
    assert not resolved.exists()


def test_context_manager_closes_when_the_block_raises(
    tmp_path: Path, release_2026q1: Path
) -> None:
    """Cleanup still runs, and the exception still propagates, on an error."""
    archive_root = tmp_path / "archives"
    archive_root.mkdir()
    _zip_release(release_2026q1, archive_root=archive_root)

    class _BoomError(Exception):
        pass

    resolved: Path | None = None
    with (
        pytest.raises(_BoomError),
        PackagedArchiveTransport(archive_root=archive_root) as transport,
    ):
        resolved = transport.resolve(
            period=ReportingPeriod.from_period_end(value="2026-03-31")
        )
        raise _BoomError

    # `pytest.raises.__exit__` returns True, so it swallows _BoomError and
    # execution continues here. Flow analysis that does not model that
    # reports these two lines as unreachable and `resolved` as unused. They
    # are not: forcing either assertion to a false value fails this test.
    assert resolved is not None
    assert not resolved.exists()


def test_context_manager_yields_the_transport_itself(tmp_path: Path) -> None:
    """`with PackagedArchiveTransport() as t` binds the transport, not None."""
    transport = PackagedArchiveTransport(archive_root=tmp_path)
    with transport as entered:
        assert entered is transport


def test_garbage_collection_cleans_up_without_a_resourcewarning(
    tmp_path: Path, release_2026q1: Path, recwarn: pytest.WarningsRecorder
) -> None:
    """An unclosed transport cleans up on collection, and stays silent doing it.

    The GC path is a documented fallback behind close(), not a leak, so it
    must not emit the ResourceWarning `tempfile.TemporaryDirectory` would.
    That warning previously surfaced in whichever unrelated test happened
    to trigger the collection.
    """
    archive_root = tmp_path / "archives"
    archive_root.mkdir()
    _zip_release(release_2026q1, archive_root=archive_root)

    transport = PackagedArchiveTransport(archive_root=archive_root)
    resolved = transport.resolve(
        period=ReportingPeriod.from_period_end(value="2026-03-31")
    )
    assert resolved.is_dir()

    del transport
    gc.collect()

    assert not resolved.exists()
    assert [w for w in recwarn if issubclass(w.category, ResourceWarning)] == []
