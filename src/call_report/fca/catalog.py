"""FCA Call Report release names and download URLs.

FCA's own publication page is
`<https://www.fca.gov/bank-oversight/call-report-data-for-download>`_.
"""

from __future__ import annotations

import re
from datetime import date

from call_report.core import Quarter, ReportingPeriod
from call_report.exceptions import PeriodNotAvailableError

_MODERN_ERA_BASE_URL = "https://www.fca.gov/template-fca/bank/"
_LEGACY_ERA_BASE_URL = "https://www.fca.gov/template-fca/download/CallReportData/"

_MODERN_ERA_FIRST_YEAR = 2015

_MONTH_NAME_BY_QUARTER_END_MONTH: dict[int, str] = {
    3: "March",
    6: "June",
    9: "September",
    12: "December",
}
_LEGACY_ABBREVIATION_BY_QUARTER_END_MONTH: dict[int, str] = {
    3: "Mar",
    6: "Jun",
    9: "Sept",
    12: "Dec",
}

_LEGACY_MONTH_BY_ABBREVIATION: dict[str, int] = {
    "mar": 3,
    "jun": 6,
    "sep": 9,
    "sept": 9,
    "dec": 12,
}
_MODERN_MONTH_BY_NAME: dict[str, int] = {
    name.lower(): month for month, name in _MONTH_NAME_BY_QUARTER_END_MONTH.items()
}
_MODERN_FILENAME = re.compile(r"(?P<year>\d{4})(?P<month>[a-z]+)\.zip", re.IGNORECASE)
_LEGACY_FILENAME = re.compile(r"(?P<month>[a-z]+)(?P<year>\d{4})\.zip", re.IGNORECASE)

# FCA's first release. The local transports check each quarter from here for a
# release, and construct_fca_download_url rejects anything earlier.
_FIRST_RELEASE_PERIOD = ReportingPeriod.from_period_end(value="2000-03-31")


# A function rather than a direct date.today() call, so tests can replace it to
# fix the current quarter.
def _today() -> date:
    """Return today's date.

    The current quarter, the upper bound on every period, is the quarter
    containing this date.

    Returns
    -------
    datetime.date
        Today's date.
    """
    return date.today()


def _current_period() -> ReportingPeriod:
    """Return the quarter containing today's date.

    No source can publish a quarter later than this one, so it is the upper
    bound on every period this package builds a URL or a folder name for.

    Returns
    -------
    ReportingPeriod
        The quarter containing today.
    """
    today = _today()
    return ReportingPeriod(year=today.year, quarter=Quarter((today.month - 1) // 3 + 1))


def fca_release_stem(*, period: ReportingPeriod) -> str:
    """Return the stem of a period's FCA release zip, such as ``"2026March"``.

    FCA changed its filename convention between the legacy and modern eras.
    Periods from 2000 through 2014 put an abbreviated month name before the
    year, such as ``"Sept2014"``. Periods from 2015 onward put the year
    before the full month name, such as ``"2026March"``. Unzipping a
    release gives a folder with this name, which is the folder name the
    transports in `call_report.fca.transport` look for. Any period is
    accepted.

    Parameters
    ----------
    period : ReportingPeriod
        The period to name.

    Returns
    -------
    str
        The release zip's filename without its ``.zip`` extension.

    Examples
    --------
    >>> fca_release_stem(period=ReportingPeriod.from_period_end(value="2026-03-31"))
    '2026March'
    >>> fca_release_stem(period=ReportingPeriod.from_period_end(value="2014-09-30"))
    'Sept2014'
    """
    if period.year >= _MODERN_ERA_FIRST_YEAR:
        return f"{period.year}{_MONTH_NAME_BY_QUARTER_END_MONTH[period.month]}"
    abbreviation = _LEGACY_ABBREVIATION_BY_QUARTER_END_MONTH[period.month]
    return f"{abbreviation}{period.year}"


def parse_fca_release_filename(name: str) -> ReportingPeriod | None:
    """Return the period an FCA release zip filename names, or None.

    Both of FCA's naming conventions are read, in any letter case.
    September may be abbreviated ``Sep`` or ``Sept`` in the legacy
    convention. A name that follows neither convention gives ``None``.

    Parameters
    ----------
    name : str
        A filename, such as ``"2026March.zip"`` or ``"Sept2014.zip"``, with
        no folder.

    Returns
    -------
    ReportingPeriod or None
        The period the file holds, or ``None`` if the name is not an FCA
        release zip.

    Examples
    --------
    >>> parse_fca_release_filename("2026March.zip").label
    '2026Q1'
    >>> parse_fca_release_filename("sep2014.ZIP").label
    '2014Q3'
    >>> parse_fca_release_filename("instructions.pdf") is None
    True
    """
    modern = _MODERN_FILENAME.fullmatch(name)
    if modern is not None:
        year = int(modern["year"])
        month = _MODERN_MONTH_BY_NAME.get(modern["month"].lower())
    else:
        legacy = _LEGACY_FILENAME.fullmatch(name)
        if legacy is None:
            return None
        year = int(legacy["year"])
        month = _LEGACY_MONTH_BY_ABBREVIATION.get(legacy["month"].lower())
    if month is None:
        return None
    return ReportingPeriod(year=year, quarter=Quarter(month // 3))


def construct_fca_download_url(*, period: ReportingPeriod) -> str:
    """Construct the FCA download URL for a period's Call Report zip archive.

    FCA changed its URL and filename convention between the legacy and
    modern eras. Periods from 2000 through 2014 use an abbreviated month
    name under ``/template-fca/download/CallReportData/``. Periods from
    2015 onward use the full month name under ``/template-fca/bank/``.

    Parameters
    ----------
    period : ReportingPeriod
        The period to build a URL for.

    Returns
    -------
    str
        The full URL to that period's zip archive.

    Raises
    ------
    PeriodNotAvailableError
        If `period` is before FCA's first release (2000Q1) or after the
        quarter containing today's date.

    Examples
    --------
    >>> construct_fca_download_url(
    ...     period=ReportingPeriod.from_period_end(value="2026-03-31")
    ... )
    'https://www.fca.gov/template-fca/bank/2026March.zip'
    >>> construct_fca_download_url(
    ...     period=ReportingPeriod.from_period_end(value="2014-09-30")
    ... )
    'https://www.fca.gov/template-fca/download/CallReportData/Sept2014.zip'
    """
    latest = _current_period()
    if period < _FIRST_RELEASE_PERIOD or period > latest:
        raise PeriodNotAvailableError(
            f"FCA does not publish {period.label}. Its releases run from "
            f"{_FIRST_RELEASE_PERIOD.label} through no later than the current "
            f"quarter, {latest.label}."
        )
    base_url = (
        _MODERN_ERA_BASE_URL
        if period.year >= _MODERN_ERA_FIRST_YEAR
        else _LEGACY_ERA_BASE_URL
    )
    return f"{base_url}{fca_release_stem(period=period)}.zip"
