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
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Self

from call_report.core import ReportingPeriod
from call_report.exceptions import DownloadError
from call_report.fca.catalog import construct_fca_download_url

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PACKAGED_ARCHIVE_ROOT = _REPO_ROOT / "data" / "fca-call-report"


class FCATransport(Protocol):
    """The interface FCACallReport uses to resolve a period's local files.

    Any object implementing `resolve` can be passed as an
    `FCACallReport`'s ``transport=`` argument. Two implementations ship
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
    url = construct_fca_download_url(period=period)
    return url.rsplit("/", 1)[-1].removesuffix(".zip")


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
        directory = Path(self.data_dir) / self.dirname_for(period)
        if not directory.is_dir():
            raise DownloadError(
                f"No local FCA data found for {period.label}; expected a directory "
                f"at {directory}. Pass data_dir=... pointing at a parent directory "
                f"containing one extracted release folder per period (e.g. "
                f"{directory.name!r}), or supply transport=... for a different "
                f"retrieval strategy."
            )
        return directory


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
        """
        cached = self._extracted.get(period)
        if cached is not None:
            return cached

        stem = self.dirname_for(period)
        archive_path = Path(self.archive_root) / f"{stem}.zip"
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
        target_dir = self._extract_root / stem
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(target_dir)

        self._extracted[period] = target_dir
        return target_dir

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
