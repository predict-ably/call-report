"""Tests for the collection of FCA charters (FCAInstitutionRegistry)."""

from __future__ import annotations

import datetime
import json
from typing import Any

import pandas as pd
import polars as pl
import pytest

from call_report.core import PeriodRange, ReportingPeriod
from call_report.exceptions import InstitutionError, PeriodNotAvailableError
from call_report.fca import FCAInstitution, FCAInstitutionRegistry
from tests.fca.institution_rows import FRAME_COLUMNS, ROW
from tests.helpers import as_date, rows_of

TEXAS: dict[str, Any] = {
    **ROW,
    "UNINUM": 610000,
    "SYSTEM": 6,
    "DIST": 10,
    "ASSOC": 0,
    "SHORTNAME": "FCB of Texas",
}
RENAMED = {**ROW, "SHORTNAME": "Farm Credit Mid-America ACA"}


def registry() -> FCAInstitutionRegistry:
    """Return a two-charter registry over three quarters.

    Mid-America ACA (722825) files all three quarters and is renamed in the
    last. FCB of Texas (610000) skips the middle quarter.
    """
    return FCAInstitutionRegistry.from_rosters(
        rosters=[
            ("2011-06-30", [ROW, TEXAS]),
            ("2011-09-30", [ROW]),
            ("2011-12-31", [TEXAS, RENAMED]),
        ]
    )


class TestConstruction:
    """Building a registry from charters and from rosters."""

    def test_from_institutions(self) -> None:
        """The constructor takes charters in any order and sorts by UNINUM."""
        mid = FCAInstitution.from_roster_rows(rows=[("2011-06-30", ROW)])
        texas = FCAInstitution.from_roster_rows(rows=[("2011-06-30", TEXAS)])
        built = FCAInstitutionRegistry(institutions=[mid, texas])
        assert list(built) == [610000, 722825]
        assert built[722825] is mid

    def test_duplicate_uninum_rejected(self) -> None:
        """Two charters with one UNINUM cannot share a registry."""
        mid = FCAInstitution.from_roster_rows(rows=[("2011-06-30", ROW)])
        with pytest.raises(InstitutionError, match="UNINUM 722825 appears more"):
            FCAInstitutionRegistry(institutions=[mid, mid])

    def test_empty_rejected(self) -> None:
        """A registry holds at least one charter."""
        with pytest.raises(InstitutionError, match="at least one institution"):
            FCAInstitutionRegistry(institutions=[])

    def test_from_rosters_groups_by_uninum(self) -> None:
        """Rows are grouped by UNINUM across every roster, in any order."""
        built = registry()
        assert list(built) == [610000, 722825]
        assert built[610000].periods == (
            PeriodRange(start="2011-06-30", end="2011-06-30"),
            PeriodRange(start="2011-12-31", end="2011-12-31"),
        )
        assert [v.value for v in built[722825].short_name_history] == [
            "Mid-America ACA",
            "Farm Credit Mid-America ACA",
        ]

    def test_roster_order_does_not_matter(self) -> None:
        """Supplying the rosters in reverse builds an equal registry."""
        rosters: list[tuple[str, list[dict[str, Any]]]] = [
            ("2011-06-30", [ROW, TEXAS]),
            ("2011-09-30", [ROW]),
            ("2011-12-31", [TEXAS, RENAMED]),
        ]
        assert FCAInstitutionRegistry.from_rosters(rosters=rosters[::-1]) == registry()

    def test_from_rosters_accepts_dataframes(self, backend: str) -> None:
        """A roster may be a native dataframe of any backend."""
        frame = pl.DataFrame([ROW, TEXAS])
        roster: Any = {
            "pandas": frame.to_pandas(),
            "polars": frame,
            "pyarrow": frame.to_arrow(),
        }[backend]
        built = FCAInstitutionRegistry.from_rosters(rosters=[("2011-06-30", roster)])
        assert list(built) == [610000, 722825]

    def test_duplicate_roster_period_rejected(self) -> None:
        """Two rosters for one quarter are ambiguous."""
        with pytest.raises(InstitutionError, match=r"more than one roster.*2011Q2"):
            FCAInstitutionRegistry.from_rosters(
                rosters=[("2011-06-30", [ROW]), (datetime.date(2011, 6, 30), [TEXAS])]
            )

    def test_duplicate_uninum_in_roster_rejected(self) -> None:
        """A UNINUM listed twice in one quarter names both in the error."""
        with pytest.raises(InstitutionError, match=r"UNINUM 722825.*2011Q2 roster"):
            FCAInstitutionRegistry.from_rosters(rosters=[("2011-06-30", [ROW, ROW])])

    def test_missing_column_rejected(self) -> None:
        """A roster row missing a required column is rejected before grouping."""
        row = {key: value for key, value in ROW.items() if key != "UNINUM"}
        with pytest.raises(InstitutionError, match=r"missing column.*UNINUM"):
            FCAInstitutionRegistry.from_rosters(rosters=[("2011-06-30", [row])])

    def test_mislabeled_roster_rejected(self) -> None:
        """A roster whose YEAR and MONTH name another quarter is rejected."""
        row = {**ROW, "YEAR": 2011, "MONTH": 3}
        with pytest.raises(InstitutionError, match="YEAR=2011 and MONTH=3"):
            FCAInstitutionRegistry.from_rosters(rosters=[("2011-06-30", [row])])

    @pytest.mark.parametrize(
        "rosters", [[], [("2011-06-30", [])]], ids=["no-rosters", "empty-roster"]
    )
    def test_no_rows_rejected(self, rosters: list[Any]) -> None:
        """Rosters with no rows cannot build a registry."""
        with pytest.raises(InstitutionError, match="hold no rows"):
            FCAInstitutionRegistry.from_rosters(rosters=rosters)


