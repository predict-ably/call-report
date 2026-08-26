"""Private narwhals-backed helpers for building and stacking dataframes.

Every reader in this package is written against these three primitives so
none of them need to know anything about the specific dataframe library
configured via :mod:`call_report.config`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal, TypeAlias, TypeVar, overload

import narwhals as nw

from call_report.config import DataFrameBackend, get_config
from call_report.exceptions import LayoutParseError, ReshapeError

if TYPE_CHECKING:
    import pandas as pd
    import polars as pl
    import pyarrow as pa

    NativeDataFrame: TypeAlias = pd.DataFrame | pa.Table | pl.DataFrame | pl.LazyFrame
    """The closed set of native dataframe types this package converts between.

    Type-checking only. It is never imported at runtime, so referencing it
    here does not make pandas, polars, or pyarrow hard dependencies.
    """

DataFrameT = TypeVar("DataFrameT", bound="NativeDataFrame")

FrameOrLazy: TypeAlias = "nw.DataFrame[Any] | nw.LazyFrame[Any]"
"""A narwhals frame that may be eager or lazy, depending on config/backend.

Used throughout this module and `call_report.fca._reshape` for
intermediate reshaping steps that stay lazy when their input already is,
so a `polars.LazyFrame` source is not collected until an operation that
genuinely requires it, such as `pivot`.
"""

SchemaPolicy = Literal["union", "intersection", "strict"]
DataFrameType = Literal[
    "pandas", "pyarrow_table", "polars_lazyframe", "polars_dataframe"
]

_SUPPORTED_DATAFRAME_TYPES: frozenset[str] = frozenset(
    {"pandas", "pyarrow_table", "polars_lazyframe", "polars_dataframe"}
)


def date_dtype() -> nw.dtypes.DType:
    """Return the dtype a calendar date should be held as, per backend.

    polars and pyarrow both have a native date type. pandas does not. Its
    only date-like dtype is ``date32[pyarrow]``, which would make pyarrow
    a requirement for every pandas user, so pandas gets ``Datetime``
    instead. A pandas caller therefore gets a ``datetime64`` column whose
    time component is always midnight, which supports the ``.dt``
    accessor and round-trips through parquet. Without this, a Python
    ``datetime.date`` lands in an object column under pandas.

    Returns
    -------
    narwhals.dtypes.DType
        `narwhals.Date` on polars and pyarrow, `narwhals.Datetime` on
        pandas.

    Examples
    --------
    >>> from call_report.config import config_context
    >>> with config_context(dataframe_backend="polars"):
    ...     date_dtype()
    Date
    >>> with config_context(dataframe_backend="pandas"):
    ...     date_dtype()
    Datetime(time_unit='us', time_zone=None)
    """
    if get_config()["dataframe_backend"] == "pandas":
        return nw.Datetime("us")
    return nw.Date()


def build_frame(
    *,
    data: dict[str, list[Any]],
    schema: Mapping[str, nw.dtypes.DType] | None = None,
) -> nw.DataFrame[Any]:
    """Build an eager narwhals DataFrame from columnar data.

    Uses the dataframe library named by the current
    :func:`~call_report.config.get_config`. The result is never lazy,
    regardless of the ``"lazy"`` setting, because laziness is applied
    once, at the public return boundary, by :func:`finalize`.

    Pass `schema` whenever the caller already knows what the columns are
    supposed to be. Without it each backend infers a dtype from the
    values, which is not a decision the values can always support: a
    column that is entirely null, or that belongs to a frame with no
    rows, gives every backend nothing to infer from, and the three
    disagree about what to call the result.

    A declared dtype is a statement about the source, not a guarantee
    about the values. A column whose values cannot all be represented as
    the dtype declared for it keeps its inferred dtype instead, so one
    unparsable value costs that one column's declared dtype rather than
    the whole frame's.

    Parameters
    ----------
    data : dict[str, list[Any]]
        Column name to column values, as produced by a parser.
    schema : Mapping[str, narwhals.dtypes.DType], optional
        The dtype each column should have. A column of `data` absent from
        `schema` keeps its inferred dtype. ``None`` (the default) infers
        every column.

    Returns
    -------
    narwhals.DataFrame
        An eager narwhals-wrapped frame of the configured backend.
    """
    backend: DataFrameBackend = get_config()["dataframe_backend"]
    if schema is None:
        return nw.from_dict(data, backend=backend)
    declared = {name: schema[name] for name in data if name in schema}
    try:
        return _build_declared_frame(data=data, schema=declared, backend=backend)
    except Exception:
        # Each backend signals an unrepresentable value with its own
        # exception type (TypeError, ValueError, pyarrow.ArrowInvalid,
        # polars.InvalidOperationError), so the catch is by position in
        # the pipeline rather than by type.
        representable = {
            name: dtype
            for name, dtype in declared.items()
            if _is_representable(values=data[name], dtype=dtype, backend=backend)
        }
        return _build_declared_frame(data=data, schema=representable, backend=backend)


def _build_declared_frame(
    *,
    data: dict[str, list[Any]],
    schema: Mapping[str, nw.dtypes.DType],
    backend: DataFrameBackend,
) -> nw.DataFrame[Any]:
    """Build an eager frame whose columns carry the dtypes `schema` declares.

    pandas cannot be handed a declared schema the way polars and pyarrow
    can. Its default dtypes are numpy-backed, so a declared ``Int64``
    column holding a null has no representation and
    ``narwhals.from_dict`` raises. For that backend this builds with
    inferred dtypes and then casts to the nullable (masked) equivalents,
    which gives pandas a null it can hold. narwhals reports those
    extension dtypes under the same names polars and pyarrow use, so all
    three agree on the result.

    Parameters
    ----------
    data : dict[str, list[Any]]
        Column name to column values, as produced by a parser.
    schema : Mapping[str, narwhals.dtypes.DType]
        The dtype each column should have. A column of `data` absent from
        it keeps its inferred dtype.
    backend : {"pandas", "polars", "pyarrow"}
        The configured dataframe backend to build with.

    Returns
    -------
    narwhals.DataFrame
        An eager narwhals-wrapped frame of `backend`.
    """
    if backend != "pandas":
        # narwhals.from_dict requires the schema to name every column, so a
        # schema covering only some of them is completed from what the
        # backend infers for the rest.
        complete = dict(schema)
        if len(complete) != len(data):
            inferred = nw.from_dict(data, backend=backend).collect_schema()
            complete = {
                name: complete.get(name, dtype) for name, dtype in inferred.items()
            }
        return nw.from_dict(data, schema=nw.Schema(complete), backend=backend)
    native = nw.from_dict(data, backend=backend).to_native()
    # Schema.to_pandas() maps the narwhals dtypes onto pandas' nullable
    # dtype names, so this reaches pandas' own extension dtypes without
    # importing pandas.
    pandas_dtypes = nw.Schema(schema).to_pandas(dtype_backend="numpy_nullable")
    return nw.from_native(native.astype(pandas_dtypes), eager_only=True)


def _is_representable(
    *, values: list[Any], dtype: nw.dtypes.DType, backend: DataFrameBackend
) -> bool:
    """Report whether one column's values can be held as `dtype`.

    Used only after a whole-frame build against the declared schema has
    already failed, to find which columns are responsible.

    Parameters
    ----------
    values : list[Any]
        One column's values.
    dtype : narwhals.dtypes.DType
        The dtype declared for that column.
    backend : {"pandas", "polars", "pyarrow"}
        The configured dataframe backend to build with.

    Returns
    -------
    bool
        True if a single-column frame of `values` can be built as `dtype`.
    """
    try:
        _build_declared_frame(
            data={"column": values}, schema={"column": dtype}, backend=backend
        )
    except Exception:
        return False
    return True


def finalize(*, frame: FrameOrLazy) -> NativeDataFrame:
    """Apply the configured laziness and unwrap to a native frame.

    This is the single point where every public, frame-returning function
    in this package converts its internal narwhals frame into the native
    object callers actually receive. `frame` is usually eager, but may
    already be lazy if it was built from another already-lazy source.
    `nw.LazyFrame.lazy()` is a no-op on an already-lazy frame, so this
    handles both cases without needing to know which it got.

    Parameters
    ----------
    frame : narwhals.DataFrame or narwhals.LazyFrame
        The narwhals frame to finalize.

    Returns
    -------
    NativeDataFrame
        A native frame of the configured backend. It is eager unless
        ``lazy=True`` is configured, in which case it is lazy (e.g. a
        ``polars.LazyFrame``).
    """
    config = get_config()
    result: FrameOrLazy = frame.lazy() if config["lazy"] else frame
    return result.to_native()


@overload
def concat(
    *, frames: Sequence[nw.DataFrame[Any]], how: SchemaPolicy
) -> nw.DataFrame[Any]:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def concat(
    *, frames: Sequence[nw.LazyFrame[Any]], how: SchemaPolicy
) -> nw.LazyFrame[Any]:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def concat(
    *, frames: Sequence[FrameOrLazy], how: SchemaPolicy
) -> FrameOrLazy:  # numpydoc ignore=GL08
    ...  # pragma: no cover
def concat(*, frames: Sequence[FrameOrLazy], how: SchemaPolicy) -> FrameOrLazy:
    """Stack multiple narwhals frames according to a schema policy.

    Used to combine one dataframe per requested period into a single
    result, reconciling any schema differences between periods (e.g. a
    column added in a later quarter) according to `how`. `frames` must be
    either all eager or all lazy, since narwhals, like polars, cannot
    concatenate a mix of the two.

    Parameters
    ----------
    frames : Sequence[narwhals.DataFrame or narwhals.LazyFrame]
        The per-period frames to stack, in the order they should appear.
    how : {"union", "intersection", "strict"}
        ``"union"`` outer-joins columns, nulling out any column a given
        frame lacks. ``"intersection"`` keeps only columns common to every
        frame. ``"strict"`` requires every frame to already share the exact
        same columns, raising `LayoutParseError` otherwise.

    Returns
    -------
    narwhals.DataFrame or narwhals.LazyFrame
        The stacked frame, lazy if `frames` were lazy and eager otherwise.

    Raises
    ------
    LayoutParseError
        If `how` is ``"strict"`` and the frames' columns are not identical.
    """
    # Column names are read via `collect_schema()` rather than `.columns`,
    # since the latter emits a `PerformanceWarning` on a `LazyFrame`.
    #
    # nw.concat's own signature binds a single FrameT, so it can't statically
    # express "homogeneously eager or homogeneously lazy, whichever `frames`
    # happens to be". Every caller in this codebase only ever passes frames
    # sharing one call's laziness state (see FrameOrLazy), so this is a real
    # runtime invariant the type system just can't see.
    if how == "union":
        return nw.concat(list(frames), how="diagonal")  # type: ignore[type-var]

    if how == "intersection":
        schemas = [frame.collect_schema().names() for frame in frames]
        common = set(schemas[0])
        for names in schemas[1:]:
            common &= set(names)
        ordered = [name for name in schemas[0] if name in common]
        selected = [frame.select(ordered) for frame in frames]
        return nw.concat(selected, how="vertical")  # type: ignore[type-var]

    if how == "strict":
        column_sets = [set(frame.collect_schema().names()) for frame in frames]
        first_columns = column_sets[0]
        for columns in column_sets[1:]:
            if columns != first_columns:
                raise LayoutParseError(
                    "schema_policy='strict' requires every stacked period to share "
                    "the exact same columns, but they differ; use 'union' or "
                    "'intersection' to reconcile schema differences across periods."
                )
        return nw.concat(list(frames), how="vertical")  # type: ignore[type-var]

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


def _with_pandas_dates(*, frame: nw.DataFrame[Any]) -> nw.DataFrame[Any]:
    """Recast any `narwhals.Date` column to the dtype pandas can hold.

    pandas has no date dtype (see `date_dtype`), so converting a polars or
    pyarrow frame with a Date column straight to pandas produces an object
    column of `datetime.date` values, or a datetime of whichever time unit
    that backend happens to pick. Casting before the conversion gives the
    same `Datetime` a pandas-native frame would already have. A Date has
    no time component, so this never loses information.

    Parameters
    ----------
    frame : narwhals.DataFrame
        The eager frame about to be converted to pandas.

    Returns
    -------
    narwhals.DataFrame
        `frame`, with every Date column cast to `narwhals.Datetime`.
    """
    schema = frame.collect_schema()
    dates = [name for name, dtype in schema.items() if dtype == nw.Date]
    if not dates:
        return frame
    return frame.with_columns(nw.col(name).cast(nw.Datetime("us")) for name in dates)


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
    close to zero-copy as each backend allows. A `data` that is already the
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
        return _with_pandas_dates(frame=frame).to_pandas()
    if dataframe_type == "pyarrow_table":
        return frame.to_arrow()
    if dataframe_type == "polars_dataframe":
        return frame.to_polars()
    return frame.to_polars().lazy()


@overload
def finalize_as(
    *, frame: FrameOrLazy, dataframe_type: None
) -> NativeDataFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def finalize_as(
    *, frame: FrameOrLazy, dataframe_type: Literal["pandas"]
) -> pd.DataFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def finalize_as(
    *, frame: FrameOrLazy, dataframe_type: Literal["pyarrow_table"]
) -> pa.Table:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def finalize_as(
    *, frame: FrameOrLazy, dataframe_type: Literal["polars_dataframe"]
) -> pl.DataFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def finalize_as(
    *, frame: FrameOrLazy, dataframe_type: Literal["polars_lazyframe"]
) -> pl.LazyFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
def finalize_as(
    *, frame: FrameOrLazy, dataframe_type: DataFrameType | None
) -> NativeDataFrame:
    """Finalize a frame and convert it to a DataFrameType, in one step.

    Combines :func:`finalize` and :func:`convert_dataframe_type`, the pair
    every standalone, dataframe-returning parsing function needs at its
    return boundary, so that combination lives in a single place rather
    than being repeated at each call site.

    Parameters
    ----------
    frame : narwhals.DataFrame or narwhals.LazyFrame
        The narwhals frame to finalize. Usually eager, but may already be
        lazy (see :func:`finalize`).
    dataframe_type : {"pandas", "pyarrow_table", "polars_lazyframe", \
"polars_dataframe"} or None
        The dataframe type to convert the finalized result to. ``None``
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
    *, frame: FrameOrLazy, on: str, index: list[str], values: str
) -> nw.DataFrame[Any]:
    """Pivot a long-shaped frame wide, including on the pyarrow backend.

    Pivoting requires eager data, because the output schema depends on
    `on`'s distinct values and those cannot be known without
    materializing. This is therefore the one place a lazy `frame` (e.g. a
    `polars.LazyFrame`-backed pipeline built by
    `call_report.fca._reshape`) is finally collected. Every operation
    before this point (melt, concat, column-key computation) stays lazy
    for as long as `frame` lets it.

    narwhals' native ``pivot`` raises ``NotImplementedError`` on the
    pyarrow backend, so for that backend this falls back to
    `_manual_pivot`, a filter-and-join reshape producing the same result.
    `index` plus `on` must be a unique grain. A duplicate raises
    `ReshapeError` on every backend rather than silently aggregating,
    either from narwhals' own error (translated here) or from
    `_manual_pivot`'s explicit check.

    Parameters
    ----------
    frame : narwhals.DataFrame or narwhals.LazyFrame
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
    if isinstance(frame, nw.LazyFrame):
        frame = frame.collect()
    if frame.implementation is nw.Implementation.PYARROW:
        return _manual_pivot(frame=frame, on=on, index=index, values=values)
    try:
        return frame.pivot(on=on, index=index, values=values, sort_columns=True)
    except Exception as error:
        raise ReshapeError(
            f"Could not pivot on={on!r}, index={index!r}, values={values!r}: {error}"
        ) from error


def assert_unique_grain(
    *, frame: FrameOrLazy, columns: Sequence[str]
) -> nw.DataFrame[Any]:
    """Collect `frame` (if lazy) and raise unless `columns` is a unique grain.

    Checking uniqueness means looking at the actual data, which is not a
    lazy-safe operation. This is the one place a caller that otherwise
    stays lazy end to end is forced to collect.

    Parameters
    ----------
    frame : narwhals.DataFrame or narwhals.LazyFrame
        The frame to check.
    columns : Sequence[str]
        The column(s) that together must identify each row uniquely.

    Returns
    -------
    narwhals.DataFrame
        `frame`, collected if it was lazy.

    Raises
    ------
    ReshapeError
        If `columns` is not a unique grain.
    """
    if isinstance(frame, nw.LazyFrame):
        frame = frame.collect()
    columns = list(columns)
    if frame.select(*columns).unique(subset=columns).shape[0] != frame.shape[0]:
        raise ReshapeError(f"columns={columns!r} is not a unique grain.")
    return frame


def _manual_pivot(
    *, frame: nw.DataFrame[Any], on: str, index: list[str], values: str
) -> nw.DataFrame[Any]:
    """Pivot `frame` wide using filter-and-join, for backends without native pivot.

    Runs one filter and join per distinct `on` value, so it costs
    O(number of distinct columns) rather than native ``pivot``'s single
    pass. It produces the same output as `nw.DataFrame.pivot` for the
    same input, and exists only for backends without a native pivot.

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
    try:
        frame = assert_unique_grain(frame=frame, columns=[*index, on])
    except ReshapeError as error:
        raise ReshapeError(
            f"Could not pivot: index={index!r} + on={on!r} is not a unique grain."
        ) from error

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

    narwhals' ``"full"`` join does not coalesce the join keys. On every
    backend this package supports it produces a ``{col}_right``
    counterpart for each `index` column instead of merging them. This
    fills each `index` column from its ``_right`` counterpart wherever
    the left side is null, then drops the ``_right`` columns.

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
