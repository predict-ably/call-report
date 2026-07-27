"""Pure FCA Call Report download-URL catalog.

FCA's own publication page is
`<https://www.fca.gov/bank-oversight/call-report-data-for-download>`_.
"""

from __future__ import annotations

from call_report.exceptions import PeriodNotAvailableError
from call_report.types import ReportingPeriod

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

EARLIEST_PERIOD = ReportingPeriod.from_period_end(value="2000-03-31")
"""ReportingPeriod: The earliest quarter FCA is known to publish."""

LATEST_KNOWN_PERIOD = ReportingPeriod.from_period_end(value="2026-03-31")
"""ReportingPeriod: The latest quarter confirmed available as of this release.

This is a point-in-time snapshot, not a live lookup -- it will need bumping
as FCA publishes new quarters, until a future release adds live discovery.
"""


def construct_fca_download_url(*, period: ReportingPeriod) -> str:
    """Construct the FCA download URL for a period's Call Report zip archive.

    FCA changed its URL and filename convention between the legacy and
    modern eras: periods from 2000 through 2014 use an abbreviated month
    name under ``/template-fca/download/CallReportData/``; periods from
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
        If `period` is outside FCA's known-published range
        (`EARLIEST_PERIOD` through `LATEST_KNOWN_PERIOD`).

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
    if period < EARLIEST_PERIOD or period > LATEST_KNOWN_PERIOD:
        raise PeriodNotAvailableError(
            f"FCA does not publish {period.label}; known-available periods run from "
            f"{EARLIEST_PERIOD.period_end.isoformat()} through "
            f"{LATEST_KNOWN_PERIOD.period_end.isoformat()}."
        )

    if period.year >= _MODERN_ERA_FIRST_YEAR:
        month_name = _MONTH_NAME_BY_QUARTER_END_MONTH[period.month]
        return f"{_MODERN_ERA_BASE_URL}{period.year}{month_name}.zip"

    abbreviation = _LEGACY_ABBREVIATION_BY_QUARTER_END_MONTH[period.month]
    return f"{_LEGACY_ERA_BASE_URL}{abbreviation}{period.year}.zip"
