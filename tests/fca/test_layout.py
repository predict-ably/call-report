"""Tests for FCA layout-file (D_<ROOT>.TXT) parsing (call_report.fca.layout)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import narwhals as nw
import pytest

from call_report.core import ReportingPeriod
from call_report.exceptions import InvalidPeriodError, LayoutParseError, SchemaError
from call_report.fca.layout import (
    FIXED_IDENTIFIER_COLUMNS,
    infer_field_dtype,
    parse_layout,
)
from tests.fca.layouts import INST_LINES, RC_LINES_7COL, RCB_LINES, RCR7_LINES
from tests.helpers import write_layout


def test_parse_layout_single_scenario(tmp_path: Path) -> None:
    """A layout with no ** columns is scenario 'single'."""
    path = write_layout(tmp_path, root="RC", variable_lines=RC_LINES_7COL)
    layout = parse_layout(path=path)
    assert layout.scenario == "single"
    assert layout.leading_columns == (
        "SYSTEM",
        "DIST",
        "ASSOC",
        "MONTH",
        "YEAR",
        "UNINUM",
        "TOTASSETS",
    )
    assert layout.multi_columns == ()
    assert layout.trailing_columns == ()


def test_parse_layout_single_multiple_scenario(tmp_path: Path) -> None:
    """A layout with exactly one run of ** columns is scenario 'single_multiple'."""
    path = write_layout(
        tmp_path,
        root="RCB",
        variable_lines=RCB_LINES,
        note="THE RECORD CONTAINS MULTIPLE OCCURRENCES OF THESE VARIABLES.",
    )
    layout = parse_layout(path=path)
    assert layout.scenario == "single_multiple"
    assert layout.leading_columns == (
        "SYSTEM",
        "DIST",
        "ASSOC",
        "MONTH",
        "YEAR",
        "UNINUM",
    )
    assert layout.multi_columns == ("INV_CODE", "AMOUNT", "AMOUNT2")
    assert layout.trailing_columns == ()
    # The NOTE block must not leak into the variable table.
    names = [row["name"] for row in layout.variables_as_dicts()]
    assert "NOTE" not in names


def test_parse_layout_single_multiple_single_scenario(tmp_path: Path) -> None:
    """A layout with single/multi/single runs is scenario 'single_multiple_single'."""
    path = write_layout(tmp_path, root="RCR7", variable_lines=RCR7_LINES)
    layout = parse_layout(path=path)
    assert layout.scenario == "single_multiple_single"
    assert layout.leading_columns == (
        "SYSTEM",
        "DIST",
        "ASSOC",
        "MONTH",
        "YEAR",
        "UNINUM",
    )
    assert layout.multi_columns == ("CAPCODE", "VAL1", "VAL2")
    assert layout.trailing_columns == ("TOTAL",)


def test_parse_layout_records_type_and_decimal_position(tmp_path: Path) -> None:
    """Each variable's type (Numeric/Alphanum.) and decimal position are captured."""
    path = write_layout(tmp_path, root="INST", variable_lines=INST_LINES)
    layout = parse_layout(path=path)
    by_name = {row["name"]: row for row in layout.variables_as_dicts()}
    assert by_name["UNINUM"]["type"] == "Numeric"
    assert by_name["UNINUM"]["decimal_position"] == 0
    assert by_name["SHORTNAME"]["type"] == "Alphanum."
    assert by_name["SHORTNAME"]["is_multi"] is False


def test_parse_layout_normalizes_lowercase_numeric(tmp_path: Path) -> None:
    """The one real-world lowercase 'numeric' anomaly normalizes like 'Numeric'."""
    lines = [
        "  SYSTEM    Numeric   0  System Code",
        "  ODDFIELD\t numeric     0  Field using lowercase numeric keyword",
    ]
    path = write_layout(tmp_path, root="RI", variable_lines=lines)
    layout = parse_layout(path=path)
    names = [row["name"] for row in layout.variables_as_dicts()]
    assert names == ["SYSTEM", "ODDFIELD"]
    assert layout.scenario == "single"


