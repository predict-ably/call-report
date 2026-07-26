"""Sanity tests for shared enums and the exception hierarchy."""

from __future__ import annotations

import pytest

from call_report.enums import FileKind, Quarter, Source
from call_report.exceptions import (
    CallReportError,
    DownloadError,
    InvalidPeriodError,
    LayoutParseError,
    PeriodNotAvailableError,
    ScheduleNotFoundError,
)


def test_quarter_is_ordered() -> None:
    """Quarter members compare in calendar order."""
    assert Quarter.Q1 < Quarter.Q2 < Quarter.Q3 < Quarter.Q4


def test_quarter_values() -> None:
    """Quarter values are the 1-4 ordinal, not the end month."""
    assert [q.value for q in Quarter] == [1, 2, 3, 4]


def test_source_members() -> None:
    """Source enumerates the four regulatory regimes named in CLAUDE.md."""
    assert {member.value for member in Source} == {"FCA", "FFIEC", "FDIC", "NCUA"}


def test_file_kind_members() -> None:
    """FileKind distinguishes layout/metadata files from data files."""
    assert {member.value for member in FileKind} == {"METADATA", "DATA"}


@pytest.mark.parametrize(
    "error_cls",
    [
        InvalidPeriodError,
        PeriodNotAvailableError,
        ScheduleNotFoundError,
        LayoutParseError,
        DownloadError,
    ],
)
def test_all_errors_are_rooted_at_call_report_error(
    error_cls: type[CallReportError],
) -> None:
    """Every domain exception is catchable via `except CallReportError`."""
    assert issubclass(error_cls, CallReportError)
    with pytest.raises(CallReportError):
        raise error_cls("boom")


def test_call_report_error_is_a_plain_exception() -> None:
    """CallReportError itself is a normal Exception subclass."""
    assert issubclass(CallReportError, Exception)
