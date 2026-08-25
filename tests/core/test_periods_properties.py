"""Property-based tests for the period arithmetic in call_report.core.

``tests/core/test_periods.py`` covers `ReportingPeriod` and `PeriodRange`
with hand-picked examples: the four quarter ends, a couple of year
rollovers, the 2014/2015 FCA URL-era boundary. Those examples are chosen
well, but they are finite, and they were chosen by someone who already
believed the code was right.

The tests here assert the same objects' *laws* instead, and let hypothesis
search for a counterexample. A law is the thing that has to hold for every
period, not just the ones a person thought to write down:
round-tripping through `period_end` is the identity, `next` and `previous`
are inverses, shifting is additive, and a `PeriodRange` is exactly as long
as the quarter distance between its bounds.

Years are bounded to 1600-2400. `ReportingPeriod` itself has no such
limit, but `period_end` builds a `datetime.date`, which is only defined
for years 1-9999, and a shift of a few hundred quarters from an unbounded
year would leave that range for reasons that say nothing about this
package's logic.
"""

from __future__ import annotations

from datetime import date, timedelta
from itertools import pairwise

from hypothesis import assume, given
from hypothesis import strategies as st

from call_report.core import PeriodRange, Quarter, ReportingPeriod
from call_report.exceptions import InvalidPeriodError

MIN_YEAR = 1600
MAX_YEAR = 2400

# Wide enough to cross many year boundaries in both directions, small
# enough that a shift from any in-range year stays inside MIN/MAX_YEAR.
MAX_SHIFT_QUARTERS = 200

years = st.integers(min_value=MIN_YEAR, max_value=MAX_YEAR)
quarters = st.sampled_from(list(Quarter))
shifts = st.integers(min_value=-MAX_SHIFT_QUARTERS, max_value=MAX_SHIFT_QUARTERS)


@st.composite
def reporting_periods(draw: st.DrawFn) -> ReportingPeriod:
    """Draw an arbitrary ReportingPeriod within the supported year range."""
    return ReportingPeriod(year=draw(years), quarter=draw(quarters))


def _in_range(period: ReportingPeriod) -> bool:
    """Return whether a period's year is still inside the tested bounds."""
    return MIN_YEAR <= period.year <= MAX_YEAR


def _is_last_day_of_month(value: date) -> bool:
    """Return whether a date is the final day of its own calendar month."""
    return (value + timedelta(days=1)).month != value.month


# ---------------------------------------------------------------------------
# ReportingPeriod
# ---------------------------------------------------------------------------


@given(period=reporting_periods())
def test_period_end_round_trips_for_every_period(period: ReportingPeriod) -> None:
    """from_period_end(p.period_end) reconstructs p, for any p."""
    assert ReportingPeriod.from_period_end(value=period.period_end) == period


@given(period=reporting_periods())
def test_period_end_is_always_a_real_quarter_end_date(
    period: ReportingPeriod,
) -> None:
    """Every period's period_end falls on the last day of a quarter-end month."""
    period_end = period.period_end
    assert period_end.month in {3, 6, 9, 12}
    assert _is_last_day_of_month(period_end)


@given(period=reporting_periods(), n=shifts)
def test_next_and_previous_are_inverses(period: ReportingPeriod, n: int) -> None:
    """Shifting forward n quarters then back n quarters is the identity."""
    assume(_in_range(period.next(n=n)))
    assert period.next(n=n).previous(n=n) == period


@given(period=reporting_periods(), a=shifts, b=shifts)
def test_shifting_is_additive(period: ReportingPeriod, a: int, b: int) -> None:
    """next(a) then next(b) lands on the same period as next(a + b)."""
    assume(_in_range(period.next(n=a)))
    assume(_in_range(period.next(n=a + b)))
    assert period.next(n=a).next(n=b) == period.next(n=a + b)


@given(period=reporting_periods(), n=st.integers(min_value=1, max_value=200))
def test_next_moves_strictly_forward_in_time(period: ReportingPeriod, n: int) -> None:
    """A positive shift always produces a strictly later period."""
    shifted = period.next(n=n)
    assume(_in_range(shifted))
    assert shifted > period
    assert shifted.period_end > period.period_end


@given(period=reporting_periods())
def test_shifting_by_zero_is_the_identity(period: ReportingPeriod) -> None:
    """next(0) and previous(0) both leave the period unchanged."""
    assert period.next(n=0) == period
    assert period.previous(n=0) == period


