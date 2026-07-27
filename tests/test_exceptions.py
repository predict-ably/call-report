"""Sanity tests for the shared exception hierarchy."""

from __future__ import annotations

import pytest

from call_report.exceptions import (
    CallReportError,
    DownloadError,
    InvalidPeriodError,
    LayoutParseError,
    PeriodNotAvailableError,
    ScheduleNotFoundError,
)


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
