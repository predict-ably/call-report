"""Private narwhals-backed helpers for building and stacking dataframes.

Every reader in this package is written against these three primitives so
none of them need to know anything about the specific dataframe library
configured via :mod:`call_report.config`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

import narwhals as nw

from call_report.config import get_config
from call_report.exceptions import LayoutParseError

SchemaPolicy = Literal["union", "intersection", "strict"]
DataFrameType = Literal[
    "pandas", "pyarrow_table", "polars_lazyframe", "polars_dataframe"
]

_SUPPORTED_DATAFRAME_TYPES: frozenset[str] = frozenset(
    {"pandas", "pyarrow_table", "polars_lazyframe", "polars_dataframe"}
)


def build_frame(*, data: dict[str, list[Any]]) -> nw.DataFrame[Any]:
    """Build an eager narwhals DataFrame from columnar data.

    Uses the dataframe library named by the current
    :func:`~call_report.config.get_config` (never lazy, regardless of the
    ``"lazy"`` setting) -- laziness is applied once, at the public return
    boundary, by :func:`finalize`.

    Parameters
    ----------
    data : dict[str, list[Any]]
        Column name to column values, as produced by a parser.

    Returns
    -------
    narwhals.DataFrame
        An eager narwhals-wrapped frame of the configured backend.
    """
    backend = get_config()["dataframe_backend"]
    return nw.from_dict(data, backend=backend)


def finalize(*, frame: nw.DataFrame[Any]) -> Any:
    """Apply the configured laziness and unwrap to a native frame.

    This is the single point where every public, frame-returning function
    in this package converts its internal, always-eager narwhals frame
    into the native object callers actually receive.

    Parameters
    ----------
    frame : narwhals.DataFrame
        The eager narwhals frame to finalize.

    Returns
    -------
    Any
        A native frame of the configured backend -- eager, or lazy if
        ``lazy=True`` is configured (e.g. a ``polars.LazyFrame``).
    """
    config = get_config()
    result: nw.DataFrame[Any] | nw.LazyFrame[Any] = (
        frame.lazy() if config["lazy"] else frame
    )
    return result.to_native()


def concat(
    *, frames: Sequence[nw.DataFrame[Any]], how: SchemaPolicy
) -> nw.DataFrame[Any]:
    """Stack multiple eager narwhals frames according to a schema policy.

    Used to combine one dataframe per requested period into a single
    result, reconciling any schema differences between periods (e.g. a
    column added in a later quarter) according to `how`.

    Parameters
    ----------
    frames : Sequence[narwhals.DataFrame]
        The per-period frames to stack, in the order they should appear.
    how : {"union", "intersection", "strict"}
        ``"union"`` outer-joins columns, nulling out any column a given
        frame lacks. ``"intersection"`` keeps only columns common to every
        frame. ``"strict"`` requires every frame to already share the exact
        same columns, raising `LayoutParseError` otherwise.

    Returns
    -------
    narwhals.DataFrame
        The stacked frame.

    Raises
    ------
    LayoutParseError
        If `how` is ``"strict"`` and the frames' columns are not identical.
    """
    if how == "union":
        return nw.concat(list(frames), how="diagonal")

    if how == "intersection":
        common = set(frames[0].columns)
        for frame in frames[1:]:
            common &= set(frame.columns)
        ordered = [name for name in frames[0].columns if name in common]
        selected = [frame.select(ordered) for frame in frames]
        return nw.concat(selected, how="vertical")

    if how == "strict":
        first_columns = set(frames[0].columns)
        for frame in frames[1:]:
            if set(frame.columns) != first_columns:
                raise LayoutParseError(
                    "schema_policy='strict' requires every stacked period to share "
                    "the exact same columns, but they differ; use 'union' or "
                    "'intersection' to reconcile schema differences across periods."
                )
        return nw.concat(list(frames), how="vertical")

    raise ValueError(
        f"Unknown schema policy {how!r}; expected 'union', 'intersection', or 'strict'."
    )


def _dataframe_type_of(data: Any) -> DataFrameType:
    """Identify which DataFrameType a native dataframe already is.

    Used by :func:`convert_dataframe_type` to short-circuit when `data`
    already is the requested type, so no conversion (and no copy) happens.

    Parameters
    ----------
    data : Any
        A native dataframe of any backend narwhals supports.

    Returns
    -------
    {"pandas", "pyarrow_table", "polars_lazyframe", "polars_dataframe"}
        The DataFrameType `data` already is.
    """
    frame = nw.from_native(data)
    is_lazy = isinstance(frame, nw.LazyFrame)
    if frame.implementation is nw.Implementation.PANDAS:
        return "pandas"
    if frame.implementation is nw.Implementation.PYARROW:
        return "pyarrow_table"
    if frame.implementation is nw.Implementation.POLARS:
        return "polars_lazyframe" if is_lazy else "polars_dataframe"
    raise AssertionError(  # pragma: no cover
        f"unsupported narwhals implementation: {frame.implementation!r}"
    )


def convert_dataframe_type(*, data: Any, dataframe_type: DataFrameType | None) -> Any:
    """Convert a native dataframe to a specific DataFrameType, if requested.

    This is the single point where every public, dataframe-returning method
    that supports a `dataframe_type` override applies it, as the last step
    before returning. Conversion goes through narwhals'
    ``to_pandas``/``to_polars``/``to_arrow`` methods, which are already as
    close to zero-copy as each backend allows; a `data` that is already the
    requested type is returned unchanged.

    Parameters
    ----------
    data : Any
        A native dataframe of any backend narwhals supports, built with
        whichever backend the caller used.
    dataframe_type : {"pandas", "pyarrow_table", "polars_lazyframe", \
"polars_dataframe"} or None
        The dataframe type to return `data` as. ``None`` returns `data`
        unchanged, whatever backend it happens to already be.

    Returns
    -------
    Any
        `data` converted to `dataframe_type`, or `data` itself if
        `dataframe_type` is ``None`` or already matches.

    Raises
    ------
    ValueError
        If `dataframe_type` is not one of the supported values.
    """
    if dataframe_type is None:
        return data
    if dataframe_type not in _SUPPORTED_DATAFRAME_TYPES:
        raise ValueError(
            f"dataframe_type must be one of {sorted(_SUPPORTED_DATAFRAME_TYPES)} "
            f"or None, got {dataframe_type!r}."
        )
    if _dataframe_type_of(data) == dataframe_type:
        return data

    frame = nw.from_native(data)
    if isinstance(frame, nw.LazyFrame):
        frame = frame.collect()
    if dataframe_type == "pandas":
        return frame.to_pandas()
    if dataframe_type == "pyarrow_table":
        return frame.to_arrow()
    if dataframe_type == "polars_dataframe":
        return frame.to_polars()
    return frame.to_polars().lazy()


def finalize_as(
    *, frame: nw.DataFrame[Any], dataframe_type: DataFrameType | None
) -> Any:
    """Finalize a frame and convert it to a DataFrameType, in one step.

    Combines :func:`finalize` and :func:`convert_dataframe_type`, the pair
    every standalone, dataframe-returning parsing function needs at its
    return boundary, so that combination lives in a single place rather
    than being repeated at each call site.

    Parameters
    ----------
    frame : narwhals.DataFrame
        The eager narwhals frame to finalize.
    dataframe_type : {"pandas", "pyarrow_table", "polars_lazyframe", \
"polars_dataframe"} or None
        The dataframe type to convert the finalized result to; ``None``
        leaves it as whatever `finalize` produced.

    Returns
    -------
    Any
        The finalized, and if requested converted, native dataframe.
    """
    return convert_dataframe_type(
        data=finalize(frame=frame), dataframe_type=dataframe_type
    )
