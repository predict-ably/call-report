"""Private wide-, long-, and code-grain reshaping logic behind ``FCACallReport``.

This module is private and covers exactly what `to_wide_format`,
`to_long_format`, `to_code_grain_format`, and the standalone
`convert_wide_format_to_long_format`,
`convert_long_format_to_wide_format`, and
`convert_long_format_to_code_grain_format` functions need. It melts each
already-loaded schedule's frame into a long-shaped intermediate, tagging a
code-bearing schedule's code column distinctly from a plain variable, and
stacks every schedule together. From there it either computes each row's
wide column name and pivots, tags single and coded rows directly as the
long-format result, or keeps the code as a row key and pivots only the
schedule and variable into columns.

Every function here accepts and returns `FrameOrLazy`. If a schedule's
frame is already a `polars.LazyFrame` (``lazy=True`` configured), the melt,
concat, and column-key steps all stay lazy too. Each entry point collects
exactly once: `to_wide_format` and `to_code_grain_format` at `pivot`, since
a pivoted result's schema depends on data values, and `to_long_format` at
its own grain-uniqueness check, since checking the data is likewise not a
lazy-safe operation.
"""

from __future__ import annotations

import operator
from collections.abc import Sequence
from dataclasses import dataclass
from functools import reduce
from typing import TYPE_CHECKING, Any, Literal, cast, overload

import narwhals as nw

from call_report.config import config_context
from call_report.core._backend import (
    DataFrameType,
    FrameOrLazy,
    assert_unique_grain,
    build_frame,
    concat,
    date_dtype,
    finalize_as,
    is_in_null_safe,
    pivot,
)
from call_report.exceptions import ReshapeError
from call_report.fca.layout import FIXED_IDENTIFIER_COLUMNS

if TYPE_CHECKING:
    import pandas
    import polars
    import pyarrow

    from call_report.core._backend import NativeDataFrame
    from call_report.fca._domain_datasets import (
        DerivedOperation,
        DomainDatasetDerived,
        DomainDatasetSource,
    )

RESHAPE_INDEX: tuple[str, ...] = ("UNINUM", "period")
"""tuple[str, ...]: The grain every wide- and long-format row is keyed by."""

_EXCLUDED_FROM_MELT = frozenset({*FIXED_IDENTIFIER_COLUMNS, "period"})


def reshape_index_dtypes() -> dict[str, nw.dtypes.DType]:
    """Return the concrete dtype each `RESHAPE_INDEX` column is held as.

    A function rather than a constant, because `date_dtype` reads the
    configured backend at call time. Every grain column needs an entry:
    `melt_schedule_frame` casts each one whose dtype narwhals could not
    infer, and a grain column with no entry here would silently keep the
    `narwhals.Unknown` dtype that breaks a later concat.

    Returns
    -------
    dict[str, narwhals.dtypes.DType]
        One entry per `RESHAPE_INDEX` column.

    Examples
    --------
    >>> from call_report.fca._reshape import reshape_index_dtypes
    >>> sorted(reshape_index_dtypes())
    ['UNINUM', 'period']
    """
    return {"UNINUM": nw.Int64(), "period": date_dtype()}


_LONG_FORMAT_GRAIN: tuple[str, ...] = (
    *RESHAPE_INDEX,
    "schedule",
    "code_column",
    "code_value",
    "variable_name",
)
"""tuple[str, ...]: The grain `to_long_format` guarantees is unique."""

LONG_FORMAT_COLUMNS: tuple[str, ...] = (*_LONG_FORMAT_GRAIN, "value", "is_multiple")
"""tuple[str, ...]: The column order every long-format frame is returned in.

Both routes to a long frame select this before returning, so
`FCACallReport.to_long_format` and `convert_wide_format_to_long_format`
agree on layout and not merely on content. Built from `_LONG_FORMAT_GRAIN`
so the guaranteed order cannot drift from the guaranteed grain. The grain
comes first, then the measure, then the flag describing it.
"""

CODE_GRAIN_INDEX: tuple[str, ...] = (*RESHAPE_INDEX, "code_column", "code_value")
"""tuple[str, ...]: The grain every code-grain row is keyed by.

`RESHAPE_INDEX` plus the code a row belongs to. The schedule is not part
of this grain. It is folded into each measure column's name instead, so
two schedules reporting at the same code contribute columns to one row
rather than a row each.
"""