def test_parse_layout_decodes_windows_1252(tmp_path: Path) -> None:
    """Non-ASCII windows-1252 bytes in a definition decode to the correct characters."""
    lines = [
        "  SYSTEM  Numeric    0  System Code",
        "  NOTE1   Alphanum.  0  Institution's café summary — details",
    ]
    path = write_layout(tmp_path, root="RI", variable_lines=lines)
    layout = parse_layout(path=path)
    by_name = {row["name"]: row for row in layout.variables_as_dicts()}
    assert "café" in by_name["NOTE1"]["definition"]
    assert "—" in by_name["NOTE1"]["definition"]


def test_parse_layout_rejects_unrecognized_run_pattern(tmp_path: Path) -> None:
    """Four alternating single/multi runs don't match any known scenario."""
    lines = [
        "  SYSTEM    Numeric  0  System Code",
        "  **CODE1   Numeric  0  Code One",
        "  MIDFIELD  Numeric  0  Mid Field",
        "  **CODE2   Numeric  0  Code Two",
    ]
    path = write_layout(tmp_path, root="BAD", variable_lines=lines)
    with pytest.raises(LayoutParseError, match="BAD"):
        parse_layout(path=path)


def test_parse_layout_is_keyword_only(tmp_path: Path) -> None:
    """parse_layout takes no positional arguments."""
    path = write_layout(tmp_path, root="RC", variable_lines=RC_LINES_7COL)
    with pytest.raises(TypeError):
        parse_layout(path)  # type: ignore[call-arg]


def test_parse_layout_skips_incomplete_variable_line(tmp_path: Path) -> None:
    """A tokenized line with fewer than three fields is skipped, not parsed.

    The trailing ``BAZ Numeric`` entry has no decimal position, so after
    tokenization it yields a two-token line that must be dropped rather than
    raising on the missing decimal-position field.
    """
    lines = [*RC_LINES_7COL, "  BAZ  Numeric"]
    path = write_layout(tmp_path, root="RC", variable_lines=lines)
    layout = parse_layout(path=path)
    names = [row["name"] for row in layout.variables_as_dicts()]
    assert "BAZ" not in names
    assert layout.scenario == "single"


def test_infer_field_dtype_numeric_whole_number_is_int64() -> None:
    """Numeric with decimal_position 0 maps to Int64."""
    assert infer_field_dtype(var_type="Numeric", decimal_position=0) == nw.Int64()


def test_infer_field_dtype_numeric_with_decimals_is_float64() -> None:
    """Numeric with a nonzero decimal_position maps to Float64."""
    assert infer_field_dtype(var_type="Numeric", decimal_position=2) == nw.Float64()


def test_infer_field_dtype_alphanum_is_string() -> None:
    """Alphanum. maps to String, regardless of decimal_position."""
    assert infer_field_dtype(var_type="Alphanum.", decimal_position=0) == nw.String()


def test_infer_field_dtype_is_keyword_only() -> None:
    """infer_field_dtype takes no positional arguments."""
    with pytest.raises(TypeError):
        infer_field_dtype("Numeric", 0)  # type: ignore[call-arg]


def test_fixed_identifier_columns_matches_a_single_scenario_layout(
    tmp_path: Path,
) -> None:
    """The fixed prefix is the start of leading_columns for a 'single' schedule."""
    path = write_layout(tmp_path, root="RC", variable_lines=RC_LINES_7COL)
    layout = parse_layout(path=path)
    assert layout.leading_columns[: len(FIXED_IDENTIFIER_COLUMNS)] == (
        FIXED_IDENTIFIER_COLUMNS
    )


def test_fixed_identifier_columns_matches_a_code_bearing_layout(
    tmp_path: Path,
) -> None:
    """The fixed prefix is exactly leading_columns for a code-bearing schedule."""
    path = write_layout(
        tmp_path,
        root="RCB",
        variable_lines=RCB_LINES,
        note="THE RECORD CONTAINS MULTIPLE OCCURRENCES OF THESE VARIABLES.",
    )
    layout = parse_layout(path=path)
    assert layout.leading_columns == FIXED_IDENTIFIER_COLUMNS


# ---------------------------------------------------------------------------
# FCALayout.to_field_schema()
# ---------------------------------------------------------------------------


