"""Source-agnostic reporting-period vocabulary: ReportingPeriod and PeriodRange.

Every call report source is keyed by calendar quarter. Callers work with
plain quarter-end dates (``"2026-03-31"``); :class:`ReportingPeriod` is the
validated, internal vocabulary those dates are converted into.
"""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from typing import overload

from call_report.exceptions import InvalidPeriodError
from call_report.types._enums import Quarter

_QUARTER_BY_END_MONTH: dict[int, Quarter] = {
    quarter.last_month: quarter for quarter in Quarter
}
_VALID_ENDINGS = "03-31, 06-30, 09-30, or 12-31"


def _last_day_of_month(*, year: int, month: int) -> int:
    """Return the actual last day of a given month.

    Delegates to :func:`calendar.monthrange` so leap-year Februaries are
    handled correctly, even though none of this module's quarter-end months
    (3, 6, 9, 12) are ever affected by that.

    Parameters
    ----------
    year : int
        The four-digit calendar year.
    month : int
        The calendar month, 1-12.

    Returns
    -------
    int
        The last day of `month` in `year`.
    """
    return monthrange(year, month)[1]


@dataclass(frozen=True, order=True, kw_only=True)
class ReportingPeriod:
    """A single calendar-quarter reporting period.

    Instances are normally created via :meth:`from_period_end` rather than
    the constructor directly.

    Attributes
    ----------
    year : int
        The four-digit calendar year.
    quarter : Quarter
        The calendar quarter.

    Examples
    --------
    >>> period = ReportingPeriod.from_period_end(value="2026-03-31")
    >>> period.label
    '2026Q1'
    """

    year: int
    quarter: Quarter

    @classmethod
    def from_period_end(cls, *, value: str | date) -> ReportingPeriod:
        """Build a ReportingPeriod from a quarter-end date.

        This is the primary, validated entry point for constructing a
        ReportingPeriod from user-supplied input.

        Parameters
        ----------
        value : str or datetime.date
            An ISO ``"YYYY-MM-DD"`` string or a `datetime.date`, naming a
            real calendar quarter-end date.

        Returns
        -------
        ReportingPeriod
            The period ending on `value`.

        Raises
        ------
        InvalidPeriodError
            If `value` cannot be parsed as a date, or is a valid calendar
            date that does not fall on 03-31, 06-30, 09-30, or 12-31.

        Examples
        --------
        >>> ReportingPeriod.from_period_end(value="2026-03-31").label
        '2026Q1'
        """
        parsed = _parse_period_end(value)
        quarter = _QUARTER_BY_END_MONTH.get(parsed.month)
        last_day = _last_day_of_month(year=parsed.year, month=parsed.month)
        if quarter is None or parsed.day != last_day:
            raise InvalidPeriodError(
                f"{value!r} is not a calendar quarter-end date; valid endings are "
                f"{_VALID_ENDINGS} (as YYYY-MM-DD)."
            )
        return cls(year=parsed.year, quarter=quarter)

    @property
    def month(self) -> int:
        """Return the quarter-end month.

        This is a convenience alias for ``self.quarter.last_month``.

        Returns
        -------
        int
            3, 6, 9, or 12.

        Examples
        --------
        >>> ReportingPeriod.from_period_end(value="2026-03-31").month
        3
        """
        return self.quarter.last_month

    @property
    def period_end(self) -> date:
        """Return the quarter-end calendar date.

        This is the inverse of :meth:`from_period_end`: round-tripping a
        period through `period_end` and back reproduces the same period.

        Returns
        -------
        datetime.date
            The last calendar day of this period's quarter.

        Examples
        --------
        >>> ReportingPeriod.from_period_end(value="2026-03-31").period_end
        datetime.date(2026, 3, 31)
        """
        month = self.month
        return date(self.year, month, _last_day_of_month(year=self.year, month=month))

    @property
    def label(self) -> str:
        """Return a short, human-readable label for this period.

        Intended for log messages, error text, and reprs -- not for parsing.

        Returns
        -------
        str
            A label of the form ``"YYYYQn"``, e.g. ``"2026Q1"``.

        Examples
        --------
        >>> ReportingPeriod.from_period_end(value="2026-03-31").label
        '2026Q1'
        """
        return f"{self.year}Q{self.quarter.value}"

    def next(self, *, n: int = 1) -> ReportingPeriod:
        """Return the period `n` quarters after this one.

        Advancing past December rolls over into Q1 of the following year.

        Parameters
        ----------
        n : int, default 1
            Number of quarters to advance. May be negative.

        Returns
        -------
        ReportingPeriod
            The resulting period, with year rollover handled automatically.

        Examples
        --------
        >>> ReportingPeriod.from_period_end(value="2025-12-31").next().label
        '2026Q1'
        """
        return _shift(self, n)

    def previous(self, *, n: int = 1) -> ReportingPeriod:
        """Return the period `n` quarters before this one.

        Going back past Q1 rolls over into Q4 of the preceding year.

        Parameters
        ----------
        n : int, default 1
            Number of quarters to go back. May be negative.

        Returns
        -------
        ReportingPeriod
            The resulting period, with year rollover handled automatically.

        Examples
        --------
        >>> ReportingPeriod.from_period_end(value="2026-03-31").previous().label
        '2025Q4'
        """
        return _shift(self, -n)


