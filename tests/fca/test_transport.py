"""Tests for the injectable FCA transport interface (call_report.fca.transport)."""

from __future__ import annotations

import gc
import zipfile
from datetime import date
from pathlib import Path

import pytest

from call_report.core import PeriodRange, ReportingPeriod
from call_report.exceptions import DownloadError, ZipArchiveUnpackingError
from call_report.fca import catalog
from call_report.fca.catalog import parse_fca_release_filename
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


# ---------------------------------------------------------------------------
# available_periods
# ---------------------------------------------------------------------------


def _labels(periods: tuple[ReportingPeriod, ...]) -> list[str]:
    """Return each period's label, for readable assertions."""
    return [period.label for period in periods]


def test_local_directory_available_periods_lists_the_folders_present(
    tmp_path: Path,
) -> None:
    """Each quarter with a release folder is listed, oldest first, gaps and all."""
    for name in ("2026March", "Dec2014", "Mar2000", "2015March"):
        (tmp_path / name).mkdir()
    (tmp_path / "notes.txt").write_text("not a release")
    (tmp_path / "2026June.zip").write_bytes(b"a zip, not a folder")
    transport = LocalDirectoryTransport(data_dir=tmp_path)
    assert _labels(transport.available_periods()) == [
        "2000Q1",
        "2014Q4",
        "2015Q1",
        "2026Q1",
    ]


def test_local_directory_available_periods_uses_dirname_for(tmp_path: Path) -> None:
    """A custom dirname_for decides which folder names count as releases."""
    (tmp_path / "q-2025-4").mkdir()
    (tmp_path / "2025December").mkdir()
    transport = LocalDirectoryTransport(
        data_dir=tmp_path,
        dirname_for=lambda period: f"q-{period.year}-{period.quarter.value}",
    )
    assert _labels(transport.available_periods()) == ["2025Q4"]


def test_local_directory_available_periods_of_an_empty_folder(tmp_path: Path) -> None:
    """An empty folder, or one that does not exist, has no periods."""
    assert LocalDirectoryTransport(data_dir=tmp_path).available_periods() == ()
    missing = LocalDirectoryTransport(data_dir=tmp_path / "missing")
    assert missing.available_periods() == ()


def test_local_directory_available_periods_reaches_the_current_quarter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder for a quarter newer than any shipped data is still found."""
    monkeypatch.setattr(catalog, "_today", lambda: date(2027, 2, 1))
    (tmp_path / "2027March").mkdir()
    (tmp_path / "2027June").mkdir()
    transport = LocalDirectoryTransport(data_dir=tmp_path)
    assert _labels(transport.available_periods()) == ["2027Q1"]


def test_packaged_archive_available_periods_lists_the_zips_present(
    tmp_path: Path,
) -> None:
    """Each quarter with a release zip is listed, and no zip is opened."""
    for name in ("Sept2014.zip", "2015March.zip"):
        (tmp_path / name).write_bytes(b"not opened")
    (tmp_path / "2015June").mkdir()
    with PackagedArchiveTransport(archive_root=tmp_path) as transport:
        assert _labels(transport.available_periods()) == ["2014Q3", "2015Q1"]
        assert transport._extract_root is None


def test_packaged_archive_available_periods_of_an_empty_folder(tmp_path: Path) -> None:
    """An archive folder with no release zips has no periods."""
    with PackagedArchiveTransport(archive_root=tmp_path) as transport:
        assert transport.available_periods() == ()


@pytest.mark.skipif(
    not REPO_DATA_ROOT.is_dir(), reason="No packaged FCA archive checked out."
)
def test_packaged_archive_available_periods_spans_the_real_archive() -> None:
    """The checked-in archive runs from 2000Q1 through its newest zip, with no gaps."""
    with PackagedArchiveTransport() as transport:
        periods = transport.available_periods()
    newest = max(
        period
        for path in REPO_DATA_ROOT.glob("*.zip")
        if (period := parse_fca_release_filename(path.name)) is not None
    )
    assert periods == tuple(PeriodRange(start="2000-03-31", end=newest.period_end))


# ---------------------------------------------------------------------------
# A corrupt packaged zip
# ---------------------------------------------------------------------------


def test_packaged_archive_transport_corrupt_zip_raises_unpacking_error(
    tmp_path: Path,
) -> None:
    """A zip that is not a valid archive raises ZipArchiveUnpackingError."""
    (tmp_path / "2026March.zip").write_bytes(b"<html>not a zip</html>")
    period = ReportingPeriod.from_period_end(value="2026-03-31")
    with (
        PackagedArchiveTransport(archive_root=tmp_path) as transport,
        pytest.raises(ZipArchiveUnpackingError, match="not a zip file"),
    ):
        transport.resolve(period=period)


def test_packaged_archive_transport_refuses_a_member_outside_its_folder(
    tmp_path: Path,
) -> None:
    """A zip whose member would land outside the extraction folder is refused."""
    with zipfile.ZipFile(tmp_path / "2026March.zip", "w") as archive:
        archive.writestr("../escape.txt", "escaped")
    period = ReportingPeriod.from_period_end(value="2026-03-31")
    with (
        PackagedArchiveTransport(archive_root=tmp_path) as transport,
        pytest.raises(ZipArchiveUnpackingError, match="outside its folder"),
    ):
        transport.resolve(period=period)


def test_packaged_archive_transport_two_periods_with_one_name_raise_download_error(
    tmp_path: Path,
) -> None:
    """A dirname_for that names two periods alike fails as DownloadError.

    The second period would extract into the first one's folder. Without
    the conversion, the FileExistsError from extract_zip would escape
    FCACallReport.fetch, which records only DownloadError and
    ZipArchiveUnpackingError.
    """
    with zipfile.ZipFile(tmp_path / "same.zip", "w") as archive:
        archive.writestr("D_RC.TXT", "placeholder layout")
    first = ReportingPeriod.from_period_end(value="2026-03-31")
    with PackagedArchiveTransport(
        archive_root=tmp_path, dirname_for=lambda period: "same"
    ) as transport:
        transport.resolve(period=first)
        with pytest.raises(DownloadError, match="another period already uses"):
            transport.resolve(period=first.next())
