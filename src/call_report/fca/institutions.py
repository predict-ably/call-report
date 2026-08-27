"""Parse the FCA institution roster (the ``INST`` root) from a release.

The roster is handled as its own module rather than through
:class:`~call_report.fca.FCASchedule`, since it describes institutions
themselves rather than a financial schedule. It is parsed the same way as
any other ``"single"``-scenario schedule.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, overload

import narwhals as nw

from call_report.core._backend import DataFrameType, finalize_as
from call_report.exceptions import DownloadError
from call_report.fca._discovery import scan_release
from call_report.fca.layout import parse_layout
from call_report.fca.reader import _read_schedule_frame

if TYPE_CHECKING:
    import pandas
    import polars
    import pyarrow

    from call_report.core._backend import NativeDataFrame

INSTITUTIONS_ROOT = "INST"
"""str: The root name FCA uses for the institution roster file pair."""


@overload
def read_institutions(
    *, release_dir: Path, dataframe_type: None = None
) -> NativeDataFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def read_institutions(
    *, release_dir: Path, dataframe_type: Literal["pandas"]
) -> pandas.DataFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def read_institutions(
    *, release_dir: Path, dataframe_type: Literal["pyarrow_table"]
) -> pyarrow.Table:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def read_institutions(
    *, release_dir: Path, dataframe_type: Literal["polars_dataframe"]
) -> polars.DataFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def read_institutions(
    *, release_dir: Path, dataframe_type: Literal["polars_lazyframe"]
) -> polars.LazyFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
def read_institutions(
    *, release_dir: Path, dataframe_type: DataFrameType | None = None
) -> NativeDataFrame:
    """Parse a release's institution roster into a native dataframe.

    Locates and parses the ``D_INST[_<YEAR>].TXT`` / ``INST...TXT`` file
    pair within `release_dir`, the same way any other schedule is parsed.

    Parameters
    ----------
    release_dir : pathlib.Path
        Path to one quarter's extracted release files.
    dataframe_type : {"pandas", "pyarrow_table", "polars_lazyframe", \
"polars_dataframe"}, optional
        The dataframe type to convert the result to as a final step.
        Leave this ``None`` (the default) to get back whatever backend
        `call_report.config.get_config` currently has configured. Set it
        when the code that consumes this result needs a specific type, for
        example a pandas DataFrame while the package is configured to use
        polars.

    Returns
    -------
    NativeDataFrame
        A native dataframe of the configured backend (or of
        `dataframe_type`, if supplied), one row per institution.

    Raises
    ------
    DownloadError
        If `release_dir` has no matched ``INST`` layout/data file pair.
    """
    return finalize_as(
        frame=_read_institutions_frame(release_dir=release_dir),
        dataframe_type=dataframe_type,
    )


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
