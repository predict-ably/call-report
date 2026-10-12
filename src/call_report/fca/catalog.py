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

# A release zip's name is four digits of year and some letters, in either
# order. parse_fca_release_filename then keeps it only if it is the exact
# stem fca_release_stem gives that year's quarter.
_RELEASE_FILENAME = re.compile(
    r"(?:[a-z]+(?P<legacy_year>\d{4})|(?P<modern_year>\d{4})[a-z]+)\.zip",
    re.IGNORECASE,
)

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

    See Also
    --------
    parse_fca_release_filename : Turn a release zip's name back into its period.
    construct_fca_download_url : Build the FCA URL of a period's release zip.

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

    A name is read only if it is the name `fca_release_stem` gives a
    quarter, followed by ``.zip``. Letter case does not matter, and
    September may be ``Sep`` as well as ``Sept``. Any other name gives
    ``None``, including a name in the other era's convention, such as
    ``"Mar2020.zip"`` for a 2020 quarter.

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

    See Also
    --------
    fca_release_stem : Return the name of a period's release zip.
    construct_fca_download_url : Build the FCA URL of a period's release zip.

    Examples
    --------
    >>> parse_fca_release_filename("2026March.zip").label
    '2026Q1'
    >>> parse_fca_release_filename("sep2014.ZIP").label
    '2014Q3'
    >>> parse_fca_release_filename("Mar2020.zip") is None
    True
    >>> parse_fca_release_filename("instructions.pdf") is None
    True
    """
    match = _RELEASE_FILENAME.fullmatch(name)
    if match is None:
        return None
    year = int(match["legacy_year"] or match["modern_year"])
    stem = name[: -len(".zip")].lower()
    if stem.startswith("sep") and not stem.startswith("sept"):
        stem = "sept" + stem.removeprefix("sep")
    for quarter in Quarter:
        period = ReportingPeriod(year=year, quarter=quarter)
        if fca_release_stem(period=period).lower() == stem:
            return period
    return None


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

    See Also
    --------
    fca_release_stem : Return the name of a period's release zip.
    parse_fca_release_filename : Turn a release zip's name back into its period.

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
            f"FCA has no release for {period.label}. Releases start at "
            f"{_FIRST_RELEASE_PERIOD.label}, and none can be later than the "
            f"current quarter, {latest.label}."
        )
    base_url = (
        _MODERN_ERA_BASE_URL
        if period.year >= _MODERN_ERA_FIRST_YEAR
        else _LEGACY_ERA_BASE_URL
    )
    return f"{base_url}{fca_release_stem(period=period)}.zip"
