"""Tests for FCA release names and download URLs (call_report.fca.catalog)."""

from __future__ import annotations

import importlib
from datetime import date

import pytest

from call_report.core import ReportingPeriod
from call_report.exceptions import PeriodNotAvailableError
from call_report.fca import catalog
from call_report.fca.catalog import (
    construct_fca_download_url,
    fca_release_stem,
    parse_fca_release_filename,
)
from tests.helpers import newest_archived_period


@pytest.fixture
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    """Fix today's date at 2026-10-12, inside 2026Q4."""
    fixed = date(2026, 10, 12)
    monkeypatch.setattr(catalog, "_today", lambda: fixed)
    return fixed


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
def test_construct_fca_download_url_matches_confirmed_fca_pattern(
    period_end: str, expected_url: str
) -> None:
    """construct_fca_download_url reproduces the exact patterns confirmed on fca.gov."""
    period = ReportingPeriod.from_period_end(value=period_end)
    assert construct_fca_download_url(period=period) == expected_url


def test_construct_fca_download_url_era_boundary_2014_to_2015() -> None:
    """The URL convention switches exactly between Dec 2014 and Mar 2015."""
    legacy = construct_fca_download_url(
        period=ReportingPeriod.from_period_end(value="2014-12-31")
    )
    modern = construct_fca_download_url(
        period=ReportingPeriod.from_period_end(value="2015-03-31")
    )
    assert "/template-fca/download/CallReportData/" in legacy
    assert "/template-fca/bank/" in modern


def test_construct_fca_download_url_september_uses_four_letter_abbreviation() -> None:
    """September abbreviates to 'Sept', not 'Sep', in the legacy URL convention."""
    url = construct_fca_download_url(
        period=ReportingPeriod.from_period_end(value="2012-09-30")
    )
    assert url.endswith("Sept2012.zip")


@pytest.mark.parametrize("name", ["LATEST_KNOWN_PERIOD", "EARLIEST_PERIOD"])
def test_the_period_constants_are_gone(name: str) -> None:
    """Neither period constant is public, so no release needs one changed."""
    module = importlib.import_module("call_report.fca.catalog")
    assert not hasattr(module, name)


def test_construct_fca_download_url_rejects_period_before_first_release(
    today: date,
) -> None:
    """A period before FCA's first release raises PeriodNotAvailableError."""
    too_early = ReportingPeriod.from_period_end(value="1999-12-31")
    with pytest.raises(PeriodNotAvailableError) as exc_info:
        construct_fca_download_url(period=too_early)
    message = str(exc_info.value)
    assert "1999Q4" in message
    assert "2000Q1" in message
    assert "2026Q4" in message


def test_construct_fca_download_url_accepts_the_current_quarter(today: date) -> None:
    """The quarter containing today is the latest period with a URL."""
    current = ReportingPeriod.from_period_end(value="2026-12-31")
    assert construct_fca_download_url(period=current).endswith("2026December.zip")


def test_construct_fca_download_url_rejects_a_period_after_today(today: date) -> None:
    """A quarter after the one containing today raises PeriodNotAvailableError."""
    too_late = ReportingPeriod.from_period_end(value="2027-03-31")
    with pytest.raises(PeriodNotAvailableError, match="2027Q1"):
        construct_fca_download_url(period=too_late)


def test_the_upper_bound_moves_with_today(monkeypatch: pytest.MonkeyPatch) -> None:
    """No constant fixes the newest quarter. It follows today's date."""
    period = ReportingPeriod.from_period_end(value="2027-03-31")
    monkeypatch.setattr(catalog, "_today", lambda: date(2027, 1, 1))
    assert construct_fca_download_url(period=period).endswith("2027March.zip")


@pytest.mark.parametrize(
    ("today_value", "expected"),
    [
        (date(2026, 1, 1), "2026Q1"),
        (date(2026, 3, 31), "2026Q1"),
        (date(2026, 4, 1), "2026Q2"),
        (date(2026, 9, 30), "2026Q3"),
        (date(2026, 12, 31), "2026Q4"),
    ],
)
def test_current_period_is_the_quarter_containing_today(
    monkeypatch: pytest.MonkeyPatch, today_value: date, expected: str
) -> None:
    """The current period is the quarter today falls in, at both of its edges."""
    monkeypatch.setattr(catalog, "_today", lambda: today_value)
    assert catalog._current_period().label == expected


