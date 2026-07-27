"""Shared enumerations used across call report sources."""

from __future__ import annotations

from enum import IntEnum, StrEnum


class Quarter(IntEnum):
    """A calendar quarter, ordered Q1 (Jan-Mar) through Q4 (Oct-Dec).

    Members compare and sort in calendar order, since the underlying values
    are simply 1 through 4.

    Examples
    --------
    >>> Quarter.Q1 < Quarter.Q4
    True
    """  # numpydoc ignore=PR01

    Q1 = 1
    Q2 = 2
    Q3 = 3
    Q4 = 4

    @property
    def first_month(self) -> int:
        """Return the first calendar month of this quarter.

        This is the month a quarter begins in, as a plain 1-12 integer.

        Returns
        -------
        int
            1, 4, 7, or 10.

        Examples
        --------
        >>> Quarter.Q2.first_month
        4
        """
        return (self.value - 1) * 3 + 1

    @property
    def last_month(self) -> int:
        """Return the last calendar month of this quarter.

        This is also the quarter's *quarter-end* month, the value used
        throughout this package to build quarter-end dates.

        Returns
        -------
        int
            3, 6, 9, or 12.

        Examples
        --------
        >>> Quarter.Q2.last_month
        6
        """
        return self.value * 3

    @property
    def months(self) -> tuple[int, int, int]:
        """Return the three calendar months that make up this quarter.

        Combines `first_month` with the two months that follow it.

        Returns
        -------
        tuple[int, int, int]
            The months in calendar order, e.g. ``(4, 5, 6)`` for Q2.

        Examples
        --------
        >>> Quarter.Q2.months
        (4, 5, 6)
        """
        first = self.first_month
        return (first, first + 1, first + 2)


class Source(StrEnum):
    """A regulatory call report source supported (or planned) by this package.

    Every source-specific sub-module (e.g. ``call_report.fca``) corresponds
    to exactly one member of this enum.

    Examples
    --------
    >>> Source.FCA
    <Source.FCA: 'FCA'>
    """  # numpydoc ignore=PR01

    FCA = "FCA"
    FFIEC = "FFIEC"
    FDIC = "FDIC"
    NCUA = "NCUA"


class FileKind(StrEnum):
    """The kind of file within a call report release.

    Used internally to classify each file discovered in a release directory
    as either a layout/metadata description or a raw data file.

    Examples
    --------
    >>> FileKind.METADATA
    <FileKind.METADATA: 'METADATA'>
    """  # numpydoc ignore=PR01

    METADATA = "METADATA"
    DATA = "DATA"