def _parse_period_end(value: str | date) -> date:
    """Parse a quarter-end value into a plain `datetime.date`.

    This only parses the value into a date; it does not check that the
    date actually falls on a calendar quarter end (see
    :meth:`ReportingPeriod.from_period_end` for that check).

    Parameters
    ----------
    value : str or datetime.date
        An ISO ``"YYYY-MM-DD"`` string or a `datetime.date`.

    Returns
    -------
    datetime.date
        The parsed date.

    Raises
    ------
    InvalidPeriodError
        If `value` is neither a `datetime.date` nor a parsable ISO date
        string.
    """
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise InvalidPeriodError(
                f"{value!r} is not a valid ISO date string (YYYY-MM-DD); valid quarter "
                f"endings are {_VALID_ENDINGS}."
            ) from exc
    raise InvalidPeriodError(
        f"period end must be a string or datetime.date, got {type(value).__name__}."
    )


def _shift(period: ReportingPeriod, delta_quarters: int) -> ReportingPeriod:
    """Return a period shifted by a number of quarters, rolling years over.

    Shared implementation backing both `ReportingPeriod.next` and
    `ReportingPeriod.previous`.

    Parameters
    ----------
    period : ReportingPeriod
        The period to shift from.
    delta_quarters : int
        Number of quarters to shift by; negative shifts backward.

    Returns
    -------
    ReportingPeriod
        The shifted period.
    """
    absolute = period.year * 4 + (period.quarter.value - 1) + delta_quarters
    year, zero_based_quarter = divmod(absolute, 4)
    return ReportingPeriod(year=year, quarter=Quarter(zero_based_quarter + 1))


