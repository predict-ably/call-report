"""The injectable FCA transport interface and its local implementations.

`FCATransport` is the seam a future network-based transport, handling
FCA's Cloudflare-protected downloads, will slot in behind without changing
any public signature on `call_report.fca.report.FCACallReport`. Every
`FCACallReport` requires a transport explicitly, with no implicit default,
so it is always clear which files a given instance will read.
"""

from __future__ import annotations

import shutil
import tempfile
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Self

from call_report.core import PeriodRange, ReportingPeriod
from call_report.core._archive import extract_zip
from call_report.exceptions import DownloadError
from call_report.fca.catalog import (
    _FIRST_RELEASE_PERIOD,
    _current_period,
    fca_release_stem,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PACKAGED_ARCHIVE_ROOT = _REPO_ROOT / "data" / "fca-call-report"


class FCATransport(Protocol):
    """The interface FCACallReport uses to resolve a period's local files.

    Any object implementing `resolve` and `available_periods` can be passed
    as an `FCACallReport`'s ``transport=`` argument. Two implementations ship
    with the package: `LocalDirectoryTransport`, for a user-supplied
    directory of already-extracted releases, and `PackagedArchiveTransport`,
    for the historical release zips checked into this repository.
    """  # numpydoc ignore=PR01

    def resolve(self, *, period: ReportingPeriod) -> Path:
        """Return a local directory containing a period's extracted files.

        Implementations may resolve this however they like, for instance
        by reading an already-extracted local directory or by downloading
        and extracting a zip archive on demand.

        Parameters
        ----------
        period : ReportingPeriod
            The period to resolve.

        Returns
        -------
        pathlib.Path
            A directory containing that period's ``D_*.TXT`` layout files
            and their data files.

        Raises
        ------
        DownloadError
            If the period's files cannot be resolved.
        ZipArchiveUnpackingError
            If the period's zip archive cannot be unpacked.
        """
        ...  # pragma: no cover

    def available_periods(self) -> tuple[ReportingPeriod, ...]:
        """Return every period this transport can supply, oldest first.

        `FCACallReport.fetch` refuses a range that starts before the first
        of these periods or ends after the last. A period between them that
        `resolve` cannot supply is recorded in `FCACallReport.errors_`.

        Returns
        -------
        tuple[ReportingPeriod, ...]
            The periods this transport can supply, oldest first. Empty if
            it can supply none.
        """
        ...  # pragma: no cover


def _default_dirname(period: ReportingPeriod) -> str:
    """Return the default per-period directory name: the FCA zip's stem.

    Matches the name produced by downloading and unzipping FCA's own
    archive for that period.

    Parameters
    ----------
    period : ReportingPeriod
        The period to name a directory for.

    Returns
    -------
    str
        The FCA download URL's filename without its ``.zip`` extension,
        e.g. ``"2026March"`` or ``"Mar2003"``.
    """
    return fca_release_stem(period=period)


def _periods_present(
    exists: Callable[[ReportingPeriod], bool],
) -> tuple[ReportingPeriod, ...]:
    """Return every period from FCA's first release to today that `exists` finds.

    Every quarter from 2000Q1 through the quarter containing today is
    checked, so the result is sorted and may have gaps.

    Parameters
    ----------
    exists : Callable[[ReportingPeriod], bool]
        Says whether a transport has a period's files.

    Returns
    -------
    tuple[ReportingPeriod, ...]
        The periods `exists` finds, oldest first.
    """
    # Each quarter is checked in turn rather than working back from the names
    # found on disk, so a transport's dirname_for can be any function.
    candidates = PeriodRange(start=_FIRST_RELEASE_PERIOD, end=_current_period())
    return tuple(period for period in candidates if exists(period))


@dataclass(kw_only=True)
class LocalDirectoryTransport:
    """A transport that resolves periods to pre-extracted local directories.

    Expects `data_dir` to contain one subdirectory per period, each
    holding that period's already-downloaded and unzipped FCA release
    files.

    Attributes
    ----------
    data_dir : pathlib.Path
        The parent directory containing one subdirectory per period.
    dirname_for : Callable[[ReportingPeriod], str]
        A function mapping a period to its subdirectory name under
        `data_dir`. Defaults to the FCA download zip's filename stem (e.g.
        ``"2026March"``), which is the name produced by unzipping that
        archive.

    Examples
    --------
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     data_dir = Path(tmp)
    ...     (data_dir / "2026March").mkdir()
    ...     transport = LocalDirectoryTransport(data_dir=data_dir)
    ...     resolved = transport.resolve(
    ...         period=ReportingPeriod.from_period_end(value="2026-03-31")
    ...     )
    ...     resolved.name
    '2026March'
    """  # numpydoc ignore=PR01

    data_dir: Path
    dirname_for: Callable[[ReportingPeriod], str] = field(default=_default_dirname)

    def resolve(self, *, period: ReportingPeriod) -> Path:
        """Return `period`'s subdirectory under `data_dir`.

        Implements the `FCATransport` protocol for pre-extracted local data.

        Parameters
        ----------
        period : ReportingPeriod
            The period to resolve.

        Returns
        -------
        pathlib.Path
            The resolved, existing directory.

        Raises
        ------
        DownloadError
            If no directory exists at the expected location.
        """
        directory = self._directory_for(period)
        if not directory.is_dir():
            raise DownloadError(
                f"No local FCA data found for {period.label}; expected a directory "
                f"at {directory}. Pass data_dir=... pointing at a parent directory "
                f"containing one extracted release folder per period (e.g. "
                f"{directory.name!r}), or supply transport=... for a different "
                f"retrieval strategy."
            )
        return directory

    def available_periods(self) -> tuple[ReportingPeriod, ...]:
        """Return every period with a release folder in `data_dir`, oldest first.

        Each quarter from FCA's first release, 2000Q1, through the quarter
        containing today is checked for a folder named by `dirname_for`.

        Returns
        -------
        tuple[ReportingPeriod, ...]
            The periods `resolve` can supply. Empty if `data_dir` holds no
            release folders.

        Examples
        --------
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     data_dir = Path(tmp)
        ...     for name in ("Dec2014", "2015March", "2015June"):
        ...         (data_dir / name).mkdir()
        ...     transport = LocalDirectoryTransport(data_dir=data_dir)
        ...     [period.label for period in transport.available_periods()]
        ['2014Q4', '2015Q1', '2015Q2']
        """
        return _periods_present(lambda period: self._directory_for(period).is_dir())

    def _directory_for(self, period: ReportingPeriod) -> Path:
        """Return where `period`'s release folder is expected.

        The folder may not exist. `resolve` and `available_periods` both check
        this one location, so they always agree.

        Parameters
        ----------
        period : ReportingPeriod
            The period to look for.

        Returns
        -------
        pathlib.Path
            `data_dir` joined with the name `dirname_for` gives `period`.
        """
        return Path(self.data_dir) / self.dirname_for(period)


@dataclass(kw_only=True)
class PackagedArchiveTransport:
    """A transport that resolves periods to the release zips shipped in ``data/``.

    By default, resolves against this repository's own
    ``data/fca-call-report/`` archive, which holds one zip per period named
    after FCA's own download filename (e.g. ``"2026March.zip"``). A
    checkout of this repository therefore has ready-to-use historical data
    with no network access required. Each zip is extracted, once per
    period, into a private temporary directory.

    That extraction directory is a real resource, so prefer using the
    transport as a context manager, which removes it on exit::

        with PackagedArchiveTransport() as transport:
            report = FCACallReport(start=..., end=..., transport=transport)

    `close` does the same thing for callers that cannot use a ``with``
    block, and is safe to call more than once. A transport that is never
    closed still cleans up when it is garbage collected, but only at
    whatever moment the interpreter happens to collect it, which emits a
    `ResourceWarning` under ``-W error``.

    That archive is not included in the distributed wheel (see
    ``pyproject.toml``'s ``[tool.hatch.build.targets.wheel]``), so this
    transport is only useful when running from a source checkout. From a
    `pip`-installed package, either pass `archive_root` pointing at your
    own directory of FCA release zips, or use `LocalDirectoryTransport`
    with already-extracted releases instead.

    Attributes
    ----------
    archive_root : pathlib.Path
        The directory containing one ``<dirname>.zip`` per period. Defaults
        to this repository's checked-in ``data/fca-call-report/`` archive.
    dirname_for : Callable[[ReportingPeriod], str]
        A function mapping a period to its zip's filename stem under
        `archive_root` (e.g. ``"2026March"``). Defaults to the FCA download
        zip's filename stem, matching FCA's own naming convention.

    Examples
    --------
    >>> with PackagedArchiveTransport() as transport:
    ...     resolved = transport.resolve(
    ...         period=ReportingPeriod.from_period_end(value="2026-03-31")
    ...     )
    ...     resolved.name
    '2026March'
    """  # numpydoc ignore=PR01

    archive_root: Path = field(default_factory=lambda: _PACKAGED_ARCHIVE_ROOT)
    dirname_for: Callable[[ReportingPeriod], str] = field(default=_default_dirname)
    _extracted: dict[ReportingPeriod, Path] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    _extract_root: Path | None = field(
        default=None, init=False, repr=False, compare=False
    )
    # The extraction directory is managed with mkdtemp plus an explicit
    # weakref.finalize rather than tempfile.TemporaryDirectory. Both clean
    # up on garbage collection, but TemporaryDirectory's finalizer also
    # emits a ResourceWarning, which for this class fires in whichever
    # unrelated code happens to trigger the collection. Cleanup here is a
    # documented fallback behind `close`, not a leak worth warning about.
    _finalizer: weakref.finalize | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def resolve(self, *, period: ReportingPeriod) -> Path:
        """Return `period`'s extracted files, extracting its zip on first use.

        Implements the `FCATransport` protocol for the packaged archive.

        Parameters
        ----------
        period : ReportingPeriod
            The period to resolve.

        Returns
        -------
        pathlib.Path
            The directory `period`'s zip was extracted into.

        Raises
        ------
        DownloadError
            If no zip exists at the expected location under `archive_root`.
        ZipArchiveUnpackingError
            If the zip is not a valid archive, or has a corrupt member or a
            member whose path would land outside its folder.
        """
        cached = self._extracted.get(period)
        if cached is not None:
            return cached

        archive_path = self._archive_for(period)
        if not archive_path.is_file():
            raise DownloadError(
                f"No packaged FCA archive found for {period.label}; expected a zip "
                f"at {archive_path}. Pass archive_root=... pointing at a directory "
                f"of FCA release zips (e.g. {archive_path.name!r}), or supply "
                f"transport=... for a different retrieval strategy."
            )

        if self._extract_root is None:
            root = Path(tempfile.mkdtemp(prefix="call_report_fca_"))
            self._extract_root = root
            self._finalizer = weakref.finalize(
                self, shutil.rmtree, root, ignore_errors=True
            )
        target_dir = extract_zip(
            archive=archive_path, target_dir=self._extract_root / archive_path.stem
        )
        self._extracted[period] = target_dir
        return target_dir

    def available_periods(self) -> tuple[ReportingPeriod, ...]:
        """Return every period with a release zip in `archive_root`, oldest first.

        Each quarter from FCA's first release, 2000Q1, through the quarter
        containing today is checked for a zip named by `dirname_for`. No zip
        is opened.

        Returns
        -------
        tuple[ReportingPeriod, ...]
            The periods `resolve` can supply. Empty if `archive_root` holds
            no release zips.

        Examples
        --------
        >>> with PackagedArchiveTransport() as transport:
        ...     periods = transport.available_periods()
        >>> periods[0].label
        '2000Q1'
        >>> periods == tuple(PeriodRange(start=periods[0], end=periods[-1]))
        True
        """
        return _periods_present(lambda period: self._archive_for(period).is_file())

    def _archive_for(self, period: ReportingPeriod) -> Path:
        """Return where `period`'s release zip is expected.

        The zip may not exist. `resolve` and `available_periods` both check
        this one location, so they always agree.

        Parameters
        ----------
        period : ReportingPeriod
            The period to look for.

        Returns
        -------
        pathlib.Path
            `archive_root` joined with the zip name `dirname_for` gives
            `period`.
        """
        return Path(self.archive_root) / f"{self.dirname_for(period)}.zip"

    def close(self) -> None:
        """Remove the temporary directory this transport extracted zips into.

        Safe to call more than once, and safe to call on a transport that
        never resolved anything, since neither creates an extraction
        directory to remove. After `close`, a further `resolve` call
        extracts into a fresh directory rather than failing, so a closed
        transport stays usable.

        Examples
        --------
        >>> transport = PackagedArchiveTransport()
        >>> resolved = transport.resolve(
        ...     period=ReportingPeriod.from_period_end(value="2026-03-31")
        ... )
        >>> resolved.is_dir()
        True
        >>> transport.close()
        >>> resolved.is_dir()
        False
        """
        if self._finalizer is not None:
            # A weakref.finalize object runs at most once, so calling close
            # twice removes the directory once and is otherwise a no-op.
            self._finalizer()
            self._finalizer = None
        self._extract_root = None
        self._extracted.clear()

    def __enter__(self) -> Self:
        """Return this transport for use in a ``with`` block.

        Entering does no work of its own. The extraction directory is
        created lazily by the first `resolve` call, and removed by the
        matching `__exit__`.

        Returns
        -------
        Self
            This instance, so ``with PackagedArchiveTransport() as t`` binds
            the transport itself.
        """
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the transport when leaving a ``with`` block.

        Cleanup runs whether the block completed normally or raised. The
        exception, if any, is left to propagate.

        Parameters
        ----------
        *exc_info : object
            The exception type, value, and traceback, ignored here.
        """
        self.close()
