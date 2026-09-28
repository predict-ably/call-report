"""The institution registry built from every archived FCA roster.

Synthetic rosters in ``test_institution_registry.py`` cover each rule on its
own. This module builds the registry from the real ``INST`` roster in every
release in ``data/fca-call-report/`` and checks it against known facts about
the archive, including the district-25 charters that were recoded to
districts 23 and 24 in 2004 and from 2006Q1 to 2008Q2.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from call_report.core import PeriodRange
from call_report.fca import FCACallReport, FCAInstitutionRegistry
from call_report.fca.catalog import EARLIEST_PERIOD, LATEST_KNOWN_PERIOD
from call_report.fca.transport import PackagedArchiveTransport
from tests.helpers import rows_of

pytestmark = pytest.mark.slow

ROSTER_COLUMNS = (
    "UNINUM",
    "SYSTEM",
    "DIST",
    "ASSOC",
    "SHORTNAME",
    "MAIL_ADDR",
    "STREET_ADDR",
    "CITY",
    "STATE",
    "ZIP",
)


@pytest.fixture(scope="module")
def stacked_roster() -> Iterator[Any]:
    """Load every archived roster once, stacked with a period column."""
    with PackagedArchiveTransport() as transport:
        report = FCACallReport(
            start=EARLIEST_PERIOD.period_end,
            end=LATEST_KNOWN_PERIOD.period_end,
            transport=transport,
        )
        yield report.load_institutions(dataframe_type="polars_dataframe")


@pytest.fixture(scope="module")
def archive_registry(stacked_roster: Any) -> FCAInstitutionRegistry:
    """Build the registry once from the stacked archive roster."""
    return FCAInstitutionRegistry.from_dataframe(data=stacked_roster)


def test_counts(archive_registry: FCAInstitutionRegistry) -> None:
    """The archive holds 344 charters across 2000Q1 to the latest release."""
    assert len(archive_registry) == 344
    assert archive_registry.first_period == EARLIEST_PERIOD
    assert archive_registry.last_period == LATEST_KNOWN_PERIOD


def test_frame_reproduces_roster(
    stacked_roster: Any, archive_registry: FCAInstitutionRegistry
) -> None:
    """The registry's frame holds exactly the archive's roster rows.

    Every one of the 9,885 roster rows comes back, with the same values in
    the same quarter, so building the registry loses nothing.
    """

    def keyed(frame: Any) -> list[tuple[Any, ...]]:
        """Return a frame's roster columns as sorted tuples."""
        return sorted(
            (row["period"], *(row[column] for column in ROSTER_COLUMNS))
            for row in rows_of(frame)
        )

    rebuilt = archive_registry.to_dataframe(dataframe_type="polars_dataframe")
    assert len(rows_of(rebuilt)) == 9885
    assert keyed(rebuilt) == keyed(stacked_roster)


def test_recoded_charter_has_gaps(archive_registry: FCAInstitutionRegistry) -> None:
    """Maine ACA files as 725008 except while recoded to 723008."""
    assert archive_registry[725008].periods == (
        PeriodRange(start="2000-03-31", end="2003-12-31"),
        PeriodRange(start="2005-03-31", end="2005-12-31"),
        PeriodRange(start="2008-09-30", end="2013-12-31"),
    )
    assert archive_registry[723008].periods == (
        PeriodRange(start="2004-03-31", end="2004-12-31"),
        PeriodRange(start="2006-03-31", end="2008-06-30"),
    )
    assert archive_registry.as_of(period="2004-06-30")[723008].short_name == (
        "Maine ACA"
    )


def test_rename(archive_registry: FCAInstitutionRegistry) -> None:
    """Mid-America ACA was renamed in 2011Q4 without changing UNINUM."""
    history = archive_registry[722825].short_name_history
    assert [(v.value, v.periods[0].label) for v in history] == [
        ("Mid-America ACA", "2000Q1"),
        ("Farm Credit Mid-America ACA", "2011Q4"),
    ]


def test_json_round_trip(archive_registry: FCAInstitutionRegistry) -> None:
    """The whole archive registry survives the JSON round trip."""
    text = archive_registry.to_json()
    assert FCAInstitutionRegistry.from_json(text=text) == archive_registry
