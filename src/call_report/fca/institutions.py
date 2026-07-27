"""Parse the FCA institution roster (the ``INST`` root) from a release.

The roster is handled as its own module rather than through
:mod:`call_report.fca.enums`'s `FCASchedule` since it describes
institutions themselves, not a financial schedule -- but under the hood
it's parsed the same way as any other ``"single"``-scenario schedule.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import narwhals as nw

from call_report.core._backend import finalize
from call_report.exceptions import DownloadError
from call_report.fca._discovery import scan_release
from call_report.fca.layout import parse_layout
from call_report.fca.reader import _read_schedule_frame

INSTITUTIONS_ROOT = "INST"
"""str: The root name FCA uses for the institution roster file pair."""


def read_institutions(*, release_dir: Path) -> Any:
    """Parse a release's institution roster into a native dataframe.

    Locates and parses the ``D_INST[_<YEAR>].TXT`` / ``INST...TXT`` file
    pair within `release_dir`, the same way any other schedule is parsed.

    Parameters
    ----------
    release_dir : pathlib.Path
        Path to one quarter's extracted release files.

    Returns
    -------
    Any
        A native dataframe of the configured backend, one row per
        institution.

    Raises
    ------
    DownloadError
        If `release_dir` has no matched ``INST`` layout/data file pair.
    """
    return finalize(frame=_read_institutions_frame(release_dir=release_dir))


def _read_institutions_frame(*, release_dir: Path) -> nw.DataFrame[Any]:
    """Parse a release's institution roster into an eager narwhals frame.

    The non-finalizing counterpart to `read_institutions`, used internally
    by `call_report.fca.report.FCACallReport` so multiple periods' rosters
    can be stacked before a single, final backend/laziness conversion.

    Parameters
    ----------
    release_dir : pathlib.Path
        Path to one quarter's extracted release files.

    Returns
    -------
    narwhals.DataFrame
        An eager narwhals-wrapped frame of the configured backend.

    Raises
    ------
    DownloadError
        If `release_dir` has no matched ``INST`` layout/data file pair.
    """
    manifest = scan_release(release_dir=release_dir)
    files = manifest.get(INSTITUTIONS_ROOT)
    if files is None:
        raise DownloadError(
            f"{release_dir}: no {INSTITUTIONS_ROOT} layout/data file pair found."
        )
    layout = parse_layout(path=files.layout_path)
    return _read_schedule_frame(data_path=files.data_path, layout=layout)