@dataclass(frozen=True, kw_only=True)
class ScheduleInputs:
    """One schedule's loaded frame and the layout facts needed to melt it.

    `FCACallReport._load_reshape_inputs` produces one of these per
    schedule. Carrying the three together means a caller cannot pair a
    frame with another schedule's code column, which three dicts keyed
    in parallel left possible.

    Attributes
    ----------
    frame : narwhals.DataFrame or narwhals.LazyFrame
        The schedule's already period-stacked frame. Lazy if the
        configured backend loaded it lazily.
    code_column : str, optional
        The schedule's code column name (`FCALayout.multi_columns[0]`),
        or ``None`` for a schedule with no code column.
    trailing_columns : tuple[str, ...]
        The schedule's trailing single-occurrence columns
        (`FCALayout.trailing_columns`), empty for a schedule with none.

    Examples
    --------
    >>> from call_report.core._backend import build_frame
    >>> from call_report.fca._reshape import ScheduleInputs
    >>> inputs = ScheduleInputs(
    ...     frame=build_frame(data={"UNINUM": [1], "INV_CODE": [15]}),
    ...     code_column="INV_CODE",
    ...     trailing_columns=(),
    ... )
    >>> inputs.code_column
    'INV_CODE'
    """  # numpydoc ignore=PR01

    frame: FrameOrLazy
    code_column: str | None
    trailing_columns: tuple[str, ...]


_LOOKUP_SCHEMA: dict[str, nw.dtypes.DType] = {
    "column_key": nw.String(),
    "schedule": nw.String(),
    "code_column": nw.String(),
    "code_value": nw.Float64(),
    "is_multiple": nw.Boolean(),
    "variable_name": nw.String(),
}
"""dict[str, narwhals.dtypes.DType]: Declared dtypes for the wide-to-long lookup.

`convert_wide_format_to_long_format` parses the wide frame's column names
into a small lookup frame. When no column is coded, `code_column` and
`code_value` are entirely null, leaving nothing to infer a dtype from, so
they are declared here instead. Inference gives each backend a different
answer: pandas reads an all-null `code_value` as String, polars as
Unknown, and pyarrow builds a null-typed column that then raises
``pyarrow.lib.ArrowInvalid`` when the lookup is joined.
"""


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

    Every `RESHAPE_INDEX` column is cast to a concrete dtype up front if
    `Unknown`, which happens when a schedule has zero rows across every
    requested period (see `_cast_unknown_dtype`). None of them is touched
    by `_cast_numeric_to_float64`'s later `"value"` and `"code_value"`
    calls, but all carry the same cross-piece concat risk, and an
    `Unknown` `period` also fails a decoding join with `ArrowInvalid`
    under pyarrow before it ever reaches a concat.
    `reshape_index_dtypes` names the target for each.

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
    for column, target in reshape_index_dtypes().items():
        frame = _cast_unknown_dtype(frame, column=column, target=target)
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


def _plain_column_key() -> nw.Expr:
    """Return the ``{schedule}__{variable_name}`` column-name expression.

    The naming a variable gets when the code it belongs to is not folded
    into its column name. That is the case for a non-coded field in the
    wide format, and for every field in the code grain, which keeps the
    code as a row key instead. Both build the name from this one
    expression, so the two cannot drift apart.

    Built with `narwhals.concat_str` rather than ``+``, because pyarrow's
    `Series.__add__` has no string-concatenation kernel, only numeric
    addition.

    Returns
    -------
    narwhals.Expr
        An expression over `schedule` and `variable_name`.
    """
    return nw.concat_str([nw.col("schedule"), nw.col("variable_name")], separator="__")


def _with_plain_column_key(frame: FrameOrLazy) -> FrameOrLazy:
    """Compute the ``{schedule}__{variable_name}`` column name for every row.

    The code-grain counterpart to `_with_column_key`. The code stays a row
    key there, so it never enters the column name and there is no coded
    branch to take.

    Parameters
    ----------
    frame : narwhals.DataFrame or narwhals.LazyFrame
        A melted frame, with `schedule` and `variable_name` columns.

    Returns
    -------
    narwhals.DataFrame or narwhals.LazyFrame
        `frame` with an added `column_key` string column.
    """
    return frame.with_columns(_plain_column_key().alias("column_key"))


