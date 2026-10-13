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


class DomainDatasetNotFoundError(CallReportError):
    """A requested curated domain dataset is not one this package ships.

    Raised for an unrecognized domain dataset name, the curated counterpart
    to `ScheduleNotFoundError`.
    """


class LayoutParseError(CallReportError):
    """A layout or data file could not be parsed into the expected shape.

    Raised for structurally invalid layout files, data rows that cannot be
    reconciled with their layout, or incompatible schemas when stacking
    multiple periods together.
    """


class SchemaError(CallReportError):
    """A schema or field-metadata object was constructed or modified invalidly.

    Raised for malformed period spans (empty, overlapping, out of order, or
    adjacent spans that should have been merged), duplicate field names, or
    an out-of-range field-insertion index.
    """


class InstitutionError(CallReportError):
    """An institution history was constructed from invalid input.

    Raised for roster rows that cannot describe one UNINUM (no rows, a
    repeated quarter, a missing column, or more than one UNINUM), for
    histories that do not cover the quarters the UNINUM was reported, and
    for malformed institution JSON.
    """


class DownloadError(CallReportError):
    """A release's files could not be resolved or retrieved.

    Raised when a transport cannot locate or fetch the files for a period,
    e.g. a missing local directory or a failed network request.
    """


class ReleaseCatalogError(DownloadError, ValueError):
    """A list of releases could not be read.

    Raised for a mirror manifest or a download page whose text cannot be
    read as a list of releases, such as invalid JSON, a missing key, or a
    page with no release links. It is a `DownloadError`, so code that
    catches failed downloads also catches it. It is also a `ValueError`,
    since the problem is the text passed in.
    """


class ZipArchiveUnpackingError(CallReportError):
    """A zip archive could not be unpacked.

    Raised for a file that is not a zip, a zip with a corrupt member, or a
    member whose path would land outside the target folder.
    """


class InsecureConnectionWarning(UserWarning):
    """A request was made over plain HTTP to a host that is not this machine.

    The request still goes ahead. Plain HTTP is neither private nor
    protected against tampering, so prefer an ``https`` URL.
    """


class ReshapeError(CallReportError):
    """Loaded schedule data could not be reshaped into the requested layout.

    Raised for a non-unique pivot grain (the same row would need to
    contribute more than one value to a single output cell) or any other
    failure while reshaping long- or wide-format data, regardless of
    which dataframe backend produced the underlying error.
    """
