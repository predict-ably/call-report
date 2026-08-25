"""Tests for the FCA data-file reader (call_report.fca.reader).

These exercise the self-describing, data-driven parsing strategy that
replaces the reference implementation's fixed ``n_codes``-per-row
assumption: occurrence groups are keyed by the code value embedded in the
data itself, so ragged rows (a real, confirmed 2024-Q1 FCA data quality
issue on bank-only schedules) and line-wrapped multi-line records both parse
correctly without knowing how many codes "should" be present.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import narwhals as nw
import pandas as pd
import polars as pl
import pyarrow as pa
import pytest

from call_report.core._backend import DataFrameType
from call_report.exceptions import LayoutParseError
from call_report.fca.layout import parse_layout
from call_report.fca.reader import read_schedule_file
from tests.fca.layouts import RC_LINES_7COL, RCB_LINES, RCR7_LINES
from tests.helpers import write_data, write_layout


def _rows(native_frame: Any) -> list[dict[str, Any]]:
    return nw.from_native(native_frame).rows(named=True)


def test_read_single_scenario(tmp_path: Path) -> None:
    """A 'single' schedule parses to one row per institution."""
    layout_path = write_layout(tmp_path, root="RC", variable_lines=RC_LINES_7COL)
    data_path = write_data(
        tmp_path,
        root="RC",
        year=2026,
        month=3,
        rows=["6,10,0,3,2026,610000,1000000", "6,20,0,3,2026,620000,2000000"],
    )
    layout = parse_layout(path=layout_path)
    result = read_schedule_file(data_path=data_path, layout=layout)
    rows = _rows(result)
    assert len(rows) == 2
    assert rows[0]["UNINUM"] == 610000
    assert rows[0]["TOTASSETS"] == 1000000


def test_read_single_multiple_scenario_with_ragged_rows(tmp_path: Path) -> None:
    """Occurrence groups are keyed by their own embedded code -- not a fixed count.

    Row 2 omits code 20 entirely (the real 2024-Q1 FCA data-quality pattern
    on bank-only schedules); it must simply be absent from the output rather
    than misaligning or raising.
    """
    layout_path = write_layout(tmp_path, root="RCB", variable_lines=RCB_LINES)
    data_path = write_data(
        tmp_path,
        root="RCB",
        year=2025,
        month=9,
        rows=[
            "6,10,0,9,2025,610000,10,100,1.50,20,200,2.50,30,300,3.50",
            "6,20,0,9,2025,620000,10,50,0.50,30,150,1.50",
        ],
    )
    layout = parse_layout(path=layout_path)
    result = read_schedule_file(data_path=data_path, layout=layout)
    rows = _rows(result)
    assert len(rows) == 5

    by_uninum_code = {(r["UNINUM"], r["INV_CODE"]): r for r in rows}
    assert (610000, 10) in by_uninum_code
    assert (610000, 20) in by_uninum_code
    assert (610000, 30) in by_uninum_code
    assert (620000, 10) in by_uninum_code
    assert (620000, 30) in by_uninum_code
    assert (
        620000,
        20,
    ) not in by_uninum_code  # ragged: never present for this institution

    row = by_uninum_code[(610000, 20)]
    assert row["AMOUNT"] == 200


def test_decimal_position_is_informational_not_a_scale_factor(tmp_path: Path) -> None:
    """DecimalPosition metadata does not trigger any manual value scaling.

    Ground truth confirms real FCA data already contains a literal decimal
    point (e.g. a dec.-pos.-2 field holding "0.0891"); dividing by 10**2
    on top of that would silently corrupt every such value.
    """
    layout_path = write_layout(tmp_path, root="RCB", variable_lines=RCB_LINES)
    data_path = write_data(
        tmp_path,
        root="RCB",
        year=2025,
        month=9,
        rows=["6,10,0,9,2025,610000,10,100,1.50"],
    )
    layout = parse_layout(path=layout_path)
    result = read_schedule_file(data_path=data_path, layout=layout)
    row = _rows(result)[0]
    assert row["AMOUNT2"] == pytest.approx(1.50)


def test_read_single_multiple_single_scenario_line_wrapped_ragged(
    tmp_path: Path,
) -> None:
    """Line-wrapped records are reconstructed via the trailing-comma boundary rule.

    A line that ends with a trailing comma continues onto the next physical
    line; a line that does not end with a comma closes the logical record.
    Institution 2 wraps across fewer physical lines than institution 1 (the
    real 2024-Q1 RCR7 pattern) -- reconstruction must not assume a fixed
    number of code lines per record.
    """
    layout_path = write_layout(tmp_path, root="RCR7", variable_lines=RCR7_LINES)
    data_path = write_data(
        tmp_path,
        root="RCR7",
        year=2025,
        month=12,
        rows=[
            "6,10,0,12,2025,610000,",
            "10,100,200,",
            "20,300,400,",
            "900",
            "6,20,0,12,2025,620000,",
            "10,50,60,",
            "800",
        ],
    )
    layout = parse_layout(path=layout_path)
    result = read_schedule_file(data_path=data_path, layout=layout)
    rows = _rows(result)
    assert len(rows) == 3

    by_uninum_code = {(r["UNINUM"], r["CAPCODE"]): r for r in rows}
    first = by_uninum_code[(610000, 10)]
    assert (first["VAL1"], first["VAL2"], first["TOTAL"]) == (100, 200, 900)
    second = by_uninum_code[(610000, 20)]
    assert (second["VAL1"], second["VAL2"], second["TOTAL"]) == (300, 400, 900)
    third = by_uninum_code[(620000, 10)]
    assert (third["VAL1"], third["VAL2"], third["TOTAL"]) == (50, 60, 800)


def test_read_single_scenario_pads_missing_trailing_columns_with_null(
    tmp_path: Path,
) -> None:
    """A short data row (older quarter, newer layout) gets nulls for new columns.

    Ground truth: FCA regenerates D_ layout files using the *current* schema
    even for archived quarters, so a historical data row can have fewer
    fields than the shipped layout describes (confirmed on 2015-Q2 RI).
    """
    lines = [*RC_LINES_7COL, "  NEWFIELD  Numeric  0  Field added later"]
    layout_path = write_layout(tmp_path, root="RC", variable_lines=lines)
    data_path = write_data(
        tmp_path,
        root="RC",
        year=2015,
        month=9,
        rows=["6,10,0,9,2015,610000,1000000"],  # 7 fields; layout describes 8
    )
    layout = parse_layout(path=layout_path)
    result = read_schedule_file(data_path=data_path, layout=layout)
    row = _rows(result)[0]
    assert row["TOTASSETS"] == 1000000
    assert row["NEWFIELD"] is None


def test_read_single_scenario_excess_fields_raises(tmp_path: Path) -> None:
    """A data row with *more* fields than the layout describes is a hard error."""
    layout_path = write_layout(tmp_path, root="RC", variable_lines=RC_LINES_7COL)
    data_path = write_data(
        tmp_path,
        root="RC",
        year=2026,
        month=3,
        rows=["6,10,0,3,2026,610000,1000000,999,888"],  # 9 fields; layout describes 7
    )
    layout = parse_layout(path=layout_path)
    with pytest.raises(LayoutParseError):
        read_schedule_file(data_path=data_path, layout=layout)


def test_read_single_scenario_trims_trailing_comma_artifacts(tmp_path: Path) -> None:
    """Dangling trailing-comma fields are trimmed, not treated as excess columns.

    Confirmed against several real, legacy (2004-2008) 'single'-scenario FCA
    schedules (e.g. RCH, RCI) whose rows end in more than one trailing
    comma -- the row must parse the same as if those commas were absent,
    rather than raising as a genuinely-oversized row would.
    """
    layout_path = write_layout(tmp_path, root="RC", variable_lines=RC_LINES_7COL)
    data_path = write_data(
        tmp_path,
        root="RC",
        year=2004,
        month=3,
        # 7 fields expected; 3 dangling trailing commas add 3 empty fields.
        rows=["6,10,0,3,2004,610000,1000000,,,"],
    )
    layout = parse_layout(path=layout_path)
    result = read_schedule_file(data_path=data_path, layout=layout)
    row = _rows(result)[0]
    assert row["TOTASSETS"] == 1000000


def test_read_single_multiple_non_divisible_remainder_raises(tmp_path: Path) -> None:
    """A row whose remainder doesn't divide evenly by the multi-column width errors."""
    layout_path = write_layout(tmp_path, root="RCB", variable_lines=RCB_LINES)
    data_path = write_data(
        tmp_path,
        root="RCB",
        year=2025,
        month=9,
        # 6 leading fields + 4 trailing (not a multiple of the 3-wide group)
        rows=["6,10,0,9,2025,610000,10,100,1.50,20"],
    )
    layout = parse_layout(path=layout_path)
    with pytest.raises(LayoutParseError):
        read_schedule_file(data_path=data_path, layout=layout)