def _with_column_key(frame: FrameOrLazy) -> FrameOrLazy:
    """Compute the wide-format column name for every melted row.

    A row with no code column keys as ``{schedule}__{variable_name}``. A
    row with a code column keys as
    ``{schedule}__{code_column}_{code_value}__{variable_name}``.

    The plain half of the key comes from `_plain_column_key`, so wide
    format and the code grain name a variable the same way.
    `code_value`'s cast to a string goes through `fill_null` first,
    because casting a `Float64`-with-null column straight to `Int64`
    raises on the pandas backend, and that is the normal shape here once
    code and non-code schedules are concatenated together.

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
        return _with_plain_column_key(frame)

    code_value_text = nw.col("code_value").fill_null(0).cast(nw.Int64).cast(nw.String)
    plain_key = _plain_column_key()
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
    inputs: dict[str, ScheduleInputs],
) -> nw.DataFrame[Any]:
    """Build the wide-format frame from a set of already-loaded schedules.

    Melts and tags every schedule via `melt_schedule_frame`, concatenates
    them, computes each row's `column_key`, then pivots on it. A schedule
    with no code column gets null `code_column` and `code_value` once
    unioned against schedules that have them. Everything before `pivot`
    stays lazy if `inputs`' frames are lazy. `pivot` is the one step that
    must collect, since a pivoted result's schema depends on
    `column_key`'s distinct values.

    Parameters
    ----------
    inputs : dict[str, ScheduleInputs]
        Each schedule's loaded frame and layout facts, keyed by schedule
        root name.

    Returns
    -------
    narwhals.DataFrame
        One row per `RESHAPE_INDEX` grain, one column per `column_key`.
    """
    melted = [
        melt_schedule_frame(
            frame=item.frame,
            schedule=schedule,
            code_column=item.code_column,
            trailing_columns=item.trailing_columns,
        )
        for schedule, item in inputs.items()
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
    inputs: dict[str, ScheduleInputs],
) -> nw.DataFrame[Any]:
    """Build the long-format frame from a set of already-loaded schedules.

    Melts and tags every schedule via `melt_schedule_frame`, the same
    per-schedule step `to_wide_format` uses, concatenates them, and adds
    `is_multiple`. Unlike `to_wide_format`, there is no pivot, so the melt,
    concat, and flag steps all stay lazy if `inputs`' frames are lazy. The
    one place this collects is the final `assert_unique_grain` call, which
    verifies `_LONG_FORMAT_GRAIN` is actually unique in the data. That is
    not a lazy-safe question, so it is a single unavoidable collect,
    matching `to_wide_format`'s own collect at `pivot`.

    Parameters
    ----------
    inputs : dict[str, ScheduleInputs]
        Each schedule's loaded frame and layout facts, keyed by schedule
        root name.

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
            frame=item.frame,
            schedule=schedule,
            code_column=item.code_column,
            trailing_columns=item.trailing_columns,
        )
        for schedule, item in inputs.items()
    ]
    combined = concat(frames=melted, how="union")
    combined = _with_is_multiple_flag(combined)
    checked = assert_unique_grain(frame=combined, columns=_LONG_FORMAT_GRAIN)
    return checked.select(*LONG_FORMAT_COLUMNS)


