"""Exception hierarchy shared by every call report source."""

from __future__ import annotations


class CallReportError(Exception):
    """Root of every domain-specific error raised by call_report.

    Catching this one exception type is enough to catch any error this
    package raises, regardless of which specific subclass was involved.

    Examples
    --------
    >>> try:
    ...     raise CallReportError("something went wrong")
    ... except CallReportError as error:
    ...     str(error)
    'something went wrong'
    """


class InvalidPeriodError(CallReportError):
    """A supplied period value is malformed or not a real calendar quarter end.

    Raised for request-shape problems such as an unparsable date string or a
    date that does not fall on a calendar quarter end.
    """


class PeriodNotAvailableError(CallReportError):
    """A period is a valid quarter end but the source does not publish it.

    Raised when a period is outside the range a specific source is known to
    have published, e.g. before FCA's earliest release.
    """


class ScheduleNotFoundError(CallReportError):
    """A requested schedule is unknown, or absent from every requested period.

    Raised for an unrecognized schedule name, or when a valid schedule has
    zero surviving periods after resolution.
    """


class LayoutParseError(CallReportError):
    """A layout or data file could not be parsed into the expected shape.

    Raised for structurally invalid layout files, data rows that cannot be
    reconciled with their layout, or incompatible schemas when stacking
    multiple periods together.
    """


class DownloadError(CallReportError):
    """A release's files could not be resolved or retrieved.

    Raised when a transport cannot locate or fetch the files for a period,
    e.g. a missing local directory or a failed network request.
    """
