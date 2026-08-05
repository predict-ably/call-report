"""Private wide-format reshaping logic behind ``FCACallReport.to_wide_format``.

Not a public module, and not yet the long-format API (a later, separate
piece of work) -- sized exactly for what wide-format stacking needs:
melt each already-loaded schedule's frame into a long-shaped
intermediate (tagging a code-bearing schedule's code column distinctly
from a plain variable), stack every schedule together, compute each row's
final wide column name, then pivot.
"""

from __future__ import annotations

from typing import Any

import narwhals as nw

from call_report.core._backend import concat, pivot
from call_report.fca.layout import FIXED_IDENTIFIER_COLUMNS

WIDE_FORMAT_INDEX: tuple[str, ...] = ("UNINUM", "period")
"""tuple[str, ...]: The grain every wide-format row is keyed by."""

_EXCLUDED_FROM_MELT = frozenset({*FIXED_IDENTIFIER_COLUMNS, "period"})


def _normalize_value_dtype(frame: nw.DataFrame[Any]) -> nw.DataFrame[Any]:
    """Cast a melted frame's numeric `value` column to a consistent Float64.

    Different fields carry different declared decimal positions, so two
    independently melted pieces -- two different schedules, or (for a
    ``single_multiple_single`` schedule) the coded-vs-trailing split
    within one schedule -- can each infer a different numeric `value`
    dtype (Int64 vs Float64). Concatenating pieces with mismatched
    numeric dtypes raises ``polars.exceptions.SchemaError`` depending on
    which piece happens to concatenate first; normalizing every numeric
    `value` column to Float64 up front removes that order-dependence. A
    non-numeric (``Alphanum.``) `value` column is left as-is.

    Parameters
    ----------
    frame : narwhals.DataFrame
        A melted frame with a `value` column.

    Returns
    -------
    narwhals.DataFrame
        `frame`, with `value` cast to Float64 if it was numeric.
    """
    if frame.schema["value"].is_numeric():
        return frame.with_columns(nw.col("value").cast(nw.Float64))
    return frame


def melt_schedule_frame(
    *,
    frame: nw.DataFrame[Any],
    schedule: str,
    code_column: str | None,
    trailing_columns: tuple[str, ...] = (),
) -> nw.DataFrame[Any]:
    """Melt one already-loaded, already-stacked schedule frame into long shape.

    Every column except `FIXED_IDENTIFIER_COLUMNS`, ``"period"``,
    `code_column`, and `trailing_columns` becomes a `variable_name`/
    `value` row pair, alongside `code_column`'s own values kept as a
    `code_value` column (rather than melted), so a code-bearing
    schedule's rows stay distinguishable by which code they belong to.

    A ``single_multiple_single``-scenario schedule's `trailing_columns`
    (e.g. RCR7's ``AvgDailyRWARegCap``) are single-occurrence fields that
    the loader nonetheless repeats identically on every one of a
    UNINUM/period's code-rows -- melting them the same way as a coded
    field would produce one redundant, identical-valued wide column per
    code instead of one clean column. They're melted separately here,
    from a frame first deduplicated down to one row per
    `WIDE_FORMAT_INDEX` grain, and tagged with no code column at all
    (relying on the same union-concat null-fill used for a schedule with
    no code column at all) so they key as plain
    ``{schedule}__{variable}`` columns.

    Parameters
    ----------
    frame : narwhals.DataFrame
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
    narwhals.DataFrame
        Columns ``UNINUM``, ``period``, ``schedule``, ``variable_name``,
        ``value``, plus ``code_column``/``code_value`` for any row that
        came from a coded field.
    """
    exclude = set(_EXCLUDED_FROM_MELT) | set(trailing_columns)
    if code_column is not None:
        exclude.add(code_column)
    melt_columns = [name for name in frame.columns if name not in exclude]

    index_columns = [*WIDE_FORMAT_INDEX]
    if code_column is not None:
        index_columns.append(code_column)

    melted = frame.unpivot(
        on=melt_columns,
        index=index_columns,
        variable_name="variable_name",
        value_name="value",
    )
    melted = _normalize_value_dtype(melted).with_columns(
        nw.lit(schedule).alias("schedule")
    )
    if code_column is not None:
        melted = melted.rename({code_column: "code_value"}).with_columns(
            nw.lit(code_column).alias("code_column")
        )

    if not trailing_columns:
        return melted

    trailing_melted = (
        frame.select(*WIDE_FORMAT_INDEX, *trailing_columns)
        .unique(subset=list(WIDE_FORMAT_INDEX))
        .unpivot(
            on=list(trailing_columns),
            index=list(WIDE_FORMAT_INDEX),
            variable_name="variable_name",
            value_name="value",
        )
    )
    trailing_melted = _normalize_value_dtype(trailing_melted).with_columns(
        nw.lit(schedule).alias("schedule")
    )
    return concat(frames=[melted, trailing_melted], how="union")


def _with_column_key(frame: nw.DataFrame[Any]) -> nw.DataFrame[Any]:
    """Compute the wide-format column name for every melted row.

    ``{schedule}__{variable_name}`` for a row with no code column;
    ``{schedule}__{code_column}_{code_value}__{variable_name}`` for one
    with a code column. Built with `narwhals.concat_str` rather than
    ``+`` -- pyarrow's `Series.__add__` has no string-concatenation
    kernel, only numeric addition. `code_value`'s cast to a string goes
    through `fill_null` first -- casting a `Float64`-with-null column
    (the normal shape here once code- and non-code schedules are
    concatenated together) straight to `Int64` raises on the pandas
    backend.

    Parameters
    ----------
    frame : narwhals.DataFrame
        The concatenated, melted frame, with `schedule`/`variable_name`
        columns and (for at least one row) `code_column`/`code_value`.

    Returns
    -------
    narwhals.DataFrame
        `frame` with an added `column_key` string column.
    """
    if "code_column" not in frame.columns:
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
    frames: dict[str, nw.DataFrame[Any]],
    code_columns: dict[str, str | None],
    trailing_columns: dict[str, tuple[str, ...]],
) -> nw.DataFrame[Any]:
    """Build the wide-format frame from a set of already-loaded schedules.

    Melts and tags every schedule via `melt_schedule_frame`, concatenates
    them (schedules with no code column naturally get null
    `code_column`/`code_value` once unioned against ones that have it),
    computes each row's `column_key`, then pivots on it.

    Parameters
    ----------
    frames : dict[str, narwhals.DataFrame]
        Each schedule's already-loaded, already-stacked frame, keyed by
        schedule root name.
    code_columns : dict[str, str or None]
        Each schedule's code column name, keyed the same way as `frames`
        -- ``None`` for a schedule with no code column.
    trailing_columns : dict[str, tuple[str, ...]]
        Each schedule's trailing single-occurrence columns, keyed the
        same way as `frames` -- an empty tuple for a schedule with none.

    Returns
    -------
    narwhals.DataFrame
        One row per `WIDE_FORMAT_INDEX` grain, one column per
        `column_key`.
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
        frame=combined, on="column_key", index=list(WIDE_FORMAT_INDEX), values="value"
    )
