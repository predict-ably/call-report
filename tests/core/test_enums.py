"""Sanity tests for the shared, source-agnostic enums."""

from __future__ import annotations

from call_report.core import FileType, Quarter, Source


def test_quarter_is_ordered() -> None:
    """Quarter members compare in calendar order."""
    assert Quarter.Q1 < Quarter.Q2 < Quarter.Q3 < Quarter.Q4


def test_quarter_values() -> None:
    """Quarter values are the 1-4 ordinal, not the end month."""
    assert [q.value for q in Quarter] == [1, 2, 3, 4]


def test_quarter_month_helpers() -> None:
    """first_month, last_month, and months describe a quarter's calendar span."""
    assert Quarter.Q2.first_month == 4
    assert Quarter.Q2.last_month == 6
    assert Quarter.Q2.months == (4, 5, 6)


def test_source_members() -> None:
    """Source enumerates the four regulatory regimes named in CLAUDE.md."""
    assert {member.value for member in Source} == {"FCA", "FFIEC", "FDIC", "NCUA"}


def test_file_type_members() -> None:
    """FileType distinguishes layout/metadata files from data files."""
    assert {member.value for member in FileType} == {"METADATA", "DATA"}