def test_to_field_schema_preserves_names_and_layout_order(tmp_path: Path) -> None:
    """The schema's fields are the layout's variables, in the order declared."""
    path = write_layout(
        tmp_path,
        root="RCB",
        variable_lines=RCB_LINES,
        note="THE RECORD CONTAINS MULTIPLE OCCURRENCES OF THESE VARIABLES.",
    )
    schema = parse_layout(path=path).to_field_schema(period="2026-03-31")
    assert schema.names == (
        "SYSTEM",
        "DIST",
        "ASSOC",
        "MONTH",
        "YEAR",
        "UNINUM",
        "INV_CODE",
        "AMOUNT",
        "AMOUNT2",
    )


def test_to_field_schema_translates_declared_types_to_dtypes(tmp_path: Path) -> None:
    """Each field's dtype comes from infer_field_dtype, reaching FieldSchema.schema."""
    path = write_layout(
        tmp_path,
        root="RCB",
        variable_lines=[*RCB_LINES, "  SHORTNAME  Alphanum.  0  Institution name"],
        note="THE RECORD CONTAINS MULTIPLE OCCURRENCES OF THESE VARIABLES.",
    )
    schema = parse_layout(path=path).to_field_schema(period="2026-03-31")
    assert schema.schema["AMOUNT"] == nw.Int64()
    assert schema.schema["AMOUNT2"] == nw.Float64()
    assert schema.schema["SHORTNAME"] == nw.String()


def test_to_field_schema_carries_definitions_verbatim(tmp_path: Path) -> None:
    """A field's definition is the layout's own text, not a summary of it."""
    path = write_layout(tmp_path, root="RC", variable_lines=RC_LINES_7COL)
    schema = parse_layout(path=path).to_field_schema(period="2026-03-31")
    assert schema["TOTASSETS"].versions[0].definition == "Total Assets"


def test_to_field_schema_gives_each_field_one_single_quarter_version(
    tmp_path: Path,
) -> None:
    """A layout describes one release, so every field spans only that quarter."""
    path = write_layout(tmp_path, root="RC", variable_lines=RC_LINES_7COL)
    schema = parse_layout(path=path).to_field_schema(period="2025-09-30")
    period = ReportingPeriod.from_period_end(value="2025-09-30")
    for field in schema.values():
        assert len(field.versions) == 1
        assert tuple(field.versions[0].periods) == (period,)


@pytest.mark.parametrize(
    "period",
    [
        "2025-09-30",
        date(2025, 9, 30),
        ReportingPeriod.from_period_end(value="2025-09-30"),
    ],
    ids=["str", "date", "reporting_period"],
)
def test_to_field_schema_accepts_every_period_form(
    tmp_path: Path, period: str | date | ReportingPeriod
) -> None:
    """The period argument is coerced by PeriodRange, so all three forms agree."""
    path = write_layout(tmp_path, root="RC", variable_lines=RC_LINES_7COL)
    schema = parse_layout(path=path).to_field_schema(period=period)
    assert schema["UNINUM"].versions[0].periods[0].label == "2025Q3"


def test_to_field_schema_rejects_a_non_quarter_end_period(tmp_path: Path) -> None:
    """A date that is not a quarter end is rejected before any field is built."""
    path = write_layout(tmp_path, root="RC", variable_lines=RC_LINES_7COL)
    layout = parse_layout(path=path)
    with pytest.raises(InvalidPeriodError):
        layout.to_field_schema(period="2025-09-15")


def test_to_field_schema_rejects_a_layout_with_a_repeated_variable(
    tmp_path: Path,
) -> None:
    """FieldSchema rejects duplicate names, so a malformed layout surfaces as one.

    No release published since 2000 declares a variable twice, but nothing in
    parse_layout enforces that, so the failure has to be a clear SchemaError
    rather than a silently dropped column.
    """
    path = write_layout(
        tmp_path,
        root="RC",
        variable_lines=[*RC_LINES_7COL, "  TOTASSETS  Numeric  0  Duplicated"],
    )
    layout = parse_layout(path=path)
    with pytest.raises(SchemaError, match="TOTASSETS"):
        layout.to_field_schema(period="2026-03-31")


def test_to_field_schema_is_keyword_only(tmp_path: Path) -> None:
    """to_field_schema takes no positional arguments."""
    path = write_layout(tmp_path, root="RC", variable_lines=RC_LINES_7COL)
    layout = parse_layout(path=path)
    with pytest.raises(TypeError):
        layout.to_field_schema("2026-03-31")  # type: ignore[call-arg]