def test_read_schedule_file_is_keyword_only(tmp_path: Path) -> None:
    """read_schedule_file takes no positional arguments."""
    layout_path = write_layout(tmp_path, root="RC", variable_lines=RC_LINES_7COL)
    data_path = write_data(
        tmp_path, root="RC", year=2026, month=3, rows=["6,10,0,3,2026,610000,1000000"]
    )
    layout = parse_layout(path=layout_path)
    with pytest.raises(TypeError):
        read_schedule_file(data_path, layout)  # type: ignore[call-overload]


@pytest.mark.parametrize(
    "dataframe_type",
    ["pandas", "pyarrow_table", "polars_dataframe", "polars_lazyframe"],
)
def test_read_schedule_file_honors_dataframe_type_override(
    tmp_path: Path, dataframe_type: DataFrameType
) -> None:
    """read_schedule_file() converts its result to `dataframe_type` as a final step."""
    expected_type = {
        "pandas": pd.DataFrame,
        "pyarrow_table": pa.Table,
        "polars_dataframe": pl.DataFrame,
        "polars_lazyframe": pl.LazyFrame,
    }[dataframe_type]
    layout_path = write_layout(tmp_path, root="RC", variable_lines=RC_LINES_7COL)
    data_path = write_data(
        tmp_path, root="RC", year=2026, month=3, rows=["6,10,0,3,2026,610000,1000000"]
    )
    layout = parse_layout(path=layout_path)
    result = read_schedule_file(
        data_path=data_path,
        layout=layout,
        dataframe_type=dataframe_type,
    )
    assert isinstance(result, expected_type)