def to_code_grain_format(
    *,
    inputs: dict[str, ScheduleInputs],
) -> nw.DataFrame[Any]:
    """Build the code-grain frame from a set of already-loaded schedules.

    Melts and tags every schedule via `melt_schedule_frame`, the same
    per-schedule step `to_wide_format` and `to_long_format` use, then
    keeps the code as a row key rather than folding it into a column name.
    The result has one row per `CODE_GRAIN_INDEX` grain and one
    ``{schedule}__{variable}`` column per variable, so two schedules
    reporting at the same code contribute columns to the same row.

    Only rows that belong to a code survive. A ``"single"``-scenario
    schedule has no code at all, and a ``single_multiple_single``
    schedule's `trailing_columns` are single-occurrence fields the loader
    repeats on every code-row, so both are institution-level rather than
    code-level and are dropped. Both melt with a null `code_column`, so
    one filter removes them.

    Everything before `pivot` stays lazy if `inputs`' frames are lazy.
    `pivot` is the one step that must collect, since a pivoted result's
    schema depends on `column_key`'s distinct values, and it is also what
    enforces that `CODE_GRAIN_INDEX` plus `column_key` is a unique grain.

    A schedule can declare a code column and still contribute no coded
    rows, which pivots to a frame of `CODE_GRAIN_INDEX` and nothing else.
    `assert_pivot_has_measurements` rejects that, so an empty result
    cannot be mistaken for a narrow one.

    Parameters
    ----------
    inputs : dict[str, ScheduleInputs]
        Each schedule's loaded frame and layout facts, keyed by schedule
        root name.

    Returns
    -------
    narwhals.DataFrame
        One row per `CODE_GRAIN_INDEX` grain, one column per
        ``{schedule}__{variable}``.

    Raises
    ------
    ReshapeError
        If no schedule in `inputs` has a code column, if every coded
        schedule resolved to zero rows for the requested periods, or if
        `CODE_GRAIN_INDEX` plus the column name is not a unique grain.
    """
    melted = [
        melt_schedule_frame(
            frame=item.frame,
            schedule=schedule,
            code_column=item.code_column,
            trailing_columns=item.trailing_columns,
        )
        for schedule, item in inputs.items()
    ]
    combined = concat(frames=melted, how="union")
    # `collect_schema()` rather than `.columns`, which emits a
    # `PerformanceWarning` on a `LazyFrame`.
    if "code_column" not in combined.collect_schema():
        raise ReshapeError(
            "None of the requested schedules reports a code, so there is no "
            f"code grain to build: {sorted(inputs)}."
        )
    coded = _with_plain_column_key(combined.filter(~nw.col("code_column").is_null()))
    code_grain = pivot(
        frame=coded, on="column_key", index=list(CODE_GRAIN_INDEX), values="value"
    )
    assert_pivot_has_measurements(
        pivoted=code_grain, message=NO_CODE_GRAIN_MEASUREMENTS
    )
    return code_grain


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
) -> pandas.DataFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def convert_wide_format_to_long_format(
    *, wide: NativeDataFrame, dataframe_type: Literal["pyarrow_table"]
) -> pyarrow.Table:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def convert_wide_format_to_long_format(
    *, wide: NativeDataFrame, dataframe_type: Literal["polars_dataframe"]
) -> polars.DataFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def convert_wide_format_to_long_format(
    *, wide: NativeDataFrame, dataframe_type: Literal["polars_lazyframe"]
) -> polars.LazyFrame:  # numpydoc ignore=GL08
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

    Columns are always returned in the order ``UNINUM``, ``period``,
    ``schedule``, ``code_column``, ``code_value``, ``variable_name``,
    ``value``, ``is_multiple``. That order is part of the contract, so a
    positional read of this frame matches one built by the other route.

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
    >>> list(long.columns)
    ['UNINUM', 'period', 'schedule', 'code_column', 'code_value', \
'variable_name', 'value', 'is_multiple']
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
        lookup: FrameOrLazy = build_frame(data=lookup_data, schema=_LOOKUP_SCHEMA)
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
    long_frame = joined.select(*LONG_FORMAT_COLUMNS)
    return finalize_as(frame=long_frame, dataframe_type=dataframe_type)


@overload
def convert_long_format_to_wide_format(
    *, long: NativeDataFrame, dataframe_type: None = None
) -> NativeDataFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def convert_long_format_to_wide_format(
    *, long: NativeDataFrame, dataframe_type: Literal["pandas"]
) -> pandas.DataFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def convert_long_format_to_wide_format(
    *, long: NativeDataFrame, dataframe_type: Literal["pyarrow_table"]
) -> pyarrow.Table:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def convert_long_format_to_wide_format(
    *, long: NativeDataFrame, dataframe_type: Literal["polars_dataframe"]
) -> polars.DataFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def convert_long_format_to_wide_format(
    *, long: NativeDataFrame, dataframe_type: Literal["polars_lazyframe"]
) -> polars.LazyFrame:  # numpydoc ignore=GL08
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