@given(period=reporting_periods())
def test_ordering_agrees_with_calendar_ordering(period: ReportingPeriod) -> None:
    """Comparing two periods agrees with comparing their quarter-end dates."""
    later = period.next(n=1)
    assume(_in_range(later))
    assert (period < later) == (period.period_end < later.period_end)


@given(year=years, quarter=quarters)
def test_label_is_year_and_quarter_number(year: int, quarter: Quarter) -> None:
    """Label is always the four-digit year followed by Q and the quarter."""
    period = ReportingPeriod(year=year, quarter=quarter)
    assert period.label == f"{year}Q{quarter.value}"


# ---------------------------------------------------------------------------
# Quarter
# ---------------------------------------------------------------------------


@given(quarter=quarters)
def test_quarter_months_are_three_consecutive_months(quarter: Quarter) -> None:
    """Months is first_month and the two months after it, ending at last_month."""
    months = quarter.months
    assert months == (quarter.first_month, quarter.first_month + 1, quarter.months[2])
    assert months[2] == quarter.last_month
    assert quarter.last_month - quarter.first_month == 2


# ---------------------------------------------------------------------------
# PeriodRange
# ---------------------------------------------------------------------------


@given(start=reporting_periods(), length=st.integers(min_value=0, max_value=200))
def test_range_length_equals_quarter_distance(
    start: ReportingPeriod, length: int
) -> None:
    """A range from p to p.next(n) contains exactly n + 1 periods."""
    end = start.next(n=length)
    assume(_in_range(end))
    assert len(PeriodRange(start=start, end=end)) == length + 1


@given(start=reporting_periods(), length=st.integers(min_value=0, max_value=60))
def test_range_is_contiguous_and_ascending(start: ReportingPeriod, length: int) -> None:
    """Consecutive elements of a range differ by exactly one quarter."""
    end = start.next(n=length)
    assume(_in_range(end))
    periods = list(PeriodRange(start=start, end=end))
    assert periods[0] == start
    assert periods[-1] == end
    for earlier, later in pairwise(periods):
        assert earlier.next() == later
        assert earlier < later


@given(start=reporting_periods(), length=st.integers(min_value=0, max_value=60))
def test_every_member_of_a_range_is_contained_in_it(
    start: ReportingPeriod, length: int
) -> None:
    """Each period a range yields also satisfies `in` on that range."""
    end = start.next(n=length)
    assume(_in_range(end))
    period_range = PeriodRange(start=start, end=end)
    for period in period_range:
        assert period in period_range
    assert start.previous() not in period_range
    assert end.next() not in period_range


@given(start=reporting_periods(), length=st.integers(min_value=0, max_value=60))
def test_ranges_with_equal_bounds_are_equal(
    start: ReportingPeriod, length: int
) -> None:
    """Two ranges built from the same bounds compare equal and agree elementwise."""
    end = start.next(n=length)
    assume(_in_range(end))
    assert PeriodRange(start=start, end=end) == PeriodRange(start=start, end=end)


@given(start=reporting_periods(), length=st.integers(min_value=0, max_value=60))
def test_range_accepts_dates_and_strings_interchangeably(
    start: ReportingPeriod, length: int
) -> None:
    """Building a range from periods, dates, or ISO strings gives the same range."""
    end = start.next(n=length)
    assume(_in_range(end))
    from_periods = PeriodRange(start=start, end=end)
    from_dates = PeriodRange(start=start.period_end, end=end.period_end)
    from_strings = PeriodRange(
        start=start.period_end.isoformat(), end=end.period_end.isoformat()
    )
    assert from_periods == from_dates == from_strings


@given(start=reporting_periods(), length=st.integers(min_value=0, max_value=60))
def test_indexing_agrees_with_iteration(start: ReportingPeriod, length: int) -> None:
    """Positive and negative indexing return the same periods iteration does."""
    end = start.next(n=length)
    assume(_in_range(end))
    period_range = PeriodRange(start=start, end=end)
    periods = list(period_range)
    for index, period in enumerate(periods):
        assert period_range[index] == period
        assert period_range[index - len(periods)] == period


@given(value=st.dates(min_value=date(MIN_YEAR, 1, 1), max_value=date(MAX_YEAR, 12, 31)))
def test_only_real_quarter_ends_are_accepted(value: date) -> None:
    """A date is accepted exactly when it is the last day of a quarter month."""
    is_quarter_end = value.month in {3, 6, 9, 12} and _is_last_day_of_month(value)
    try:
        ReportingPeriod.from_period_end(value=value)
    except InvalidPeriodError:
        accepted = False
    else:
        accepted = True
    assert accepted == is_quarter_end