class TestFromDataframe:
    """Building a registry from one stacked roster frame."""

    def test_round_trip(self, backend: str) -> None:
        """A registry rebuilds from its own frame, most_recent_* ignored."""
        built = registry()
        assert FCAInstitutionRegistry.from_dataframe(data=built.to_dataframe()) == built

    def test_lazy_frame(self, lazy_polars_backend: str) -> None:
        """A lazy frame is collected rather than rejected."""
        built = registry()
        frame = built.to_dataframe()
        assert isinstance(frame, pl.LazyFrame)
        assert FCAInstitutionRegistry.from_dataframe(data=frame) == built

    def test_stacked_roster_with_month_and_year(self) -> None:
        """The load_institutions shape, with MONTH and YEAR, is accepted."""
        frame = pl.DataFrame(
            [
                {**ROW, "MONTH": 6, "YEAR": 2011, "period": datetime.date(2011, 6, 30)},
                {**ROW, "MONTH": 9, "YEAR": 2011, "period": datetime.date(2011, 9, 30)},
            ]
        )
        built = FCAInstitutionRegistry.from_dataframe(data=frame)
        assert built[722825].periods == (
            PeriodRange(start="2011-06-30", end="2011-09-30"),
        )

    def test_missing_period_rejected(self) -> None:
        """Without a period column the rows cannot be placed in quarters."""
        with pytest.raises(InstitutionError, match="no 'period' column"):
            FCAInstitutionRegistry.from_dataframe(data=pl.DataFrame([ROW]))