@overload
def convert_long_format_to_code_grain_format(
    *, long: NativeDataFrame, dataframe_type: None = None
) -> NativeDataFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def convert_long_format_to_code_grain_format(
    *, long: NativeDataFrame, dataframe_type: Literal["pandas"]
) -> pandas.DataFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def convert_long_format_to_code_grain_format(
    *, long: NativeDataFrame, dataframe_type: Literal["pyarrow_table"]
) -> pyarrow.Table:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def convert_long_format_to_code_grain_format(
    *, long: NativeDataFrame, dataframe_type: Literal["polars_dataframe"]
) -> polars.DataFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def convert_long_format_to_code_grain_format(
    *, long: NativeDataFrame, dataframe_type: Literal["polars_lazyframe"]
) -> polars.LazyFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
def convert_long_format_to_code_grain_format(
    *, long: NativeDataFrame, dataframe_type: DataFrameType | None = None
) -> NativeDataFrame:
    """Convert an already-built long-format frame to the code grain.

    This function is self-contained. The long format already carries
    `code_column` and `code_value` as first-class columns, so the code
    grain is the pivot that keeps them as row keys instead of folding
    them into the column name. It needs no `FCACallReport` instance,
    layout lookups, or other external metadata.

    Only multiple-occurrence rows survive. A single-occurrence variable
    has no code to key on, so it is dropped rather than carried with null
    code keys. Pivoting requires the `CODE_GRAIN_INDEX` plus
    ``{schedule}__{variable}`` grain to be unique, and `pivot` enforces
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
        The code-grain frame (see
        `FCACallReport.to_code_grain_format`), of the configured backend,
        or of `dataframe_type` if it was supplied.

    Raises
    ------
    ReshapeError
        If `long` has no multiple-occurrence rows, or if
        ``(UNINUM, period, code_column, code_value, schedule,
        variable_name)`` is not a unique grain.

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
    >>> code_grain = convert_long_format_to_code_grain_format(long=long)
    >>> list(code_grain.columns)[:5]
    ['UNINUM', 'period', 'code_column', 'code_value', 'RCB__BKVAL']
    """
    frame = nw.from_native(long)
    coded = _with_plain_column_key(frame.filter(~nw.col("code_column").is_null()))
    code_grain = pivot(
        frame=coded, on="column_key", index=list(CODE_GRAIN_INDEX), values="value"
    )
    assert_pivot_has_measurements(
        pivoted=code_grain, message=NO_MULTIPLE_OCCURRENCE_ROWS
    )
    return finalize_as(frame=code_grain, dataframe_type=dataframe_type)


_DOMAIN_LOOKUP_SCHEMA: dict[str, nw.dtypes.DType] = {
    "variable_name": nw.String(),
    "output_column": nw.String(),
    "mapped_code": nw.Float64(),
}
"""dict[str, narwhals.dtypes.DType]: Declared dtypes for the decoding lookup.

`mapped_code` is entirely null for a code-bearing source, which reports
its code in the data rather than in its variable names. That leaves
nothing to infer a dtype from, and inference gives each backend a
different answer, so the dtypes are declared here instead. This is the
same hazard `_LOOKUP_SCHEMA` documents for the wide-to-long lookup.
"""


def apply_domain_dataset_decoding(
    *, frame: FrameOrLazy, source: DomainDatasetSource, code_column: str
) -> FrameOrLazy:
    """Rewrite one melted schedule's rows into a domain dataset's own terms.

    Replaces each row's `variable_name` with the curated output column the
    dataset declares for it, and sets `code_column` and `code_value` to
    the curated code the row belongs to. A variable the dataset does not
    declare is dropped.

    Both kinds of source end in the same shape. A code-bearing source
    already carries `code_value` from the melt and only needs its
    `variable_name` rewritten. A source that encodes its breakdown in
    variable names instead has no `code_value` at all until this supplies
    one from the declaration.

    The rewrite is a join against a small lookup frame rather than a pass
    over rows, so it is backend-agnostic and stays lazy. The join is inner,
    which is what drops an undeclared variable.

    Parameters
    ----------
    frame : narwhals.DataFrame or narwhals.LazyFrame
        One schedule's melted frame, i.e. `melt_schedule_frame`'s result.
    source : DomainDatasetSource
        The dataset's declaration for the schedule `frame` came from.
    code_column : str
        The curated name every decoded row's ``code_column`` is set to.

    Returns
    -------
    narwhals.DataFrame or narwhals.LazyFrame
        `frame` with `variable_name`, `code_column`, and `code_value`
        expressed in the dataset's terms. Lazy if `frame` was lazy.
    """
    variables = sorted(source.columns)
    declarations = [source.columns[name] for name in variables]
    with config_context(dataframe_backend=frame.implementation.name.lower()):
        if source.code_column is None:
            codes: list[float | None] = [
                float(cast("int", item.code)) for item in declarations
            ]
            lookup: FrameOrLazy = build_frame(
                data={
                    "variable_name": variables,
                    "output_column": [item.column for item in declarations],
                    "mapped_code": codes,
                },
                schema=_DOMAIN_LOOKUP_SCHEMA,
            )
        else:
            lookup = build_frame(
                data={
                    "variable_name": variables,
                    "output_column": [item.column for item in declarations],
                },
                schema={
                    name: dtype
                    for name, dtype in _DOMAIN_LOOKUP_SCHEMA.items()
                    if name != "mapped_code"
                },
            )
    if isinstance(frame, nw.LazyFrame):
        lookup = lookup.lazy()

    # `frame` and `lookup` are matched to the same laziness just above, but
    # narwhals' `.join` signature binds a single concrete frame type (see
    # convert_wide_format_to_long_format for the same invariant).
    decoded = frame.join(lookup, on="variable_name", how="inner")  # type: ignore[arg-type]
    if source.code_column is None:
        decoded = decoded.with_columns(nw.col("mapped_code").alias("code_value"))
        decoded = decoded.drop("mapped_code")
    return decoded.with_columns(
        nw.col("output_column").alias("variable_name"),
        nw.lit(code_column).alias("code_column"),
    ).drop("output_column")


