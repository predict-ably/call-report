"""Private narwhals-backed helpers for building and stacking dataframes.

Every reader in this package is written against these three primitives so
none of them need to know anything about the specific dataframe library
configured via :mod:`call_report.config`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal, TypeAlias, TypeVar, overload

import narwhals as nw

from call_report.config import get_config
from call_report.exceptions import LayoutParseError, ReshapeError

if TYPE_CHECKING:
    import pandas as pd
    import polars as pl
    import pyarrow as pa

    NativeDataFrame: TypeAlias = pd.DataFrame | pa.Table | pl.DataFrame | pl.LazyFrame
    """The closed set of native dataframe types this package converts between.

    Type-checking only -- never imported at runtime, so referencing it here
    does not make pandas/polars/pyarrow hard dependencies.
    """

DataFrameT = TypeVar("DataFrameT", bound="NativeDataFrame")

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


def finalize(*, frame: nw.DataFrame[Any]) -> NativeDataFrame:
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
    NativeDataFrame
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


def _dataframe_type_of(data: NativeDataFrame) -> DataFrameType:
    """Identify which DataFrameType a native dataframe already is.

    Used by :func:`convert_dataframe_type` to short-circuit when `data`
    already is the requested type, so no conversion (and no copy) happens.

    Parameters
    ----------
    data : NativeDataFrame
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


@overload
def convert_dataframe_type(
    *, data: DataFrameT, dataframe_type: None
) -> DataFrameT:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def convert_dataframe_type(
    *, data: NativeDataFrame, dataframe_type: Literal["pandas"]
) -> pd.DataFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def convert_dataframe_type(
    *, data: NativeDataFrame, dataframe_type: Literal["pyarrow_table"]
) -> pa.Table:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def convert_dataframe_type(
    *, data: NativeDataFrame, dataframe_type: Literal["polars_dataframe"]
) -> pl.DataFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def convert_dataframe_type(
    *, data: NativeDataFrame, dataframe_type: Literal["polars_lazyframe"]
) -> pl.LazyFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
def convert_dataframe_type(
    *, data: NativeDataFrame, dataframe_type: DataFrameType | None
) -> NativeDataFrame:
    """Convert a native dataframe to a specific DataFrameType, if requested.

    This is the single point where every public, dataframe-returning method
    that supports a `dataframe_type` override applies it, as the last step
    before returning. Conversion goes through narwhals'
    ``to_pandas``/``to_polars``/``to_arrow`` methods, which are already as
    close to zero-copy as each backend allows; a `data` that is already the
    requested type is returned unchanged.

    Parameters
    ----------
    data : NativeDataFrame
        A native dataframe of any backend narwhals supports, built with
        whichever backend the caller used.
    dataframe_type : {"pandas", "pyarrow_table", "polars_lazyframe", \
"polars_dataframe"} or None
        The dataframe type to return `data` as. ``None`` returns `data`
        unchanged, whatever backend it happens to already be.

    Returns
    -------
    NativeDataFrame
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


@overload
def finalize_as(
    *, frame: nw.DataFrame[Any], dataframe_type: None
) -> NativeDataFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def finalize_as(
    *, frame: nw.DataFrame[Any], dataframe_type: Literal["pandas"]
) -> pd.DataFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def finalize_as(
    *, frame: nw.DataFrame[Any], dataframe_type: Literal["pyarrow_table"]
) -> pa.Table:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def finalize_as(
    *, frame: nw.DataFrame[Any], dataframe_type: Literal["polars_dataframe"]
) -> pl.DataFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def finalize_as(
    *, frame: nw.DataFrame[Any], dataframe_type: Literal["polars_lazyframe"]
) -> pl.LazyFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
def finalize_as(
    *, frame: nw.DataFrame[Any], dataframe_type: DataFrameType | None
) -> NativeDataFrame:
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
    NativeDataFrame
        The finalized, and if requested converted, native dataframe.
    """
    return convert_dataframe_type(
        data=finalize(frame=frame), dataframe_type=dataframe_type
    )


