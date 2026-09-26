"""Property-based tests for FCAInstitution's history building.

``tests/fca/test_institution.py`` covers hand-picked histories: a gap, a
value that changes and changes back, a missing value. The laws here must
hold for any set of quarters and any sequence of values, so hypothesis
searches for a counterexample:

- ``to_dataframe`` gives back exactly the roster values the history was
  built from, one row per quarter.
- ``as_of`` gives back each quarter's own roster values.
- ``to_json`` and ``from_json`` are inverses.
- Versions are minimal. Two adjacent versions never share a value.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from call_report.core import ReportingPeriod
from call_report.fca import FCAInstitution
from tests.helpers import as_date, rows_of

COLUMNS = ("SHORTNAME", "MAIL_ADDR", "STREET_ADDR", "CITY", "STATE", "ZIP")
STEMS = ("short_name", "mail_addr", "street_addr", "city", "state", "zip")

# A small alphabet makes repeated values, and so merged versions, likely.
values = st.sampled_from(["A", "B", None])

# Offsets from 2000Q1, spread over 40 quarters so that gaps are common.
offsets = st.sets(st.integers(min_value=0, max_value=39), min_size=1, max_size=20)

START = ReportingPeriod.from_period_end(value="2000-03-31")


@st.composite
def rosters(draw: st.DrawFn) -> list[tuple[ReportingPeriod, dict[str, Any]]]:
    """Draw one charter's roster rows over a random set of quarters."""
    rows = []
    for offset in sorted(draw(offsets)):
        row: dict[str, Any] = {"UNINUM": 722825, "SYSTEM": 7, "DIST": 22, "ASSOC": 825}
        row.update({column: draw(values) for column in COLUMNS})
        rows.append((START.next(n=offset), row))
    return rows


@given(rows=rosters())
def test_to_dataframe_reproduces_rows(
    rows: list[tuple[ReportingPeriod, dict[str, Any]]],
) -> None:
    """The tabular frame holds exactly the roster values it was built from."""
    frame_rows = rows_of(FCAInstitution.from_roster_rows(rows=rows).to_dataframe())
    assert [as_date(row["period"]) for row in frame_rows] == [
        quarter.period_end for quarter, _ in rows
    ]
    for frame_row, (_, row) in zip(frame_rows, rows, strict=True):
        assert {column: frame_row[column] for column in COLUMNS} == {
            column: row[column] for column in COLUMNS
        }


@given(rows=rosters())
def test_as_of_reproduces_each_quarter(
    rows: list[tuple[ReportingPeriod, dict[str, Any]]],
) -> None:
    """A snapshot at any filed quarter holds that quarter's roster values."""
    institution = FCAInstitution.from_roster_rows(rows=rows)
    for quarter, row in rows:
        snapshot = institution.as_of(period=quarter)
        assert tuple(getattr(snapshot, stem) for stem in STEMS) == tuple(
            row[column] for column in COLUMNS
        )


@given(rows=rosters())
def test_json_round_trip(rows: list[tuple[ReportingPeriod, dict[str, Any]]]) -> None:
    """to_json and from_json are inverses."""
    institution = FCAInstitution.from_roster_rows(rows=rows)
    assert FCAInstitution.from_json(text=institution.to_json()) == institution


@given(rows=rosters())
def test_versions_are_minimal(
    rows: list[tuple[ReportingPeriod, dict[str, Any]]],
) -> None:
    """Adjacent versions never share a value, so no history can be shortened."""
    institution = FCAInstitution.from_roster_rows(rows=rows)
    for stem in STEMS:
        history = getattr(institution, f"{stem}_history")
        for previous, version in pairwise(history):
            adjacent = version.periods[0] == previous.periods[-1].next()
            assert not (adjacent and version.value == previous.value)