def _derived_expression(
    *, components: Sequence[str], operation: DerivedOperation
) -> nw.Expr:
    """Build the expression for one derived column.

    A null component counts as zero, but only when at least one component
    is present. A row where every component is null gets a null result
    rather than a zero, because those two say different things: one is a
    measure of zero, the other is a measure the source did not report.

    Parameters
    ----------
    components : Sequence[str]
        The output columns this is computed from, in order.
    operation : {"sum", "difference"}
        How `components` combine. A difference subtracts every later
        component from the first.

    Returns
    -------
    narwhals.Expr
        The derived column's expression.
    """
    filled = [nw.col(name).fill_null(0) for name in components]
    first, *rest = filled
    if operation == "sum":
        combined = reduce(operator.add, rest, first)
    else:
        combined = reduce(operator.sub, rest, first)
    any_present = reduce(operator.or_, (~nw.col(name).is_null() for name in components))
    return (
        nw.when(any_present).then(combined).otherwise(nw.lit(None, dtype=nw.Float64()))
    )


def add_derived_columns(
    *, frame: nw.DataFrame[Any], derived: Sequence[DomainDatasetDerived]
) -> nw.DataFrame[Any]:
    """Add a domain dataset's derived columns to its pivoted frame.

    Runs after the pivot, since each derived column spans several of the
    pivot's output columns. A declaration whose components are not all
    present is skipped, which happens when the requested range has no
    period for the schedule that supplies them.

    A column the source already reports is never overwritten. The derived
    value fills only the rows where the reported one is null. RI-E reports
    net charge-offs directly for two portfolios and leaves the rest to be
    computed, so both meanings live in one column.

    Parameters
    ----------
    frame : narwhals.DataFrame
        The pivoted frame, one column per curated output column.
    derived : Sequence[DomainDatasetDerived]
        The dataset's derived column declarations, in order.

    Returns
    -------
    narwhals.DataFrame
        `frame` with each computable derived column added or filled in.
    """
    available = set(frame.collect_schema().names())
    for item in derived:
        if not set(item.components) <= available:
            continue
        expression = _derived_expression(
            components=item.components, operation=item.operation
        )
        if item.column in available:
            expression = (
                nw.when(nw.col(item.column).is_null())
                .then(expression)
                .otherwise(nw.col(item.column))
            )
        frame = frame.with_columns(expression.alias(item.column))
        available.add(item.column)
    return frame


def exclude_reported_totals(
    *, frame: FrameOrLazy, total_codes: frozenset[int]
) -> FrameOrLazy:
    """Drop every row whose code_value is one of a dataset's reported totals.

    Used by `FCACallReport._to_domain_dataset` to implement
    ``include_totals=False``. A null `code_value` is never one of the
    source's own declared totals, so it must survive this on every
    backend. `call_report.core._backend.is_in_null_safe` is what
    guarantees that.

    Parameters
    ----------
    frame : narwhals.DataFrame or narwhals.LazyFrame
        A decoded, stacked frame with a `code_value` column.
    total_codes : frozenset[int]
        The codes to drop, from `DomainDataset.total_codes`.

    Returns
    -------
    narwhals.DataFrame or narwhals.LazyFrame
        `frame` with every row whose `code_value` is in `total_codes`
        removed. Lazy if `frame` was lazy.
    """
    is_total = is_in_null_safe(column="code_value", values=sorted(total_codes))
    return frame.filter(~is_total)