def test_the_real_today_is_on_or_after_the_newest_archived_quarter() -> None:
    """The unpatched upper bound never cuts off a quarter the archive holds."""
    assert catalog._current_period() >= newest_archived_period()


# ---------------------------------------------------------------------------
# fca_release_stem
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("period_end", "stem"),
    [
        ("2000-03-31", "Mar2000"),
        ("2003-06-30", "Jun2003"),
        ("2014-09-30", "Sept2014"),
        ("2014-12-31", "Dec2014"),
        ("2015-03-31", "2015March"),
        ("2020-06-30", "2020June"),
        ("2020-09-30", "2020September"),
        ("2026-12-31", "2026December"),
    ],
)
def test_fca_release_stem_follows_each_era(period_end: str, stem: str) -> None:
    """The stem puts an abbreviated month first before 2015, the year first after."""
    period = ReportingPeriod.from_period_end(value=period_end)
    assert fca_release_stem(period=period) == stem


def test_fca_release_stem_has_no_upper_bound(today: date) -> None:
    """A quarter long after today still gets a stem, so any folder can be named."""
    period = ReportingPeriod.from_period_end(value="2099-06-30")
    assert fca_release_stem(period=period) == "2099June"


def test_fca_release_stem_matches_the_download_url() -> None:
    """The stem is the download URL's filename without ``.zip``."""
    period = newest_archived_period()
    url = construct_fca_download_url(period=period)
    assert url.endswith(f"/{fca_release_stem(period=period)}.zip")


# ---------------------------------------------------------------------------
# parse_fca_release_filename
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "period_end"),
    [
        ("Mar2000.zip", "2000-03-31"),
        ("Jun2003.zip", "2003-06-30"),
        ("Sept2014.zip", "2014-09-30"),
        ("Sep2014.zip", "2014-09-30"),
        ("Dec2014.zip", "2014-12-31"),
        ("2015March.zip", "2015-03-31"),
        ("2026June.zip", "2026-06-30"),
        ("2020September.zip", "2020-09-30"),
        ("2025December.zip", "2025-12-31"),
        ("2026MARCH.ZIP", "2026-03-31"),
        ("sept2014.zip", "2014-09-30"),
        ("2026june.Zip", "2026-06-30"),
    ],
)
def test_parse_fca_release_filename_reads_both_eras(name: str, period_end: str) -> None:
    """Both naming conventions are read, in any letter case, with Sep or Sept."""
    expected = ReportingPeriod.from_period_end(value=period_end)
    assert parse_fca_release_filename(name) == expected


@pytest.mark.parametrize(
    "name",
    [
        "2026March.pdf",
        "2026March",
        "2026Mar.zip",
        "March2026.zip",
        "Jan2014.zip",
        "2026Jan.zip",
        "Mar14.zip",
        "UCRCallRptInstructionsJune2026.zip",
        "bank/2026March.zip",
        "2026March.zip.bak",
        "",
        "Mar2020.zip",
        "Sept2015.zip",
        "2014March.zip",
        "2000March.zip",
        "0001March.zip",
        "Mar0001.zip",
        "Sep2020.zip",
        "Septe2014.zip",
    ],
)
def test_parse_fca_release_filename_rejects_other_names(name: str) -> None:
    """A name that is not the release zip name FCA uses gives None.

    That includes a real month and year written in the other era's
    convention, such as ``Mar2020.zip``. FCA names 2020Q1's zip
    ``2020March.zip``, so accepting both would let the two disagree.
    """
    assert parse_fca_release_filename(name) is None


def test_construct_fca_download_url_is_keyword_only() -> None:
    """construct_fca_download_url takes no positional arguments."""
    period = ReportingPeriod.from_period_end(value="2026-03-31")
    with pytest.raises(TypeError):
        construct_fca_download_url(period)  # type: ignore[call-arg]
