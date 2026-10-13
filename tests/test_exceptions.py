"""Sanity tests for the shared exception hierarchy."""

from __future__ import annotations

import pytest

from call_report.exceptions import (
    CallReportError,
    DomainDatasetNotFoundError,
    DownloadError,
    InsecureConnectionWarning,
    InstitutionError,
    InvalidPeriodError,
    LayoutParseError,
    PeriodNotAvailableError,
    ReleaseCatalogError,
    ReshapeError,
    ScheduleNotFoundError,
    SchemaError,
    ZipArchiveUnpackingError,
)


@pytest.mark.parametrize(
    "error_cls",
    [
        InvalidPeriodError,
        PeriodNotAvailableError,
        ScheduleNotFoundError,
        DomainDatasetNotFoundError,
        LayoutParseError,
        SchemaError,
        DownloadError,
        InstitutionError,
        ReshapeError,
        ZipArchiveUnpackingError,
        ReleaseCatalogError,
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


def test_insecure_connection_warning_is_a_user_warning() -> None:
    """InsecureConnectionWarning is shown by default and can be filtered alone."""
    assert issubclass(InsecureConnectionWarning, UserWarning)
    assert not issubclass(InsecureConnectionWarning, CallReportError)


def test_release_catalog_error_is_a_download_error_and_a_value_error() -> None:
    """Text that is not a list of releases is a failed download and bad input.

    Code that catches `DownloadError` around a download keeps working, and
    code that expects `ValueError` for bad input text gets one.
    """
    assert issubclass(ReleaseCatalogError, DownloadError)
    assert issubclass(ReleaseCatalogError, ValueError)