class TestAccessors:
    """Mapping behaviour, period bounds, and quarter snapshots."""

    def test_mapping(self) -> None:
        """The registry behaves as a read-only mapping of UNINUM to charter."""
        built = registry()
        assert len(built) == 2
        assert 722825 in built
        assert 999999 not in built
        assert built[722825].most_recent_short_name == "Farm Credit Mid-America ACA"

    def test_unknown_uninum(self) -> None:
        """An unknown UNINUM raises KeyError naming it."""
        with pytest.raises(KeyError, match="UNINUM 999999 is not in the registry"):
            registry()[999999]

    def test_period_bounds(self) -> None:
        """The bounds cover every charter's quarters."""
        built = registry()
        assert built.first_period == ReportingPeriod.from_period_end(value="2011-06-30")
        assert built.last_period == ReportingPeriod.from_period_end(value="2011-12-31")

    def test_as_of(self) -> None:
        """A quarter's snapshot holds only charters that filed that quarter."""
        snapshots = registry().as_of(period="2011-09-30")
        assert list(snapshots) == [722825]
        assert snapshots[722825].short_name == "Mid-America ACA"

    def test_as_of_is_read_only(self) -> None:
        """The snapshot mapping cannot be modified."""
        snapshots: Any = registry().as_of(period="2011-12-31")
        with pytest.raises(TypeError):
            snapshots[1] = None

    def test_as_of_unfiled_quarter_rejected(self) -> None:
        """A quarter no charter filed raises PeriodNotAvailableError."""
        with pytest.raises(PeriodNotAvailableError, match=r"no institution.*2012Q1"):
            registry().as_of(period="2012-03-31")

    def test_repr(self) -> None:
        """The repr names the size and span."""
        assert repr(registry()) == (
            "FCAInstitutionRegistry(institutions=2, "
            "first_period=2011Q2, last_period=2011Q4)"
        )


class TestToDataframe:
    """The stacked frame and its one-row-per-charter form."""

    def test_rows(self, backend: str) -> None:
        """One row per charter per quarter filed, ordered by UNINUM then period."""
        rows = rows_of(registry().to_dataframe())
        assert list(rows[0]) == FRAME_COLUMNS
        assert [(row["UNINUM"], as_date(row["period"])) for row in rows] == [
            (610000, datetime.date(2011, 6, 30)),
            (610000, datetime.date(2011, 12, 31)),
            (722825, datetime.date(2011, 6, 30)),
            (722825, datetime.date(2011, 9, 30)),
            (722825, datetime.date(2011, 12, 31)),
        ]
        assert [row["SHORTNAME"] for row in rows[2:]] == [
            "Mid-America ACA",
            "Mid-America ACA",
            "Farm Credit Mid-America ACA",
        ]

    def test_latest_only(self, backend: str) -> None:
        """latest_only keeps each charter's last quarter, one row per charter."""
        rows = rows_of(registry().to_dataframe(latest_only=True))
        assert [
            (row["UNINUM"], as_date(row["period"]), row["SHORTNAME"]) for row in rows
        ] == [
            (610000, datetime.date(2011, 12, 31), "FCB of Texas"),
            (722825, datetime.date(2011, 12, 31), "Farm Credit Mid-America ACA"),
        ]
        assert all(
            row["SHORTNAME"] == row["most_recent_short_name"]
            and row["ZIP"] == row["most_recent_zip"]
            for row in rows
        )

    def test_dataframe_type(self, polars_backend: str) -> None:
        """dataframe_type converts the result whatever the backend."""
        frame = registry().to_dataframe(dataframe_type="pandas")
        assert isinstance(frame, pd.DataFrame)


class TestJson:
    """The JSON round trip."""

    def test_round_trip(self) -> None:
        """to_json then from_json gives back an equal registry."""
        built = registry()
        assert FCAInstitutionRegistry.from_json(text=built.to_json()) == built

    def test_shape(self) -> None:
        """Charters are stored in UNINUM order under 'institutions'."""
        payload = json.loads(registry().to_json(indent=None))
        assert [item["uninum"] for item in payload["institutions"]] == [610000, 722825]

    @pytest.mark.parametrize(
        ("text", "match"),
        [
            ("{not json", "is not valid JSON"),
            ("[]", "expected a JSON object, got list"),
            ("{}", "expected an 'institutions' list"),
            ('{"institutions": {}}', "expected an 'institutions' list"),
            ('{"institutions": [{"uninum": 1}]}', "malformed institution JSON"),
        ],
        ids=["invalid", "not-object", "missing-key", "not-list", "bad-institution"],
    )
    def test_malformed_rejected(self, text: str, match: str) -> None:
        """Anything other than to_json's shape raises InstitutionError."""
        with pytest.raises(InstitutionError, match=match):
            FCAInstitutionRegistry.from_json(text=text)
