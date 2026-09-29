"""Tests for FCACallReport.institutions and FCACallReport.get_institution."""

from __future__ import annotations

from pathlib import Path

import pytest

from call_report.core import PeriodRange
from call_report.exceptions import DownloadError, LayoutParseError
from call_report.fca import FCACallReport, FCAInstitutionRegistry
from call_report.fca.transport import LocalDirectoryTransport
from tests.fca.layouts import RC_LINES_7COL
from tests.helpers import write_data, write_layout

# The roster's real columns, in FCA's order.
INST_FULL_LINES = [
    "  SYSTEM       Numeric    0  System Code",
    "  DIST         Numeric    0  District Code",
    "  ASSOC        Numeric    0  Association Code",
    "  MONTH        Numeric    0  Month of Report",
    "  YEAR         Numeric    0  Year of Report",
    "  UNINUM       Numeric    0  Unique institution number",
    "  SHORTNAME    Alphanum.  0  Institution short name",
    "  MAIL_ADDR    Alphanum.  0  Mailing address",
    "  STREET_ADDR  Alphanum.  0  Street address",
    "  CITY         Alphanum.  0  City name",
    "  STATE        Alphanum.  0  State name",
    "  ZIP          Alphanum.  0  Zip code",
]


def roster_row(*, month: int, year: int, uninum: int, name: str) -> str:
    """Return one roster data line for a district-22 association."""
    return (
        f'7,22,{uninum % 1000},{month},{year},{uninum},"{name}",'
        '"P.O. Box 34390","1601 UPS Drive","Louisville","KY","40223-4390"'
    )


def write_release(
    data_dir: Path, *, name: str, year: int, month: int, rows: list[str]
) -> None:
    """Write one release directory holding only an institution roster."""
    directory = data_dir / name
    directory.mkdir()
    write_layout(directory, root="INST", variable_lines=INST_FULL_LINES)
    write_data(directory, root="INST", year=year, month=month, rows=rows)


@pytest.fixture
def two_quarters(tmp_path: Path) -> Path:
    """Two releases: 722825 is renamed in the second, and 722233 appears in it."""
    write_release(
        tmp_path,
        name="2025September",
        year=2025,
        month=9,
        rows=[roster_row(month=9, year=2025, uninum=722825, name="Mid-America ACA")],
    )
    write_release(
        tmp_path,
        name="2025December",
        year=2025,
        month=12,
        rows=[
            roster_row(
                month=12, year=2025, uninum=722825, name="Farm Credit Mid-America ACA"
            ),
            roster_row(month=12, year=2025, uninum=722233, name="Other ACA"),
        ],
    )
    return tmp_path


def report(data_dir: Path) -> FCACallReport:
    """Return a report over the two fixture quarters, not yet fetched."""
    return FCACallReport(
        start="2025-09-30",
        end="2025-12-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )


def test_institutions_without_fetch(two_quarters: Path, backend: str) -> None:
    """institutions() fetches on its own and covers every roster row."""
    registry = report(two_quarters).institutions()
    assert list(registry) == [722233, 722825]
    assert registry[722825].periods == (
        PeriodRange(start="2025-09-30", end="2025-12-31"),
    )
    assert [v.value for v in registry[722825].short_name_history] == [
        "Mid-America ACA",
        "Farm Credit Mid-America ACA",
    ]


def test_institutions_matches_the_loaded_roster(two_quarters: Path) -> None:
    """The registry is exactly what load_institutions() returns, as a registry."""
    fca = report(two_quarters)
    expected = FCAInstitutionRegistry.from_dataframe(data=fca.load_institutions())
    assert fca.institutions() == expected


def test_institutions_is_reused_until_refetch(two_quarters: Path) -> None:
    """The registry is built once, and built again after fetch() runs again.

    Without the reset, a report re-fetched after its release files changed
    would keep serving the old registry.
    """
    fca = report(two_quarters)
    first = fca.institutions()
    assert fca.institutions() is first
    fca.fetch()
    assert fca.institutions() is not first
    assert fca.institutions() == first


def test_get_institution(two_quarters: Path) -> None:
    """get_institution() returns one UNINUM's history."""
    institution = report(two_quarters).get_institution(uninum=722233)
    assert institution.most_recent_short_name == "Other ACA"


def test_get_institution_unknown_uninum(two_quarters: Path) -> None:
    """A UNINUM that is in none of the report's rosters raises KeyError."""
    with pytest.raises(KeyError, match="UNINUM 999999 is not in the registry"):
        report(two_quarters).get_institution(uninum=999999)


def test_unreadable_roster_is_skipped(two_quarters: Path) -> None:
    """A quarter whose roster cannot be parsed is left out and recorded.

    This follows load_institutions(), which skips such a quarter rather
    than failing the whole report.
    """
    write_release(
        two_quarters,
        name="2026March",
        year=2026,
        month=3,
        rows=[roster_row(month=3, year=2026, uninum=722825, name="X")],
    )
    bad = two_quarters / "2026March"
    for layout in bad.glob("D_*"):
        layout.unlink()
    write_layout(
        bad,
        root="INST",
        variable_lines=[
            "  SYSTEM    Numeric  0  System Code",
            "  **CODE1   Numeric  0  Code One",
            "  MIDFIELD  Numeric  0  Mid Field",
            "  **CODE2   Numeric  0  Code Two",
        ],
    )
    fca = FCACallReport(
        start="2025-09-30",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=two_quarters),
    )
    registry = fca.institutions()
    assert registry.last_period.label == "2025Q4"
    assert any(isinstance(issue.error, LayoutParseError) for issue in fca.errors_)


def test_no_roster_raises(tmp_path: Path) -> None:
    """A report whose releases have no institution roster cannot build a registry."""
    directory = tmp_path / "2026March"
    directory.mkdir()
    write_layout(directory, root="RC", variable_lines=RC_LINES_7COL)
    write_data(
        directory, root="RC", year=2026, month=3, rows=["6,10,0,3,2026,610000,1"]
    )
    fca = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=tmp_path),
    )
    with pytest.raises(DownloadError, match="roster"):
        fca.institutions()