def pivot(
    *, frame: nw.DataFrame[Any], on: str, index: list[str], values: str
) -> nw.DataFrame[Any]:
    """Pivot a long-shaped frame wide, working around a real pyarrow gap.

    narwhals' native ``pivot`` is not implemented for the pyarrow backend
    (confirmed: it raises ``NotImplementedError`` outright); for that one
    backend this falls back to `_manual_pivot`, a filter-and-join reshape
    verified to produce identical results to the native path on every
    other backend. `index` + `on` must be a unique grain -- a genuine
    duplicate raises `ReshapeError` on every backend rather than silently
    aggregating, whether that's narwhals' own native error (translated
    here) or `_manual_pivot`'s own explicit check.

    Parameters
    ----------
    frame : narwhals.DataFrame
        The long-shaped frame to pivot.
    on : str
        The column whose distinct values become new column names.
    index : list[str]
        The column(s) that stay fixed, identifying each output row.
    values : str
        The column supplying each new column's values.

    Returns
    -------
    narwhals.DataFrame
        The pivoted, wide-shaped frame, with columns in a deterministic
        (sorted) order.

    Raises
    ------
    ReshapeError
        If `index` + `on` is not a unique grain, or the pivot otherwise
        fails.
    """
    if frame.implementation is nw.Implementation.PYARROW:
        return _manual_pivot(frame=frame, on=on, index=index, values=values)
    try:
        return frame.pivot(on=on, index=index, values=values, sort_columns=True)
    except Exception as error:
        raise ReshapeError(
            f"Could not pivot on={on!r}, index={index!r}, values={values!r}: {error}"
        ) from error


def _manual_pivot(
    *, frame: nw.DataFrame[Any], on: str, index: list[str], values: str
) -> nw.DataFrame[Any]:
    """Pivot `frame` wide using filter-and-join, for backends without native pivot.

    One filter+join per distinct `on` value, so this is O(number of
    distinct columns) rather than native ``pivot``'s single pass -- the
    honest cost of a backend (pyarrow) lacking the primitive, not a
    hidden inefficiency. Verified to produce output identical to
    narwhals' native `nw.DataFrame.pivot` for the same input.

    Parameters
    ----------
    frame : narwhals.DataFrame
        The long-shaped frame to pivot.
    on : str
        The column whose distinct values become new column names.
    index : list[str]
        The column(s) that stay fixed, identifying each output row.
    values : str
        The column supplying each new column's values.

    Returns
    -------
    narwhals.DataFrame
        The pivoted, wide-shaped frame, with columns in a deterministic
        (sorted) order.

    Raises
    ------
    ReshapeError
        If `index` + `on` is not a unique grain.
    """
    grain_columns = [*index, on]
    if (
        frame.select(*grain_columns).unique(subset=grain_columns).shape[0]
        != frame.shape[0]
    ):
        raise ReshapeError(
            f"Could not pivot: index={index!r} + on={on!r} is not a unique grain."
        )

    key_rows = frame.select(on).unique(subset=[on]).sort(on).rows(named=True)
    pieces: list[nw.DataFrame[Any]] = []
    for row in key_rows:
        key_value = row[on]
        column_name = str(key_value)
        piece = (
            frame.filter(nw.col(on) == key_value)
            .select(*index, values)
            .rename({values: column_name})
        )
        pieces.append(piece)

    result = pieces[0]
    for piece in pieces[1:]:
        result = _join_on_index(left=result, right=piece, index=index)
    return result.sort(index)


def _join_on_index(
    *, left: nw.DataFrame[Any], right: nw.DataFrame[Any], index: list[str]
) -> nw.DataFrame[Any]:
    """Full-join two frames on `index`, coalescing the duplicated join-key columns.

    narwhals' ``"full"`` join does not coalesce the join keys -- it
    produces a ``{col}_right`` counterpart for each `index` column
    instead of merging them (confirmed on every backend this package
    supports). This fills each `index` column from its ``_right``
    counterpart wherever the left side is null, then drops the
    ``_right`` columns.

    Parameters
    ----------
    left : narwhals.DataFrame
        The left side of the join.
    right : narwhals.DataFrame
        The right side of the join.
    index : list[str]
        The shared column(s) to join and coalesce on.

    Returns
    -------
    narwhals.DataFrame
        The joined frame, with exactly one copy of each `index` column.
    """
    joined = left.join(right, on=index, how="full")
    for column in index:
        right_column = f"{column}_right"
        joined = joined.with_columns(
            nw.col(column).fill_null(nw.col(right_column)).alias(column)
        ).drop(right_column)
    return joined
