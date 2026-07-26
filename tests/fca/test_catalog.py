"""Tests for the pure FCA download-URL catalog (call_report.fca.catalog)."""

from __future__ import annotations

import pytest

from call_report.exceptions import PeriodNotAvailableError
from call_report.fca.catalog import EARLIEST_PERIOD, LATEST_KNOWN_PERIOD, url_for
from call_report.periods import ReportingPeriod


@pytest.mark.parametrize(
    ("period_end", "expected_url"),
    [
        # modern era (2015+): full month name, /template-fca/bank/
        ("2026-03-31", "https://www.fca.gov/template-fca/bank/2026March.zip"),
        ("2015-03-31", "https://www.fca.gov/template-fca/bank/2015March.zip"),
        ("2020-06-30", "https://www.fca.gov/template-fca/bank/2020June.zip"),
        ("2020-09-30", "https://www.fca.gov/template-fca/bank/2020September.zip"),
        ("2020-12-31", "https://www.fca.gov/template-fca/bank/2020December.zip"),
        # legacy era (2000-2014): abbreviated month, /template-fca/download/
        (
            "2014-12-31",
            "https://www.fca.gov/template-fca/download/CallReportData/Dec2014.zip",
        ),
        (
            "2014-09-30",
            "https://www.fca.gov/template-fca/download/CallReportData/Sept2014.zip",
        ),
        (
            "2014-06-30",
            "https://www.fca.gov/template-fca/download/CallReportData/Jun2014.zip",
        ),
        (
            "2000-03-31",
            "https://www.fca.gov/template-fca/download/CallReportData/Mar2000.zip",
        ),
    ],
)
def test_url_for_matches_confirmed_fca_pattern(
    period_end: str, expected_url: str
) -> None:
    """url_for reproduces the exact URL patterns confirmed on fca.gov."""
    period = ReportingPeriod.from_period_end(value=period_end)
    assert url_for(period=period) == expected_url


def test_url_for_era_boundary_2014_to_2015() -> None:
    """The URL convention switches exactly between Dec 2014 and Mar 2015."""
    legacy = url_for(period=ReportingPeriod.from_period_end(value="2014-12-31"))
    modern = url_for(period=ReportingPeriod.from_period_end(value="2015-03-31"))
    assert "/template-fca/download/CallReportData/" in legacy
    assert "/template-fca/bank/" in modern


def test_url_for_september_uses_four_letter_abbreviation() -> None:
    """September abbreviates to 'Sept', not 'Sep', in the legacy URL convention."""
    url = url_for(period=ReportingPeriod.from_period_end(value="2012-09-30"))
    assert url.endswith("Sept2012.zip")


def test_earliest_and_latest_known_period_bounds() -> None:
    """The catalog's known bounds match the confirmed FCA publication history."""
    assert ReportingPeriod.from_period_end(value="2000-03-31") == EARLIEST_PERIOD
    assert ReportingPeriod.from_period_end(value="2026-03-31") == LATEST_KNOWN_PERIOD


def test_url_for_rejects_period_before_earliest() -> None:
    """A period before FCA's earliest publication raises PeriodNotAvailableError."""
    too_early = ReportingPeriod.from_period_end(value="1999-12-31")
    with pytest.raises(PeriodNotAvailableError) as exc_info:
        url_for(period=too_early)
    message = str(exc_info.value)
    assert "2000-03-31" in message
    assert "2026-03-31" in message


def test_url_for_rejects_period_after_latest_known() -> None:
    """A period after the latest known release raises PeriodNotAvailableError."""
    too_late = ReportingPeriod.from_period_end(value="2026-06-30")
    with pytest.raises(PeriodNotAvailableError):
        url_for(period=too_late)


def test_url_for_is_keyword_only() -> None:
    """url_for takes no positional arguments."""
    period = ReportingPeriod.from_period_end(value="2026-03-31")
    with pytest.raises(TypeError):
        url_for(period)  # type: ignore[misc]
