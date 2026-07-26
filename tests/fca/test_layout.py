"""Tests for FCA layout-file (D_<ROOT>.TXT) parsing (call_report.fca.layout)."""

from __future__ import annotations

from pathlib import Path

import pytest

from call_report.exceptions import LayoutParseError
from call_report.fca.layout import parse_layout
from tests.conftest import write_layout
from tests.fca.conftest import INST_LINES, RC_LINES_7COL, RCB_LINES, RCX_LINES


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
    path = write_layout(tmp_path, root="RCX", variable_lines=RCX_LINES)
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
        parse_layout(path)  # type: ignore[misc]
