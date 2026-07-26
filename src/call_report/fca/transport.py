"""The injectable FCA transport interface and its local-directory implementation.

`FCATransport` is the seam a future network-based transport (handling
FCA's Cloudflare-protected downloads) will slot in behind, without changing
any public signature on `call_report.fca.report.FCACallReport`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from call_report.exceptions import DownloadError
from call_report.fca.catalog import construct_fca_download_url
from call_report.types import ReportingPeriod


class FCATransport(Protocol):
    """The interface FCACallReport uses to resolve a period's local files.

    Any object implementing `resolve` can be passed as an
    `FCACallReport`'s ``transport=`` argument; `LocalDirectoryTransport` is
    the only implementation shipped so far.
    """

    def resolve(self, *, period: ReportingPeriod) -> Path:
        """Return a local directory containing a period's extracted files.

        Implementations may resolve this however they like -- reading from
        an already-extracted local directory, downloading and extracting a
        zip archive on demand, etc.

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

    Matches how most users will have named the directory after manually
    downloading and unzipping FCA's own archive for that period.

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

    Expects `data_dir` to contain one subdirectory per period, each holding
    that period's already-downloaded-and-unzipped FCA release files. This
    is the supported transport while network downloading is not yet
    implemented; a future release can add a network-based transport
    implementing the same `FCATransport` interface without changing
    `FCACallReport`'s public signature.

    Attributes
    ----------
    data_dir : pathlib.Path
        The parent directory containing one subdirectory per period.
    dirname_for : Callable[[ReportingPeriod], str]
        A function mapping a period to its subdirectory name under
        `data_dir`. Defaults to the FCA download zip's filename stem (e.g.
        ``"2026March"``), matching how most users will have named the
        directory after manually unzipping it.

    Examples
    --------
    >>> transport = LocalDirectoryTransport(data_dir=Path("fca_data"))
    >>> transport.resolve(
    ...     period=ReportingPeriod.from_period_end(value="2026-03-31")
    ... )  # doctest: +SKIP
    PosixPath('fca_data/2026March')
    """

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
