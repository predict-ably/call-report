"""Tests for the source-agnostic period vocabulary (call_report.core)."""

from __future__ import annotations

from datetime import date

import pytest

from call_report.core import PeriodRange, Quarter, ReportingPeriod
from call_report.exceptions import InvalidPeriodError

VALID_QUARTER_ENDS = [
    ("2026-03-31", 2026, Quarter.Q1, 3),
    ("2026-06-30", 2026, Quarter.Q2, 6),
    ("2026-09-30", 2026, Quarter.Q3, 9),
    ("2026-12-31", 2026, Quarter.Q4, 12),
]


@pytest.mark.parametrize(("value", "year", "quarter", "month"), VALID_QUARTER_ENDS)
def test_from_period_end_accepts_all_four_valid_endings(
    value: str, year: int, quarter: Quarter, month: int
) -> None:
    """Each of the four calendar quarter-end dates is accepted."""
    period = ReportingPeriod.from_period_end(value=value)
    assert period.year == year
    assert period.quarter == quarter
    assert period.month == month
    assert period.period_end == date(year, month, [31, 30, 30, 31][quarter.value - 1])


def test_from_period_end_accepts_date_object() -> None:
    """A datetime.date is accepted equally to an ISO string."""
    from_string = ReportingPeriod.from_period_end(value="2026-03-31")
    from_date = ReportingPeriod.from_period_end(value=date(2026, 3, 31))
    assert from_string == from_date


@pytest.mark.parametrize(
    "value",
    [
        "2026-05-15",  # not a quarter end at all
        "2026-06-31",  # June has 30 days -- not a valid calendar date
        "2026-12-30",  # one day short of an actual quarter end
        "",  # empty string
        "not-a-date",  # malformed
        "2026-13-31",  # invalid month
    ],
)
def test_from_period_end_rejects_invalid_values(value: str) -> None:
    """Anything that isn't a real calendar quarter-end date is rejected."""
    with pytest.raises(InvalidPeriodError) as exc_info:
        ReportingPeriod.from_period_end(value=value)
    message = str(exc_info.value)
    # The error must name all four valid endings so the fix is obvious.
    for ending in ("03-31", "06-30", "09-30", "12-31"):
        assert ending in message


def test_period_end_roundtrip() -> None:
    """from_period_end(p.period_end) reconstructs an equal ReportingPeriod."""
    for value in ("2000-03-31", "2014-12-31", "2015-03-31", "2026-03-31"):
        period = ReportingPeriod.from_period_end(value=value)
        assert ReportingPeriod.from_period_end(value=period.period_end) == period


def test_reporting_period_label() -> None:
    """Label is a short, human-readable identifier."""
    period = ReportingPeriod.from_period_end(value="2026-03-31")
    assert period.label == "2026Q1"


@pytest.mark.parametrize(
    ("start", "n", "expected"),
    [
        ("2025-12-31", 1, "2026-03-31"),  # rolls over a year boundary
        ("2026-03-31", 1, "2026-06-30"),
        ("2025-06-30", 4, "2026-06-30"),  # rolls forward a full year
        ("2024-12-31", 8, "2026-12-31"),  # rolls across two year boundaries
    ],
)
def test_next_rolls_over_years(start: str, n: int, expected: str) -> None:
    """next(n=...) correctly rolls the quarter and year forward."""
    period = ReportingPeriod.from_period_end(value=start)
    assert period.next(n=n) == ReportingPeriod.from_period_end(value=expected)


@pytest.mark.parametrize(
    ("start", "n", "expected"),
    [
        ("2026-03-31", 1, "2025-12-31"),  # rolls back over a year boundary
        ("2026-06-30", 1, "2026-03-31"),
        ("2026-06-30", 4, "2025-06-30"),
        ("2026-12-31", 8, "2024-12-31"),  # rolls back across two year boundaries
    ],
)
def test_previous_rolls_over_years(start: str, n: int, expected: str) -> None:
    """previous(n=...) correctly rolls the quarter and year backward."""
    period = ReportingPeriod.from_period_end(value=start)
    assert period.previous(n=n) == ReportingPeriod.from_period_end(value=expected)


def test_next_and_previous_are_inverses() -> None:
    """next() followed by previous() returns to the starting period."""
    period = ReportingPeriod.from_period_end(value="2026-03-31")
    assert period.next(n=3).previous(n=3) == period


def test_reporting_period_ordering_and_hashing() -> None:
    """ReportingPeriod is fully ordered and hashable."""
    q1 = ReportingPeriod.from_period_end(value="2026-03-31")
    q2 = ReportingPeriod.from_period_end(value="2026-06-30")
    prior_year_q4 = ReportingPeriod.from_period_end(value="2025-12-31")
    assert prior_year_q4 < q1 < q2
    assert sorted([q2, q1, prior_year_q4]) == [prior_year_q4, q1, q2]
    assert len({q1, q1, q2}) == 2


def test_reporting_period_is_keyword_only() -> None:
    """The ReportingPeriod dataclass constructor takes no positional args."""
    with pytest.raises(TypeError):
        ReportingPeriod(2026, Quarter.Q1)  # type: ignore[call-arg]


def test_from_period_end_is_keyword_only() -> None:
    """from_period_end takes no positional args."""
    with pytest.raises(TypeError):
        ReportingPeriod.from_period_end("2026-03-31")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# PeriodRange
# ---------------------------------------------------------------------------


