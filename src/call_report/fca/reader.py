"""Parse FCA schedule data files using their layout.

The parsing strategy here is data-driven rather than positional. Real FCA
releases contain rows with a variable number of multiple-occurrence groups
per institution, because banks and associations report different code sets.
Rather than trusting a fixed count of codes per row, each occurrence group
is identified by the code value embedded in the row itself, so ragged rows
and line-wrapped records both parse correctly without needing to know how
many groups "should" be present.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, overload

import narwhals as nw

from call_report.core._backend import DataFrameType, build_frame, finalize_as
from call_report.exceptions import LayoutParseError
from call_report.fca.layout import ENCODING, FCALayout, infer_field_dtype

if TYPE_CHECKING:
    import pandas as pd
    import polars as pl
    import pyarrow as pa

    from call_report.core._backend import NativeDataFrame


@overload
def read_schedule_file(
    *, data_path: Path, layout: FCALayout, dataframe_type: None = None
) -> NativeDataFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def read_schedule_file(
    *, data_path: Path, layout: FCALayout, dataframe_type: Literal["pandas"]
) -> pd.DataFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def read_schedule_file(
    *, data_path: Path, layout: FCALayout, dataframe_type: Literal["pyarrow_table"]
) -> pa.Table:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def read_schedule_file(
    *, data_path: Path, layout: FCALayout, dataframe_type: Literal["polars_dataframe"]
) -> pl.DataFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
@overload
def read_schedule_file(
    *, data_path: Path, layout: FCALayout, dataframe_type: Literal["polars_lazyframe"]
) -> pl.LazyFrame:  # numpydoc ignore=GL08
    ...  # pragma: no cover
def read_schedule_file(
    *, data_path: Path, layout: FCALayout, dataframe_type: DataFrameType | None = None
) -> NativeDataFrame:
    """Parse a schedule's data file into a tidy native dataframe.

    The row shape depends on `layout`'s scenario. A ``"single"`` layout
    produces one row per institution. The two multi-occurrence scenarios
    produce one row per (institution, reported code), with the code itself
    taken from the data rather than assumed from a fixed count.

    Parameters
    ----------
    data_path : pathlib.Path
        Path to the raw, comma-delimited data file.
    layout : FCALayout
        The layout describing `data_path`'s columns, as returned by
        `call_report.fca.layout.parse_layout`.
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
        A native dataframe of the configured backend, or of
        `dataframe_type` if it was supplied.

    Raises
    ------
    LayoutParseError
        If a row's field count cannot be reconciled with `layout`.
    """
    return finalize_as(
        frame=_read_schedule_frame(data_path=data_path, layout=layout),
        dataframe_type=dataframe_type,
    )


def _read_schedule_frame(*, data_path: Path, layout: FCALayout) -> nw.DataFrame[Any]:
    """Parse a schedule's data file into an eager narwhals frame.

    The non-finalizing counterpart to `read_schedule_file`, used internally
    by `call_report.fca.report.FCACallReport` so multiple periods' frames
    can be stacked before a single, final backend/laziness conversion.

    Parameters
    ----------
    data_path : pathlib.Path
        Path to the raw, comma-delimited data file.
    layout : FCALayout
        The layout describing `data_path`'s columns.

    Returns
    -------
    narwhals.DataFrame
        An eager narwhals-wrapped frame of the configured backend.

    Raises
    ------
    LayoutParseError
        If a row's field count cannot be reconciled with `layout`.
    """
    raw_text = data_path.read_bytes().decode(ENCODING)
    lines = [line for line in raw_text.splitlines() if line.strip() != ""]
    variables = layout.variables_as_dicts()
    type_by_name = {row["name"]: row["type"] for row in variables}
    schema = {
        row["name"]: infer_field_dtype(
            var_type=row["type"], decimal_position=row["decimal_position"]
        )
        for row in variables
    }

    if layout.scenario == "single":
        rows = _read_single(
            lines=lines, layout=layout, path=data_path, type_by_name=type_by_name
        )
        columns = layout.leading_columns
    elif layout.scenario == "single_multiple":
        rows = _read_single_multiple(
            lines=lines, layout=layout, path=data_path, type_by_name=type_by_name
        )
        columns = layout.leading_columns + layout.multi_columns
    else:
        rows = _read_single_multiple_single(
            lines=lines, layout=layout, path=data_path, type_by_name=type_by_name
        )
        columns = (
            layout.leading_columns + layout.multi_columns + layout.trailing_columns
        )

    data = {name: [row[name] for row in rows] for name in columns}
    return build_frame(data=data, schema=schema)


def _parse_csv_line(*, line: str) -> list[str]:
    """Split one comma-delimited line into fields, honoring quoted values.

    A thin wrapper around the standard library's `csv` module, matching
    FCA's convention of double-quoting alphanumeric values that may
    themselves contain commas (e.g. a street address).

    Parameters
    ----------
    line : str
        A single physical (or reconstructed logical) data line.

    Returns
    -------
    list[str]
        The line's raw, unquoted field values.
    """
    return next(csv.reader([line]))


def _cast(*, raw: str, var_type: str) -> Any:
    """Cast one raw field value per its layout-declared type.

    Values already contain a literal decimal point where relevant (FCA's
    ``DecimalPosition`` metadata is informational only, not a scale
    factor), so this only needs to distinguish int from float from text.

    Parameters
    ----------
    raw : str
        The raw, already-unquoted field value.
    var_type : {"Numeric", "Alphanum."}
        The variable's declared type, from its layout.

    Returns
    -------
    Any
        ``None`` for an empty value, an `int` or `float` for a numeric
        value, and otherwise the stripped string unchanged.
    """
    value = raw.strip()
    if value == "":
        return None
    if var_type == "Numeric":
        try:
            return float(value) if "." in value else int(value)
        except ValueError:
            return value
    return value


def _read_single(
    *, lines: list[str], layout: FCALayout, path: Path, type_by_name: dict[str, str]
) -> list[dict[str, Any]]:
    """Parse a ``"single"``-scenario file into one row dict per institution.

    A row with fewer fields than the layout describes is padded with nulls
    for the missing trailing columns. FCA regenerates layout files against
    the *current* schema, so an older quarter's data can be missing columns
    added in a later revision. A row with more fields than expected is a
    hard error, except for dangling trailing-comma artifacts, which are
    trimmed rather than rejected.

    Parameters
    ----------
    lines : list[str]
        The data file's non-blank physical lines.
    layout : FCALayout
        The ``"single"``-scenario layout describing these lines.
    path : pathlib.Path
        The source file's path, for error messages.
    type_by_name : dict[str, str]
        Each column's declared type, keyed by name.

    Returns
    -------
    list[dict[str, Any]]
        One row dict per line.

    Raises
    ------
    LayoutParseError
        If a line has more fields than `layout` describes (beyond dangling
        trailing-comma artifacts).
    """
    expected = len(layout.leading_columns)
    rows: list[dict[str, Any]] = []
    for line in lines:
        fields = _parse_csv_line(line=line)
        # Trailing dangling empty fields are a trailing-comma artifact,
        # not genuinely extra columns. They appear on several legacy
        # (2004-2008) single-scenario schedules such as RC1, RCH, RCI, RI,
        # and RIB, some with more than one trailing comma.
        while len(fields) > expected and fields[-1] == "":
            fields = fields[:-1]
        if len(fields) < expected:
            fields = [*fields, *([""] * (expected - len(fields)))]
        elif len(fields) > expected:
            raise LayoutParseError(
                f"{path}: expected {expected} fields for this 'single' scenario row, "
                f"got {len(fields)}."
            )
        rows.append(
            {
                name: _cast(raw=value, var_type=type_by_name[name])
                for name, value in zip(layout.leading_columns, fields, strict=True)
            }
        )
    return rows


def _read_single_multiple(
    *, lines: list[str], layout: FCALayout, path: Path, type_by_name: dict[str, str]
) -> list[dict[str, Any]]:
    """Parse a ``"single_multiple"``-scenario file into one row per (row, code).

    Each occurrence group's own first value (the code column) keys that
    group, rather than assuming a fixed number of groups per row. Real
    releases contain rows with fewer groups than others, such as an
    association that does not report every bank-only code.

    Parameters
    ----------
    lines : list[str]
        The data file's non-blank physical lines.
    layout : FCALayout
        The ``"single_multiple"``-scenario layout describing these lines.
    path : pathlib.Path
        The source file's path, for error messages.
    type_by_name : dict[str, str]
        Each column's declared type, keyed by name.

    Returns
    -------
    list[dict[str, Any]]
        One row dict per (institution row, reported code).

    Raises
    ------
    LayoutParseError
        If a row's remaining fields (after the leading columns) don't
        divide evenly by the multi-occurrence group width.
    """
    n_leading = len(layout.leading_columns)
    group_width = len(layout.multi_columns)
    rows: list[dict[str, Any]] = []
    for line in lines:
        fields = _parse_csv_line(line=line)
        remainder = fields[n_leading:]
        # A lone dangling empty field is a trailing-comma artifact, not a
        # partial group.
        if remainder and remainder[-1] == "" and len(remainder) % group_width != 0:
            remainder = remainder[:-1]
        if len(remainder) % group_width != 0:
            raise LayoutParseError(
                f"{path}: row has {len(remainder)} multi-occurrence fields, not a "
                f"multiple of the {group_width}-wide column group "
                f"{layout.multi_columns}."
            )
        leading_values = {
            name: _cast(raw=value, var_type=type_by_name[name])
            for name, value in zip(
                layout.leading_columns, fields[:n_leading], strict=True
            )
        }
        for start in range(0, len(remainder), group_width):
            group = remainder[start : start + group_width]
            group_values = {
                name: _cast(raw=value, var_type=type_by_name[name])
                for name, value in zip(layout.multi_columns, group, strict=True)
            }
            rows.append({**leading_values, **group_values})
    return rows


def _reconstruct_records(*, lines: list[str]) -> list[str]:
    """Reassemble physical lines into logical, comma-delimited records.

    FCA's ``single_multiple_single`` data files wrap one logical CSV row
    across an irregular number of physical lines. Every line except a
    record's last ends with a trailing comma, which acts as a continuation
    marker. Concatenating lines up to and including the first one that does
    *not* end with a comma reconstructs the original flat row, without
    needing to know in advance how many lines a record spans.

    Parameters
    ----------
    lines : list[str]
        The data file's non-blank physical lines.

    Returns
    -------
    list[str]
        One reconstructed, comma-delimited record per logical row.
    """
    records: list[str] = []
    buffer: list[str] = []
    for line in lines:
        buffer.append(line)
        if not line.endswith(","):
            records.append("".join(buffer))
            buffer = []
    return records


def _read_single_multiple_single(
    *, lines: list[str], layout: FCALayout, path: Path, type_by_name: dict[str, str]
) -> list[dict[str, Any]]:
    """Parse a ``single_multiple_single`` file into one row per (record, code).

    Reconstructs each logical record via `_reconstruct_records` before
    splitting it the same way `_read_single_multiple` splits a flat row.

    Parameters
    ----------
    lines : list[str]
        The data file's non-blank physical lines.
    layout : FCALayout
        The ``"single_multiple_single"``-scenario layout describing these
        lines.
    path : pathlib.Path
        The source file's path, for error messages.
    type_by_name : dict[str, str]
        Each column's declared type, keyed by name.

    Returns
    -------
    list[dict[str, Any]]
        One row dict per (logical record, reported code). The trailing
        columns' values are repeated across every code row within a
        record.

    Raises
    ------
    LayoutParseError
        If a reconstructed record's middle section doesn't divide evenly by
        the multi-occurrence group width.
    """
    n_leading = len(layout.leading_columns)
    n_trailing = len(layout.trailing_columns)
    group_width = len(layout.multi_columns)

    rows: list[dict[str, Any]] = []
    for record_text in _reconstruct_records(lines=lines):
        fields = _parse_csv_line(line=record_text)
        leading_fields = fields[:n_leading]
        trailing_fields = fields[len(fields) - n_trailing :] if n_trailing else []
        middle = (
            fields[n_leading : len(fields) - n_trailing]
            if n_trailing
            else fields[n_leading:]
        )

        if len(middle) % group_width != 0:
            raise LayoutParseError(
                f"{path}: reconstructed record has {len(middle)} multi-occurrence "
                f"fields, not a multiple of the {group_width}-wide column group "
                f"{layout.multi_columns}."
            )

        leading_values = {
            name: _cast(raw=value, var_type=type_by_name[name])
            for name, value in zip(layout.leading_columns, leading_fields, strict=True)
        }
        trailing_values = {
            name: _cast(raw=value, var_type=type_by_name[name])
            for name, value in zip(
                layout.trailing_columns, trailing_fields, strict=True
            )
        }
        for start in range(0, len(middle), group_width):
            group = middle[start : start + group_width]
            group_values = {
                name: _cast(raw=value, var_type=type_by_name[name])
                for name, value in zip(layout.multi_columns, group, strict=True)
            }
            rows.append({**leading_values, **group_values, **trailing_values})
    return rows