def test_cast_non_numeric_value_falls_back_to_string(tmp_path: Path) -> None:
    """A Numeric-typed field holding a non-numeric value is kept as a string."""
    layout_path = write_layout(tmp_path, root="RC", variable_lines=RC_LINES_7COL)
    data_path = write_data(
        tmp_path,
        root="RC",
        year=2026,
        month=3,
        rows=["6,10,0,3,2026,610000,NA"],  # TOTASSETS is Numeric but the value is "NA"
    )
    layout = parse_layout(path=layout_path)
    row = _rows(read_schedule_file(data_path=data_path, layout=layout))[0]
    assert row["TOTASSETS"] == "NA"


def test_read_single_multiple_trims_trailing_comma_artifact(tmp_path: Path) -> None:
    """A trailing comma's dangling empty field is trimmed, not kept as a group."""
    layout_path = write_layout(tmp_path, root="RCB", variable_lines=RCB_LINES)
    data_path = write_data(
        tmp_path,
        root="RCB",
        year=2025,
        month=9,
        rows=["6,10,0,9,2025,610000,10,100,1.50,"],  # trailing comma -> dangling field
    )
    layout = parse_layout(path=layout_path)
    rows = _rows(read_schedule_file(data_path=data_path, layout=layout))
    assert len(rows) == 1
    assert rows[0]["INV_CODE"] == 10
    assert rows[0]["AMOUNT"] == 100


def test_single_multiple_single_non_divisible_middle_raises(tmp_path: Path) -> None:
    """A reconstructed record whose middle isn't a group-width multiple errors."""
    layout_path = write_layout(tmp_path, root="RCR7", variable_lines=RCR7_LINES)
    data_path = write_data(
        tmp_path,
        root="RCR7",
        year=2025,
        month=12,
        # 6 leading + 2 middle (not a multiple of the 3-wide group) + 1 trailing.
        rows=["6,10,0,12,2025,610000,", "10,100,", "900"],
    )
    layout = parse_layout(path=layout_path)
    with pytest.raises(LayoutParseError):
        read_schedule_file(data_path=data_path, layout=layout)