class PeriodRange(Sequence[ReportingPeriod]):
    """An inclusive, contiguous sequence of quarterly ReportingPeriods.

    Both `start` and `end` must always be supplied explicitly -- unlike the
    estimator-style entry points, there is no single-quarter convenience
    default, so a range's bounds are never ambiguous.

    Parameters
    ----------
    start : str, datetime.date, or ReportingPeriod
        The first quarter-end in the range.
    end : str, datetime.date, or ReportingPeriod
        The last quarter-end in the range (inclusive).

    Raises
    ------
    InvalidPeriodError
        If either bound is not a valid quarter-end date, or if `end` is
        before `start`.

    Examples
    --------
    >>> period_range = PeriodRange(start="2025-09-30", end="2026-03-31")
    >>> [period.label for period in period_range]
    ['2025Q3', '2025Q4', '2026Q1']
    """

    def __init__(
        self,
        *,
        start: str | date | ReportingPeriod,
        end: str | date | ReportingPeriod,
    ) -> None:
        start_period = (
            start
            if isinstance(start, ReportingPeriod)
            else ReportingPeriod.from_period_end(value=start)
        )
        end_period = (
            end
            if isinstance(end, ReportingPeriod)
            else ReportingPeriod.from_period_end(value=end)
        )
        if end_period < start_period:
            raise InvalidPeriodError(
                f"end ({end_period.label}) is before start ({start_period.label})."
            )
        self._start = start_period
        self._end = end_period

        periods = []
        current = start_period
        while current <= end_period:
            periods.append(current)
            current = current.next()
        self._periods: tuple[ReportingPeriod, ...] = tuple(periods)

    def __len__(self) -> int:
        """Return the number of quarters in the range.

        Implements the `len()` builtin for `PeriodRange`.

        Returns
        -------
        int
            The count of periods, always at least 1.
        """
        return len(self._periods)

    @overload
    def __getitem__(self, index: int) -> ReportingPeriod:  # numpydoc ignore=GL08
        ...  # pragma: no cover

    @overload
    def __getitem__(
        self, index: slice
    ) -> Sequence[ReportingPeriod]:  # numpydoc ignore=GL08
        ...  # pragma: no cover

    def __getitem__(
        self, index: int | slice
    ) -> ReportingPeriod | Sequence[ReportingPeriod]:
        """Return the period at an index, or a sub-range/tuple for a slice.

        A contiguous, step-1 slice returns a new `PeriodRange`; any other
        slice (e.g. with a step) returns a plain tuple, since a stepped
        selection of periods is not itself a contiguous range.

        Parameters
        ----------
        index : int or slice
            The position, or range of positions, to select.

        Returns
        -------
        ReportingPeriod or Sequence[ReportingPeriod]
            A single period for an int index; a `PeriodRange` for a
            contiguous slice; a plain tuple for any other slice.
        """
        if isinstance(index, slice):
            selected = self._periods[index]
            if selected and index.step in (None, 1):
                return PeriodRange(start=selected[0], end=selected[-1])
            return selected
        return self._periods[index]

    def __iter__(self) -> Iterator[ReportingPeriod]:
        """Iterate over the range's periods in chronological order.

        Implements the iteration protocol so a `PeriodRange` can be looped
        over directly, e.g. in a ``for`` statement.

        Returns
        -------
        Iterator[ReportingPeriod]
            An iterator over the periods, earliest first.
        """
        return iter(self._periods)

    def __contains__(self, item: object) -> bool:
        """Return whether a period is one of this range's periods.

        Implements the `in` operator for `PeriodRange`.

        Parameters
        ----------
        item : object
            The value to check for membership.

        Returns
        -------
        bool
            ``True`` if `item` is one of this range's periods.
        """
        return item in self._periods

    def __eq__(self, other: object) -> bool:
        """Return whether another object is a PeriodRange over the same periods.

        Two ranges are equal only if their fully-derived period sequences
        match; `NotImplemented` is returned for non-`PeriodRange` operands
        so Python can fall back to the other object's comparison logic.

        Parameters
        ----------
        other : object
            The value to compare against.

        Returns
        -------
        bool
            ``True`` if `other` is a `PeriodRange` covering exactly the same
            periods as this one.
        """
        if not isinstance(other, PeriodRange):
            return NotImplemented
        return self._periods == other._periods

    def __repr__(self) -> str:
        """Return a repr showing the range's start and end labels.

        Implements `repr()` for `PeriodRange`.

        Returns
        -------
        str
            A string of the form ``PeriodRange(start='...', end='...')``.
        """
        return f"PeriodRange(start={self._start.label!r}, end={self._end.label!r})"
