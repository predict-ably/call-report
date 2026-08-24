"""Private wide- and long-format reshaping logic behind ``FCACallReport``.

This module is private and covers exactly what `to_wide_format`,
`to_long_format`, and the standalone `convert_wide_format_to_long_format`
and `convert_long_format_to_wide_format` functions need. It melts each
already-loaded schedule's frame into a long-shaped intermediate, tagging a
code-bearing schedule's code column distinctly from a plain variable, and
stacks every schedule together. From there it either computes each row's
wide column name and pivots, or tags single and coded rows directly as the
long-format result.

Every function here accepts and returns `FrameOrLazy`. If a schedule's
frame is already a `polars.LazyFrame` (``lazy=True`` configured), the melt,
concat, and column-key steps all stay lazy too. Each entry point collects
exactly once: `to_wide_format` at `pivot`, since a pivoted result's schema
depends on data values, and `to_long_format` at its own grain-uniqueness
check, since checking the data is likewise not a lazy-safe operation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, overload

import narwhals as nw

from call_report.config import config_context
from call_report.core._backend import (
    DataFrameType,
    FrameOrLazy,
    assert_unique_grain,
    build_frame,
    concat,
    finalize_as,
    pivot,
)
from call_report.exceptions import ReshapeError
from call_report.fca.layout import FIXED_IDENTIFIER_COLUMNS

if TYPE_CHECKING:
    import pandas as pd
    import polars as pl
    import pyarrow as pa

    from call_report.core._backend import NativeDataFrame

RESHAPE_INDEX: tuple[str, ...] = ("UNINUM", "period")
"""tuple[str, ...]: The grain every wide- and long-format row is keyed by."""

_EXCLUDED_FROM_MELT = frozenset({*FIXED_IDENTIFIER_COLUMNS, "period"})

_LONG_FORMAT_GRAIN: tuple[str, ...] = (
    *RESHAPE_INDEX,
    "schedule",
    "code_column",
    "code_value",
    "variable_name",
)
"""tuple[str, ...]: The grain `to_long_format` guarantees is unique."""


def _cast_unknown_dtype(
    frame: FrameOrLazy, *, column: str, target: nw.dtypes.DType
) -> FrameOrLazy:
    """Cast `column` to `target` if narwhals couldn't infer any concrete dtype.

    Two real cases leave narwhals unable to infer any concrete dtype for
    a column: a schedule with zero rows for a period, such as RCO at
    2000Q1, and a field that is entirely null even though rows exist, such
    as RCF1's `value` at 2000Q1. This affects every column, not just
    numeric ones, so RCO's `UNINUM` identifier is also `narwhals.Unknown`
    when RCO has zero rows.

    Left alone, an `Unknown` column reaches the same cross-piece concat as
    a real typed one and can raise ``polars.exceptions.SchemaError: type
    Int64 is incompatible with expected type Null``. Whether it does
    depends on the installed polars version and on concat order, so the
    failure is intermittent across platforms. A column that already has a
    concrete dtype is left as-is. This helper only resolves the specific
    ambiguity of having no data to infer from.

    Parameters
    ----------
    frame : narwhals.DataFrame or narwhals.LazyFrame
        A frame with a `column` column.
    column : str
        The column to normalize.
    target : narwhals.dtypes.DType
        The dtype to cast `column` to, if it's currently `Unknown`.

    Returns
    -------
    narwhals.DataFrame or narwhals.LazyFrame
        `frame`, with `column` cast to `target` if its dtype was unknown.
    """
    if isinstance(frame.collect_schema()[column], nw.Unknown):
        return frame.with_columns(nw.col(column).cast(target))
    return frame


def _cast_numeric_to_float64(frame: FrameOrLazy, *, column: str) -> FrameOrLazy:
    """Cast `column` to a consistent Float64 if it is numeric or unknown.

    Different fields carry different declared decimal positions, so two
    independently melted pieces can each infer a different numeric dtype
    (Int64 or Float64) for the same column. Those pieces are either two
    different schedules, or the coded and trailing split within one
    ``single_multiple_single`` schedule. Concatenating pieces with
    mismatched numeric dtypes raises ``polars.exceptions.SchemaError``
    depending on which piece concatenates first, so normalizing every
    numeric occurrence of `column` to Float64 up front removes that
    order-dependence.

    Used for both `"value"`, holding every schedule's measures, and
    `"code_value"`, holding every code-bearing schedule's own code. A code
    is always Int64 in practice, but is normalized for the same reason.
    This also resolves `column` being `Unknown` (see `_cast_unknown_dtype`).
    `"value"` and `"code_value"` are always numeric when populated, since
    no non-identifier `FCASchedule` field is ever ``Alphanum.``, so
    `Unknown` is safe to treat as Float64 here. A genuinely non-numeric
    column with real data is left as-is.

    Parameters
    ----------
    frame : narwhals.DataFrame or narwhals.LazyFrame
        A frame with a `column` column.
    column : str
        The column to normalize.

    Returns
    -------
    narwhals.DataFrame or narwhals.LazyFrame
        `frame`, with `column` cast to Float64 if it was numeric or of
        unknown (empty/all-null) type.
    """
    # `collect_schema()` rather than the `.schema` property, which emits a
    # `PerformanceWarning` when `frame` is a `LazyFrame`.
    if frame.collect_schema()[column].is_numeric():
        return frame.with_columns(nw.col(column).cast(nw.Float64))
    return _cast_unknown_dtype(frame, column=column, target=nw.Float64())


def melt_schedule_frame(
    *,
    frame: FrameOrLazy,
    schedule: str,
    code_column: str | None,
    trailing_columns: tuple[str, ...] = (),
) -> FrameOrLazy:
    """Melt one already-loaded, already-stacked schedule frame into long shape.

    Every column except `FIXED_IDENTIFIER_COLUMNS`, ``"period"``,
    `code_column`, and `trailing_columns` becomes a `variable_name`/
    `value` row pair, alongside `code_column`'s own values kept as a
    `code_value` column (rather than melted), so a code-bearing
    schedule's rows stay distinguishable by which code they belong to.

    A ``single_multiple_single``-scenario schedule's `trailing_columns`
    (e.g. RCR7's ``AvgDailyRWARegCap``) are single-occurrence fields that
    the loader repeats identically on every code-row of a UNINUM and
    period. Melting them the same way as a coded field would produce one
    redundant, identical-valued wide column per code instead of one clean
    column. They are melted separately here, from a frame first
    deduplicated down to one row per `RESHAPE_INDEX` grain, and tagged with
    no code column, so they key as plain ``{schedule}__{variable}``
    columns. That relies on the same union-concat null-fill used for a
    schedule that has no code column.

    `UNINUM` is cast to Int64 up front if it is `Unknown`, which happens
    when a schedule has zero rows for this period (see
    `_cast_unknown_dtype`). It is not touched by
    `_cast_numeric_to_float64`'s later `"value"` and `"code_value"` calls,
    but carries the same cross-piece concat risk.

    Parameters
    ----------
    frame : narwhals.DataFrame or narwhals.LazyFrame
        One schedule's already period-stacked frame (i.e. what
        `FCACallReport._load` produces, wrapped back into narwhals).
    schedule : str
        The schedule's root name (e.g. ``"RCB"``), recorded on every row.
    code_column : str, optional
        The schedule's code column name (`FCALayout.multi_columns[0]`),
        or ``None`` for a schedule with no code column.
    trailing_columns : tuple[str, ...], default ()
        The schedule's trailing single-occurrence columns
        (`FCALayout.trailing_columns`), if any.

    Returns
    -------
    narwhals.DataFrame or narwhals.LazyFrame
        Columns ``UNINUM``, ``period``, ``schedule``, ``variable_name``,
        ``value``, plus ``code_column``/``code_value`` for any row that
        came from a coded field. Lazy if `frame` was lazy.
    """
    frame = _cast_unknown_dtype(frame, column="UNINUM", target=nw.Int64())
    exclude = set(_EXCLUDED_FROM_MELT) | set(trailing_columns)
    if code_column is not None:
        exclude.add(code_column)
    melt_columns = [
        name for name in frame.collect_schema().names() if name not in exclude
    ]

    index_columns = [*RESHAPE_INDEX]
    if code_column is not None:
        index_columns.append(code_column)

    melted = frame.unpivot(
        on=melt_columns,
        index=index_columns,
        variable_name="variable_name",
        value_name="value",
    )
    melted = _cast_numeric_to_float64(melted, column="value").with_columns(
        nw.lit(schedule).alias("schedule")
    )
    if code_column is not None:
        melted = melted.rename({code_column: "code_value"}).with_columns(
            nw.lit(code_column).alias("code_column")
        )
        melted = _cast_numeric_to_float64(melted, column="code_value")

    if not trailing_columns:
        return melted

    trailing_melted = (
        frame.select(*RESHAPE_INDEX, *trailing_columns)
        .unique(subset=list(RESHAPE_INDEX))
        .unpivot(
            on=list(trailing_columns),
            index=list(RESHAPE_INDEX),
            variable_name="variable_name",
            value_name="value",
        )
    )
    trailing_melted = _cast_numeric_to_float64(trailing_melted, column="value")
    trailing_melted = trailing_melted.with_columns(nw.lit(schedule).alias("schedule"))
    return concat(frames=[melted, trailing_melted], how="union")


def _with_column_key(frame: FrameOrLazy) -> FrameOrLazy:
    """Compute the wide-format column name for every melted row.

    A row with no code column keys as ``{schedule}__{variable_name}``. A
    row with a code column keys as
    ``{schedule}__{code_column}_{code_value}__{variable_name}``.

    The key is built with `narwhals.concat_str` rather than ``+``, because
    pyarrow's `Series.__add__` has no string-concatenation kernel, only
    numeric addition. `code_value`'s cast to a string goes through
    `fill_null` first, because casting a `Float64`-with-null column
    straight to `Int64` raises on the pandas backend, and that is the
    normal shape here once code and non-code schedules are concatenated
    together.

    Parameters
    ----------
    frame : narwhals.DataFrame or narwhals.LazyFrame
        The concatenated, melted frame, with `schedule`/`variable_name`
        columns and (for at least one row) `code_column`/`code_value`.

    Returns
    -------
    narwhals.DataFrame or narwhals.LazyFrame
        `frame` with an added `column_key` string column.
    """
    # `collect_schema()` rather than `.columns`, which emits a
    # `PerformanceWarning` on a `LazyFrame`.
    if "code_column" not in frame.collect_schema():
        return frame.with_columns(
            nw.concat_str(
                [nw.col("schedule"), nw.col("variable_name")], separator="__"
            ).alias("column_key")
        )

    code_value_text = nw.col("code_value").fill_null(0).cast(nw.Int64).cast(nw.String)
    plain_key = nw.concat_str(
        [nw.col("schedule"), nw.col("variable_name")], separator="__"
    )
    coded_key = nw.concat_str(
        [
            nw.col("schedule"),
            nw.concat_str([nw.col("code_column"), code_value_text], separator="_"),
            nw.col("variable_name"),
        ],
        separator="__",
    )
    return frame.with_columns(
        nw.when(nw.col("code_column").is_null())
        .then(plain_key)
        .otherwise(coded_key)
        .alias("column_key")
    )


def to_wide_format(
    *,
    frames: dict[str, FrameOrLazy],
    code_columns: dict[str, str | None],
    trailing_columns: dict[str, tuple[str, ...]],
) -> nw.DataFrame[Any]:
    """Build the wide-format frame from a set of already-loaded schedules.

    Melts and tags every schedule via `melt_schedule_frame`, concatenates
    them, computes each row's `column_key`, then pivots on it. A schedule
    with no code column gets null `code_column` and `code_value` once
    unioned against schedules that have them. Everything before `pivot`
    stays lazy if `frames`' values are lazy. `pivot` is the one step that
    must collect, since a pivoted result's schema depends on
    `column_key`'s distinct values.

    Parameters
    ----------
    frames : dict[str, narwhals.DataFrame or narwhals.LazyFrame]
        Each schedule's already-loaded, already-stacked frame, keyed by
        schedule root name.
    code_columns : dict[str, str or None]
        Each schedule's code column name, keyed the same way as `frames`.
        Use ``None`` for a schedule with no code column.
    trailing_columns : dict[str, tuple[str, ...]]
        Each schedule's trailing single-occurrence columns, keyed the
        same way as `frames`. Use an empty tuple for a schedule with
        none.

    Returns
    -------
    narwhals.DataFrame
        One row per `RESHAPE_INDEX` grain, one column per `column_key`.
    """
    melted = [
        melt_schedule_frame(
            frame=frame,
            schedule=schedule,
            code_column=code_columns[schedule],
            trailing_columns=trailing_columns[schedule],
        )
        for schedule, frame in frames.items()
    ]
    combined = concat(frames=melted, how="union")
    combined = _with_column_key(combined)
    return pivot(
        frame=combined, on="column_key", index=list(RESHAPE_INDEX), values="value"
    )


def _with_is_multiple_flag(frame: FrameOrLazy) -> FrameOrLazy:
    """Add `is_multiple`: True for a coded (multi-occurrence) variable's row.

    `code_column` and `code_value` are already null for a
    single-occurrence variable, via the same union-concat null-fill
    `to_wide_format` relies on. `is_multiple` is an explicit, filterable
    flag for that same fact, matching `FCALayout.scenario`'s own
    "single"/"multiple" vocabulary, so callers do not have to check
    `code_column.is_null()` themselves.

    If every requested schedule is non-coded, `code_column` and
    `code_value` are absent from the schema entirely rather than merely
    null. They are added here as all-null columns of the long format's
    declared types, so the long-format schema is always complete whichever
    schedules were requested.

    Parameters
    ----------
    frame : narwhals.DataFrame or narwhals.LazyFrame
        The concatenated, melted frame.

    Returns
    -------
    narwhals.DataFrame or narwhals.LazyFrame
        `frame` with `code_column`/`code_value` guaranteed present, plus
        an added `is_multiple` boolean column.
    """
    # `collect_schema()` rather than `.columns`, which emits a
    # `PerformanceWarning` on a `LazyFrame`.
    if "code_column" not in frame.collect_schema():
        return frame.with_columns(
            nw.lit(None, dtype=nw.String).alias("code_column"),
            nw.lit(None, dtype=nw.Float64).alias("code_value"),
            nw.lit(value=False).alias("is_multiple"),
        )
    return frame.with_columns((~nw.col("code_column").is_null()).alias("is_multiple"))


def to_long_format(
    *,
    frames: dict[str, FrameOrLazy],
    code_columns: dict[str, str | None],
    trailing_columns: dict[str, tuple[str, ...]],
) -> nw.DataFrame[Any]:
    """Build the long-format frame from a set of already-loaded schedules.

    Melts and tags every schedule via `melt_schedule_frame`, the same
    per-schedule step `to_wide_format` uses, concatenates them, and adds
    `is_multiple`. Unlike `to_wide_format`, there is no pivot, so the melt,
    concat, and flag steps all stay lazy if `frames`' values are lazy. The
    one place this collects is the final `assert_unique_grain` call, which
    verifies `_LONG_FORMAT_GRAIN` is actually unique in the data. That is
    not a lazy-safe question, so it is a single unavoidable collect,
    matching `to_wide_format`'s own collect at `pivot`.

    Parameters
    ----------
    frames : dict[str, narwhals.DataFrame or narwhals.LazyFrame]
        Each schedule's already-loaded, already-stacked frame, keyed by
        schedule root name.
    code_columns : dict[str, str or None]
        Each schedule's code column name, keyed the same way as `frames`.
        Use ``None`` for a schedule with no code column.
    trailing_columns : dict[str, tuple[str, ...]]
        Each schedule's trailing single-occurrence columns, keyed the
        same way as `frames`. Use an empty tuple for a schedule with
        none.

    Returns
    -------
    narwhals.DataFrame
        One row per `_LONG_FORMAT_GRAIN` grain.

    Raises
    ------
    ReshapeError
        If `_LONG_FORMAT_GRAIN` is not a unique grain, which means a
        duplicated row in the source data.
    """
    melted = [
        melt_schedule_frame(
            frame=frame,
            schedule=schedule,
            code_column=code_columns[schedule],
            trailing_columns=trailing_columns[schedule],
        )
        for schedule, frame in frames.items()
    ]
    combined = concat(frames=melted, how="union")
    combined = _with_is_multiple_flag(combined)
    return assert_unique_grain(frame=combined, columns=_LONG_FORMAT_GRAIN)


def _parse_wide_column_key(
    column_key: str,
) -> tuple[str, str | None, float | None, bool, str]:
    """Parse a wide-format column name into its long-format components.

    Inverts the naming `_with_column_key` builds:
    ``{schedule}__{variable}`` for a plain field and
    ``{schedule}__{code_column}_{code_value}__{variable}`` for a coded one.
    Splitting on ``"__"`` is unambiguous, because no FCA schedule or field
    name contains a literal ``"__"``. For the coded case, `code_column` and
    `code_value` are recovered by splitting the middle segment from the
    right on a single ``"_"``. That isolates `code_value`, which is always
    purely numeric digits, even when `code_column` itself contains an
    underscore, as in ``"INV_CODE"``.

    Parameters
    ----------
    column_key : str
        One wide-format column name (i.e. not one of `RESHAPE_INDEX`).

    Returns
    -------
    tuple[str, str or None, float or None, bool, str]
        ``(schedule, code_column, code_value, is_multiple, variable_name)``.

    Raises
    ------
    ReshapeError
        If `column_key` matches neither the plain nor the coded pattern.
    """
    parts = column_key.split("__")
    if len(parts) == 2:
        schedule, variable_name = parts
        return schedule, None, None, False, variable_name
    if len(parts) == 3:
        schedule, coded, variable_name = parts
        code_column, _, code_value_text = coded.rpartition("_")
        if code_column and code_value_text.isdigit():
            return schedule, code_column, float(code_value_text), True, variable_name

    raise ReshapeError(
        f"{column_key!r} doesn't match the wide-format naming convention "
        "('{schedule}__{variable}' or "
        "'{schedule}__{code_column}_{code_value}__{variable}')."
    )


@overload
def convert_wide_format_to_long_format(
    *, wide: NativeDataFrame, dataframe_type: None = None
) -> NativeDataFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def convert_wide_format_to_long_format(
    *, wide: NativeDataFrame, dataframe_type: Literal["pandas"]
) -> pd.DataFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def convert_wide_format_to_long_format(
    *, wide: NativeDataFrame, dataframe_type: Literal["pyarrow_table"]
) -> pa.Table:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def convert_wide_format_to_long_format(
    *, wide: NativeDataFrame, dataframe_type: Literal["polars_dataframe"]
) -> pl.DataFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def convert_wide_format_to_long_format(
    *, wide: NativeDataFrame, dataframe_type: Literal["polars_lazyframe"]
) -> pl.LazyFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
def convert_wide_format_to_long_format(
    *, wide: NativeDataFrame, dataframe_type: DataFrameType | None = None
) -> NativeDataFrame:
    """Convert an already-built wide-format frame to long format.

    This function is self-contained. `wide`'s own column names fully
    describe the long-format row each one unpivots to (see
    `_parse_wide_column_key`), so it needs no `FCACallReport` instance,
    layout lookups, or other external metadata.

    Column names are schema rather than data. A wide frame has at most a
    few thousand columns, known statically via `collect_schema`, so they
    are parsed in plain Python rather than with narwhals string
    expressions. That is necessary, not merely stylistic:
    `narwhals.Expr.str.split` requires a pyarrow-backed pandas series and
    raises ``TypeError`` on plain numpy-backed pandas, this package's
    default backend, and `Expr.str` has no regex-capture-group equivalent
    to work around it. The reshape itself, one `unpivot` plus a `join`
    against a small lookup frame built from the parsed column names, uses
    only lazy-safe narwhals operations, so it stays a deferred,
    uncollected query when `wide` is a `polars.LazyFrame`. No
    duplicate-grain check is needed either, since each wide column maps to
    exactly one `(schedule, code_column, code_value, variable_name)` tuple
    by construction, making the long-format grain unique automatically.

    Every row with a non-null `value` matches what
    `FCACallReport.to_long_format` would build directly from the same
    source data, but row *counts* can still differ. Pivoting fills in every
    `(UNINUM, period)` by wide-column combination as an explicit null row,
    including combinations no institution actually reported, such as an
    investment code one institution used and another never did.
    `to_long_format` only ever has a row for a combination that genuinely
    appeared in the source. Filter to non-null `value` before comparing
    row-for-row against a directly-built long-format frame.

    Parameters
    ----------
    wide : NativeDataFrame
        A wide-format frame, e.g. from `FCACallReport.to_wide_format`.
    dataframe_type : {"pandas", "pyarrow_table", "polars_lazyframe", \
"polars_dataframe"}, optional
        The dataframe type to convert the result to as a final step.
        Leave this ``None`` (the default) to get back whatever backend
        `call_report.config.get_config` currently has configured.

    Returns
    -------
    NativeDataFrame
        The long-format frame (see `FCACallReport.to_long_format` for the
        schema), of the configured backend, or of `dataframe_type` if it
        was supplied.

    Raises
    ------
    ReshapeError
        If a column name in `wide` (other than `RESHAPE_INDEX`) matches
        neither the plain nor the coded wide-format naming convention.

    Examples
    --------
    >>> from call_report.fca.transport import PackagedArchiveTransport
    >>> from call_report.fca.report import FCACallReport
    >>> report = FCACallReport(
    ...     start="2026-03-31",
    ...     end="2026-03-31",
    ...     transport=PackagedArchiveTransport(),
    ... )
    >>> wide = report.to_wide_format(schedules=["RC", "RCB"])
    >>> long = convert_wide_format_to_long_format(wide=wide)
    >>> sorted(long.columns)
    ['UNINUM', 'code_column', 'code_value', 'is_multiple', 'period', \
'schedule', 'value', 'variable_name']
    """
    frame = nw.from_native(wide)
    value_columns = [
        name for name in frame.collect_schema().names() if name not in RESHAPE_INDEX
    ]
    parsed = [_parse_wide_column_key(name) for name in value_columns]
    lookup_data: dict[str, list[Any]] = {
        "column_key": value_columns,
        "schedule": [item[0] for item in parsed],
        "code_column": [item[1] for item in parsed],
        "code_value": [item[2] for item in parsed],
        "is_multiple": [item[3] for item in parsed],
        "variable_name": [item[4] for item in parsed],
    }
    with config_context(dataframe_backend=frame.implementation.name.lower()):
        lookup: FrameOrLazy = build_frame(data=lookup_data)
    if isinstance(frame, nw.LazyFrame):
        lookup = lookup.lazy()

    long_frame = frame.unpivot(
        on=value_columns,
        index=list(RESHAPE_INDEX),
        variable_name="column_key",
        value_name="value",
    )
    # `long_frame`/`lookup` are matched to the same laziness just above, but
    # narwhals' `.join` signature binds a single concrete frame type, so it
    # can't statically see that. This is the same real-but-unexpressible
    # runtime invariant as `concat`'s type-var mismatch (see
    # core._backend.concat).
    joined = long_frame.join(lookup, on="column_key", how="left")  # type: ignore[arg-type]
    long_frame = joined.drop("column_key")
    return finalize_as(frame=long_frame, dataframe_type=dataframe_type)


@overload
def convert_long_format_to_wide_format(
    *, long: NativeDataFrame, dataframe_type: None = None
) -> NativeDataFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def convert_long_format_to_wide_format(
    *, long: NativeDataFrame, dataframe_type: Literal["pandas"]
) -> pd.DataFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def convert_long_format_to_wide_format(
    *, long: NativeDataFrame, dataframe_type: Literal["pyarrow_table"]
) -> pa.Table:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def convert_long_format_to_wide_format(
    *, long: NativeDataFrame, dataframe_type: Literal["polars_dataframe"]
) -> pl.DataFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def convert_long_format_to_wide_format(
    *, long: NativeDataFrame, dataframe_type: Literal["polars_lazyframe"]
) -> pl.LazyFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
def convert_long_format_to_wide_format(
    *, long: NativeDataFrame, dataframe_type: DataFrameType | None = None
) -> NativeDataFrame:
    """Convert an already-built long-format frame to wide format.

    This function is self-contained. It builds each row's wide column name
    with `_with_column_key`, then pivots with
    `call_report.core._backend.pivot`. Pivoting requires the
    `(UNINUM, period, column_key)` grain to be unique, and `pivot` enforces
    that and raises `ReshapeError` if it is not, so no separate check is
    needed here.

    Parameters
    ----------
    long : NativeDataFrame
        A long-format frame, e.g. from `FCACallReport.to_long_format`.
    dataframe_type : {"pandas", "pyarrow_table", "polars_lazyframe", \
"polars_dataframe"}, optional
        The dataframe type to convert the result to as a final step.
        Leave this ``None`` (the default) to get back whatever backend
        `call_report.config.get_config` currently has configured.

    Returns
    -------
    NativeDataFrame
        The wide-format frame (see `FCACallReport.to_wide_format`), of
        the configured backend, or of `dataframe_type` if it was
        supplied.

    Raises
    ------
    ReshapeError
        If `(UNINUM, period, schedule, code_column, code_value,
        variable_name)` is not a unique grain.

    Examples
    --------
    >>> from call_report.fca.transport import PackagedArchiveTransport
    >>> from call_report.fca.report import FCACallReport
    >>> report = FCACallReport(
    ...     start="2026-03-31",
    ...     end="2026-03-31",
    ...     transport=PackagedArchiveTransport(),
    ... )
    >>> long = report.to_long_format(schedules=["RC", "RCB"])
    >>> wide = convert_long_format_to_wide_format(long=long)
    >>> "RCB__INV_CODE_15__BKVAL" in wide.columns
    True
    """
    frame = nw.from_native(long)
    keyed = _with_column_key(frame)
    wide = pivot(
        frame=keyed, on="column_key", index=list(RESHAPE_INDEX), values="value"
    )
    return finalize_as(frame=wide, dataframe_type=dataframe_type)