NO_DOMAIN_DATASET_MEASUREMENTS = (
    "The requested schedules contributed no measurement columns, so there is "
    "no domain dataset to build. This happens when every contributing "
    "schedule resolved to zero rows for the requested periods."
)
"""str: `assert_pivot_has_measurements` message for a curated domain dataset."""

NO_CODE_GRAIN_MEASUREMENTS = (
    "The requested schedules contributed no coded measurement columns, so "
    "there is no code grain to build. This happens when every schedule that "
    "declares a code column resolved to zero rows for the requested periods."
)
"""str: `assert_pivot_has_measurements` message for `to_code_grain_format`."""

NO_MULTIPLE_OCCURRENCE_ROWS = (
    "`long` has no multiple-occurrence rows (every row's code_column is "
    "null), so there is no code grain to build."
)
"""str: `assert_pivot_has_measurements` message for the long-format conversion."""


def assert_pivot_has_measurements(*, pivoted: nw.DataFrame[Any], message: str) -> None:
    """Raise if a pivoted frame carries no columns beyond `CODE_GRAIN_INDEX`.

    A pivot on an entirely empty input still produces the four
    `CODE_GRAIN_INDEX` columns and nothing else. Returning that silently
    would look like a real, if narrow, result rather than the absence of
    any real one.

    Every code-grain route pivots to the same shape and so needs the same
    check. `message` is what differs between them, since each reaches an
    empty pivot for its own reason.

    Parameters
    ----------
    pivoted : narwhals.DataFrame
        The frame `core._backend.pivot` returned.
    message : str
        The `ReshapeError` message to raise, describing why this caller's
        pivot came back with no measurements.

    Raises
    ------
    ReshapeError
        If `pivoted` has no column beyond `CODE_GRAIN_INDEX`, for example
        because every contributing schedule resolved to zero rows for
        the requested periods.
    """
    if len(pivoted.columns) == len(CODE_GRAIN_INDEX):
        raise ReshapeError(message)


def pivot_domain_dataset_wide(*, frame: nw.DataFrame[Any]) -> nw.DataFrame[Any]:
    """Pivot a curated domain dataset frame wider, one column per code and measure.

    Takes the code-grain-shaped frame `FCACallReport._to_domain_dataset`
    already built (one row per `CODE_GRAIN_INDEX` grain, one column per
    curated measure) and pivots it again so each row is keyed by
    `RESHAPE_INDEX` alone, with one output column per
    ``{code_value}__{measure}`` combination, e.g. ``100__accruing``.
    `code_column` is dropped rather than folded into the name, since a
    domain dataset declares exactly one and it therefore disambiguates
    nothing once every column already names its own measure.

    `code_value`'s cast to a string goes through `fill_null` first,
    because casting a `Float64`-with-null column straight to `Int64`
    raises on the pandas backend. A dataset's own coded rows are never
    null in practice, so this only guards a row that reached here some
    other way.

    Parameters
    ----------
    frame : narwhals.DataFrame
        The narrow, code-grain-shaped curated frame.

    Returns
    -------
    narwhals.DataFrame
        One row per `RESHAPE_INDEX` grain, one column per
        ``{code_value}__{measure}`` combination.

    Raises
    ------
    ReshapeError
        If `RESHAPE_INDEX` plus ``{code_value}__{measure}`` is not a
        unique grain.
    """
    measures = [column for column in frame.columns if column not in CODE_GRAIN_INDEX]
    melted = frame.unpivot(
        on=measures,
        index=list(CODE_GRAIN_INDEX),
        variable_name="measure",
        value_name="value",
    )
    code_value_text = nw.col("code_value").fill_null(0).cast(nw.Int64).cast(nw.String)
    melted = melted.with_columns(
        nw.concat_str([code_value_text, nw.col("measure")], separator="__").alias(
            "column_key"
        )
    )
    return pivot(
        frame=melted, on="column_key", index=list(RESHAPE_INDEX), values="value"
    )