def test_period_range_requires_both_start_and_end() -> None:
    """Both start and end must be supplied -- neither has a default."""
    with pytest.raises(TypeError):
        PeriodRange(start="2026-03-31")  # type: ignore[call-arg]


def test_period_range_end_before_start_rejected() -> None:
    """A range where end predates start is rejected with a helpful message."""
    with pytest.raises(InvalidPeriodError, match="before"):
        PeriodRange(start="2026-03-31", end="2025-12-31")


def test_period_range_start_equal_end_yields_one_quarter() -> None:
    """Start == end produces a single-period range."""
    period_range = PeriodRange(start="2026-03-31", end="2026-03-31")
    assert len(period_range) == 1
    assert period_range[0] == ReportingPeriod.from_period_end(value="2026-03-31")


def test_period_range_is_inclusive_of_both_endpoints() -> None:
    """Both the start and end quarters are included in the range."""
    period_range = PeriodRange(start="2025-09-30", end="2026-03-31")
    assert list(period_range) == [
        ReportingPeriod.from_period_end(value="2025-09-30"),
        ReportingPeriod.from_period_end(value="2025-12-31"),
        ReportingPeriod.from_period_end(value="2026-03-31"),
    ]


def test_period_range_spans_year_boundary() -> None:
    """A range crossing a calendar year boundary enumerates correctly."""
    period_range = PeriodRange(start="2025-06-30", end="2026-06-30")
    assert len(period_range) == 5
    assert period_range[0].year == 2025
    assert period_range[-1].year == 2026


def test_period_range_spans_2014_2015_url_era_boundary() -> None:
    """A range crossing the FCA URL-era boundary (2014/2015) enumerates correctly."""
    period_range = PeriodRange(start="2014-06-30", end="2015-06-30")
    assert len(period_range) == 5
    labels = [p.label for p in period_range]
    assert labels == ["2014Q2", "2014Q3", "2014Q4", "2015Q1", "2015Q2"]


def test_period_range_accepts_date_objects_and_reporting_periods() -> None:
    """start/end accept ISO strings, date objects, or ReportingPeriod instances."""
    from_strings = PeriodRange(start="2025-09-30", end="2026-03-31")
    from_dates = PeriodRange(start=date(2025, 9, 30), end=date(2026, 3, 31))
    from_periods = PeriodRange(
        start=ReportingPeriod.from_period_end(value="2025-09-30"),
        end=ReportingPeriod.from_period_end(value="2026-03-31"),
    )
    assert list(from_strings) == list(from_dates) == list(from_periods)


def test_period_range_indexing_and_slicing() -> None:
    """PeriodRange supports int indexing, negative indexing, and slicing."""
    period_range = PeriodRange(start="2025-03-31", end="2026-03-31")
    assert period_range[0] == ReportingPeriod.from_period_end(value="2025-03-31")
    assert period_range[-1] == ReportingPeriod.from_period_end(value="2026-03-31")
    sliced = period_range[1:3]
    assert isinstance(sliced, PeriodRange)
    assert list(sliced) == [
        ReportingPeriod.from_period_end(value="2025-06-30"),
        ReportingPeriod.from_period_end(value="2025-09-30"),
    ]


def test_period_range_containment() -> None:
    """`in` checks membership by ReportingPeriod equality."""
    period_range = PeriodRange(start="2025-03-31", end="2026-03-31")
    assert ReportingPeriod.from_period_end(value="2025-09-30") in period_range
    assert ReportingPeriod.from_period_end(value="2024-12-31") not in period_range


def test_period_range_equality() -> None:
    """Two PeriodRanges with the same bounds are equal."""
    first = PeriodRange(start="2025-03-31", end="2026-03-31")
    second = PeriodRange(start="2025-03-31", end="2026-03-31")
    third = PeriodRange(start="2025-03-31", end="2025-12-31")
    assert first == second
    assert first != third


def test_period_range_is_keyword_only() -> None:
    """PeriodRange takes no positional arguments."""
    with pytest.raises(TypeError):
        PeriodRange("2025-03-31", "2026-03-31")  # type: ignore[call-arg]


def test_from_period_end_rejects_non_string_non_date() -> None:
    """A value that is neither a string nor a date is rejected."""
    with pytest.raises(InvalidPeriodError, match=r"string or datetime\.date"):
        ReportingPeriod.from_period_end(value=20260331)  # type: ignore[arg-type]


def test_period_range_stepped_slice_returns_tuple() -> None:
    """A stepped slice returns a plain tuple, not a contiguous PeriodRange."""
    period_range = PeriodRange(start="2025-03-31", end="2026-03-31")
    stepped = period_range[::2]
    assert isinstance(stepped, tuple)
    assert stepped == (
        ReportingPeriod.from_period_end(value="2025-03-31"),
        ReportingPeriod.from_period_end(value="2025-09-30"),
        ReportingPeriod.from_period_end(value="2026-03-31"),
    )


def test_period_range_equality_with_non_range_is_false() -> None:
    """Comparing a PeriodRange to a non-PeriodRange is False, not an error."""
    period_range = PeriodRange(start="2025-03-31", end="2026-03-31")
    assert period_range != 42
    assert (period_range == "not a range") is False


def test_period_range_repr() -> None:
    """Repr shows the range's start and end labels."""
    period_range = PeriodRange(start="2025-09-30", end="2026-03-31")
    assert repr(period_range) == "PeriodRange(start='2025Q3', end='2026Q1')"
