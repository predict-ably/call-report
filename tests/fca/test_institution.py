"""Tests for one FCA charter's cross-period history (FCAInstitution)."""

from __future__ import annotations

import datetime
import json
from typing import Any

import narwhals as nw
import pandas as pd
import polars as pl
import pytest

from call_report.core import PeriodRange, ReportingPeriod
from call_report.exceptions import InstitutionError, PeriodNotAvailableError
from call_report.fca import (
    FCAInstitution,
    FCAInstitutionSnapshot,
    InstitutionAttributeVersion,
)
from tests.helpers import as_date, rows_of

ROW: dict[str, Any] = {
    "UNINUM": 722825,
    "SYSTEM": 7,
    "DIST": 22,
    "ASSOC": 825,
    "SHORTNAME": "Mid-America ACA",
    "MAIL_ADDR": "P.O. Box 34390",
    "STREET_ADDR": "1601 UPS Drive",
    "CITY": "Louisville",
    "STATE": "KY",
    "ZIP": "40223-4390",
}

FRAME_COLUMNS = [
    "UNINUM",
    "period",
    "SYSTEM",
    "DIST",
    "ASSOC",
    "SHORTNAME",
    "MAIL_ADDR",
    "STREET_ADDR",
    "CITY",
    "STATE",
    "ZIP",
    "most_recent_short_name",
    "most_recent_mail_addr",
    "most_recent_street_addr",
    "most_recent_city",
    "most_recent_state",
    "most_recent_zip",
]


def period(value: str) -> ReportingPeriod:
    """Return the ReportingPeriod for a quarter-end string."""
    return ReportingPeriod.from_period_end(value=value)


def version(value: str | None, start: str, end: str) -> InstitutionAttributeVersion:
    """Return one attribute version spanning `start` to `end`."""
    return InstitutionAttributeVersion(
        value=value, periods=PeriodRange(start=start, end=end)
    )


def histories(value: str | None, start: str, end: str) -> dict[str, Any]:
    """Return every attribute history as one version holding `value`."""
    one = (version(value, start, end),)
    return {
        f"{stem}_history": one
        for stem in ("short_name", "mail_addr", "street_addr", "city", "state", "zip")
    }


def gapped() -> FCAInstitution:
    """Return a charter that files 2004Q1-2004Q2, skips 2004Q3, and returns.

    The street address changes and then changes back, and the name is the
    same on both sides of the gap.
    """
    return FCAInstitution.from_roster_rows(
        rows=[
            ("2004-03-31", ROW),
            ("2004-06-30", {**ROW, "STREET_ADDR": "PO Box 69"}),
            ("2004-12-31", ROW),
            ("2005-03-31", {**ROW, "SHORTNAME": "Farm Credit Mid-America ACA"}),
        ]
    )


class TestFromRosterRows:
    """Building a charter's history from per-quarter roster rows."""

    def test_single_row(self) -> None:
        """One row gives one span and one version per attribute."""
        institution = FCAInstitution.from_roster_rows(rows=[("2011-09-30", ROW)])
        assert institution.uninum == 722825
        assert (institution.system, institution.district, institution.association) == (
            7,
            22,
            825,
        )
        assert institution.periods == (
            PeriodRange(start="2011-09-30", end="2011-09-30"),
        )
        assert institution.short_name_history == (
            version("Mid-America ACA", "2011-09-30", "2011-09-30"),
        )

    def test_rows_in_any_order(self) -> None:
        """Rows are sorted by quarter, so input order does not matter."""
        rows = [
            ("2011-12-31", {**ROW, "SHORTNAME": "Farm Credit Mid-America ACA"}),
            ("2011-06-30", ROW),
            ("2011-09-30", ROW),
        ]
        institution = FCAInstitution.from_roster_rows(rows=rows)
        assert institution == FCAInstitution.from_roster_rows(rows=reversed(rows))
        assert institution.short_name_history == (
            version("Mid-America ACA", "2011-06-30", "2011-09-30"),
            version("Farm Credit Mid-America ACA", "2011-12-31", "2011-12-31"),
        )

    def test_accepts_every_period_spelling(self) -> None:
        """A period may be a string, a date, or a ReportingPeriod."""
        institution = FCAInstitution.from_roster_rows(
            rows=[
                ("2011-03-31", ROW),
                (datetime.date(2011, 6, 30), ROW),
                (period("2011-09-30"), ROW),
            ]
        )
        assert institution.periods == (
            PeriodRange(start="2011-03-31", end="2011-09-30"),
        )

    def test_gap_splits_presence_and_versions(self) -> None:
        """An absent quarter splits presence, and a version never spans it.

        The name is unchanged across the gap but still becomes two versions,
        because a version covers one contiguous span.
        """
        institution = gapped()
        assert institution.periods == (
            PeriodRange(start="2004-03-31", end="2004-06-30"),
            PeriodRange(start="2004-12-31", end="2005-03-31"),
        )
        assert institution.short_name_history == (
            version("Mid-America ACA", "2004-03-31", "2004-06-30"),
            version("Mid-America ACA", "2004-12-31", "2004-12-31"),
            version("Farm Credit Mid-America ACA", "2005-03-31", "2005-03-31"),
        )

    def test_value_can_recur(self) -> None:
        """A value that changes and changes back gives three versions."""
        institution = FCAInstitution.from_roster_rows(
            rows=[
                ("2004-03-31", ROW),
                ("2004-06-30", {**ROW, "STREET_ADDR": "PO Box 69"}),
                ("2004-09-30", ROW),
            ]
        )
        assert [v.value for v in institution.street_addr_history] == [
            "1601 UPS Drive",
            "PO Box 69",
            "1601 UPS Drive",
        ]

    def test_missing_text_becomes_none(self) -> None:
        """None and a float NaN (pandas' spelling) both become None.

        Without this, a pandas-built roster would record NaN and None as two
        different values and open a spurious version between them.
        """
        institution = FCAInstitution.from_roster_rows(
            rows=[
                ("2011-06-30", {**ROW, "MAIL_ADDR": None}),
                ("2011-09-30", {**ROW, "MAIL_ADDR": float("nan")}),
            ]
        )
        assert institution.mail_addr_history == (
            version(None, "2011-06-30", "2011-09-30"),
        )
        assert institution.most_recent_mail_addr is None

    def test_numeric_codes_become_int(self) -> None:
        """A code arriving as a float, as a pandas row can, is stored as an int."""
        institution = FCAInstitution.from_roster_rows(
            rows=[("2011-09-30", {**ROW, "UNINUM": 722825.0, "DIST": 22.0})]
        )
        assert institution.uninum == 722825
        assert isinstance(institution.district, int)

    def test_extra_columns_ignored(self) -> None:
        """Columns outside the roster's tracked set are ignored."""
        institution = FCAInstitution.from_roster_rows(
            rows=[("2011-09-30", {**ROW, "EXTRA": "x"})]
        )
        assert institution == FCAInstitution.from_roster_rows(
            rows=[("2011-09-30", ROW)]
        )

    def test_matching_year_and_month_accepted(self) -> None:
        """YEAR and MONTH naming the paired quarter are accepted."""
        institution = FCAInstitution.from_roster_rows(
            rows=[("2011-09-30", {**ROW, "YEAR": 2011, "MONTH": 9})]
        )
        assert institution.last_period == period("2011-09-30")

    def test_missing_year_skips_check(self) -> None:
        """A row with no YEAR value is not checked against its quarter."""
        institution = FCAInstitution.from_roster_rows(
            rows=[("2011-09-30", {**ROW, "YEAR": None, "MONTH": 3})]
        )
        assert institution.last_period == period("2011-09-30")

    def test_mismatched_year_and_month_rejected(self) -> None:
        """A row whose YEAR and MONTH name another quarter was mislabeled."""
        with pytest.raises(InstitutionError, match="YEAR=2011 and MONTH=6"):
            FCAInstitution.from_roster_rows(
                rows=[("2011-09-30", {**ROW, "YEAR": 2011, "MONTH": 6})]
            )

    def test_empty_rejected(self) -> None:
        """At least one row is needed."""
        with pytest.raises(InstitutionError, match="at least one roster row"):
            FCAInstitution.from_roster_rows(rows=[])

    def test_duplicate_quarter_rejected(self) -> None:
        """Two rows for the same quarter cannot both describe the charter."""
        with pytest.raises(InstitutionError, match=r"more than one roster row.*2011Q3"):
            FCAInstitution.from_roster_rows(
                rows=[("2011-09-30", ROW), (period("2011-09-30"), ROW)]
            )

    def test_missing_column_rejected(self) -> None:
        """A row missing a required column names every missing column."""
        row = {key: value for key, value in ROW.items() if key not in {"CITY", "ZIP"}}
        with pytest.raises(InstitutionError, match=r"missing column.*CITY, ZIP"):
            FCAInstitution.from_roster_rows(rows=[("2011-09-30", row)])

    @pytest.mark.parametrize("column", ["UNINUM", "SYSTEM"])
    def test_missing_code_rejected(self, column: str) -> None:
        """A row with no value for a code cannot be placed."""
        with pytest.raises(InstitutionError, match=f"has no {column}"):
            FCAInstitution.from_roster_rows(
                rows=[("2011-09-30", {**ROW, column: None})]
            )

    def test_mixed_uninum_rejected(self) -> None:
        """Rows for two UNINUMs are two charters, not one."""
        with pytest.raises(
            InstitutionError, match="mix UNINUM 722825 and UNINUM 722233"
        ):
            FCAInstitution.from_roster_rows(
                rows=[("2011-09-30", ROW), ("2011-12-31", {**ROW, "UNINUM": 722233})]
            )

    def test_mixed_codes_rejected(self) -> None:
        """A UNINUM whose codes change between quarters is inconsistent input."""
        with pytest.raises(InstitutionError, match=r"\(7, 22, 825\).*\(7, 23, 825\)"):
            FCAInstitution.from_roster_rows(
                rows=[("2011-09-30", ROW), ("2011-12-31", {**ROW, "DIST": 23})]
            )


class TestConstruction:
    """Validation when constructing an FCAInstitution directly."""

    def test_valid(self) -> None:
        """Histories that cover exactly the presence spans are accepted."""
        institution = FCAInstitution(
            uninum=722825,
            system=7,
            district=22,
            association=825,
            periods=(PeriodRange(start="2011-03-31", end="2011-09-30"),),
            **histories("x", "2011-03-31", "2011-09-30"),
        )
        assert institution.most_recent_city == "x"

    def test_same_value_across_gap_allowed(self) -> None:
        """Equal values on either side of a gap are two separate versions."""
        split = (
            version("x", "2011-03-31", "2011-03-31"),
            version("x", "2011-09-30", "2011-09-30"),
        )
        attributes = dict.fromkeys(histories("x", "2011-03-31", "2011-03-31"), split)
        institution = FCAInstitution(
            uninum=1,
            system=0,
            district=0,
            association=1,
            periods=(
                PeriodRange(start="2011-03-31", end="2011-03-31"),
                PeriodRange(start="2011-09-30", end="2011-09-30"),
            ),
            **attributes,
        )
        assert len(institution.periods) == 2

    def test_empty_periods_rejected(self) -> None:
        """A charter must have filed at least once."""
        with pytest.raises(InstitutionError, match="at least one period span"):
            FCAInstitution(
                uninum=1,
                system=0,
                district=0,
                association=1,
                periods=(),
                **histories("x", "2011-03-31", "2011-03-31"),
            )

    @pytest.mark.parametrize(
        "spans",
        [
            (("2011-06-30", "2011-09-30"), ("2011-03-31", "2011-03-31")),
            (("2011-03-31", "2011-06-30"), ("2011-06-30", "2011-09-30")),
            (("2011-03-31", "2011-06-30"), ("2011-09-30", "2011-12-31")),
        ],
        ids=["out-of-order", "overlapping", "adjacent"],
    )
    def test_bad_spans_rejected(self, spans: tuple[tuple[str, str], ...]) -> None:
        """Presence spans must be ordered, disjoint, and separated by a gap."""
        with pytest.raises(InstitutionError, match="period spans must be"):
            FCAInstitution(
                uninum=1,
                system=0,
                district=0,
                association=1,
                periods=tuple(PeriodRange(start=s, end=e) for s, e in spans),
                **histories("x", "2011-03-31", "2011-12-31"),
            )

    @pytest.mark.parametrize(
        "city_history",
        [
            (),
            (version("x", "2011-03-31", "2011-06-30"),),
            (version("x", "2011-03-31", "2011-12-31"),),
            (
                version("y", "2011-06-30", "2011-09-30"),
                version("x", "2011-03-31", "2011-03-31"),
            ),
        ],
        ids=["empty", "short", "long", "out-of-order"],
    )
    def test_history_must_cover_presence(
        self, city_history: tuple[InstitutionAttributeVersion, ...]
    ) -> None:
        """Each history must cover exactly the quarters filed, in order."""
        attributes = histories("x", "2011-03-31", "2011-09-30")
        attributes["city_history"] = city_history
        with pytest.raises(InstitutionError, match="CITY history must cover exactly"):
            FCAInstitution(
                uninum=1,
                system=0,
                district=0,
                association=1,
                periods=(PeriodRange(start="2011-03-31", end="2011-09-30"),),
                **attributes,
            )

    def test_adjacent_equal_versions_rejected(self) -> None:
        """Two touching versions with one value should have been one version."""
        attributes = histories("x", "2011-03-31", "2011-06-30")
        attributes["state_history"] = (
            version("KY", "2011-03-31", "2011-03-31"),
            version("KY", "2011-06-30", "2011-06-30"),
        )
        with pytest.raises(InstitutionError, match="STATE history has adjacent"):
            FCAInstitution(
                uninum=1,
                system=0,
                district=0,
                association=1,
                periods=(PeriodRange(start="2011-03-31", end="2011-06-30"),),
                **attributes,
            )


class TestAccessors:
    """Period bounds, most-recent values, and point-in-time snapshots."""

    def test_first_and_last_period(self) -> None:
        """The bounds span every presence span, gaps included."""
        institution = gapped()
        assert institution.first_period == period("2004-03-31")
        assert institution.last_period == period("2005-03-31")

    def test_most_recent_values(self) -> None:
        """Every most_recent_* property reads the last quarter filed."""
        latest = {
            **ROW,
            "SHORTNAME": "Farm Credit Mid-America ACA",
            "MAIL_ADDR": "P.O. Box 1",
            "STREET_ADDR": "12501 Lakefront Place",
            "CITY": "Lexington",
            "STATE": "OH",
            "ZIP": "40299-4894",
        }
        institution = FCAInstitution.from_roster_rows(
            rows=[("2022-12-31", ROW), ("2023-03-31", latest)]
        )
        assert (
            institution.most_recent_short_name,
            institution.most_recent_mail_addr,
            institution.most_recent_street_addr,
            institution.most_recent_city,
            institution.most_recent_state,
            institution.most_recent_zip,
        ) == (
            "Farm Credit Mid-America ACA",
            "P.O. Box 1",
            "12501 Lakefront Place",
            "Lexington",
            "OH",
            "40299-4894",
        )

    def test_as_of(self) -> None:
        """A snapshot holds the values in effect in the requested quarter."""
        snapshot = gapped().as_of(period="2004-06-30")
        assert snapshot == FCAInstitutionSnapshot(
            uninum=722825,
            period=period("2004-06-30"),
            system=7,
            district=22,
            association=825,
            short_name="Mid-America ACA",
            mail_addr="P.O. Box 34390",
            street_addr="PO Box 69",
            city="Louisville",
            state="KY",
            zip="40223-4390",
        )

    def test_as_of_accepts_reporting_period(self) -> None:
        """as_of takes a ReportingPeriod as well as a string."""
        assert gapped().as_of(period=period("2005-03-31")).short_name == (
            "Farm Credit Mid-America ACA"
        )

    @pytest.mark.parametrize("value", ["2004-09-30", "2003-12-31", "2005-06-30"])
    def test_as_of_unfiled_quarter_rejected(self, value: str) -> None:
        """A quarter in a gap or outside the history was not filed."""
        with pytest.raises(PeriodNotAvailableError, match="did not file"):
            gapped().as_of(period=value)

    def test_repr(self) -> None:
        """The repr names the charter, its latest name, and its span."""
        assert repr(gapped()) == (
            "FCAInstitution(uninum=722825, "
            "most_recent_short_name='Farm Credit Mid-America ACA', "
            "first_period=2004Q1, last_period=2005Q1)"
        )


class TestToDataframe:
    """The one-row-per-quarter tabular frame."""

    def test_rows_and_columns(self, backend: str) -> None:
        """One row per quarter filed, with values in effect that quarter."""
        rows = rows_of(gapped().to_dataframe())
        assert list(rows[0]) == FRAME_COLUMNS
        assert [as_date(row["period"]) for row in rows] == [
            datetime.date(2004, 3, 31),
            datetime.date(2004, 6, 30),
            datetime.date(2004, 12, 31),
            datetime.date(2005, 3, 31),
        ]
        assert [row["STREET_ADDR"] for row in rows] == [
            "1601 UPS Drive",
            "PO Box 69",
            "1601 UPS Drive",
            "1601 UPS Drive",
        ]
        assert [row["SHORTNAME"] for row in rows][-2:] == [
            "Mid-America ACA",
            "Farm Credit Mid-America ACA",
        ]
        assert {row["most_recent_short_name"] for row in rows} == {
            "Farm Credit Mid-America ACA"
        }
        assert {
            (row["UNINUM"], row["SYSTEM"], row["DIST"], row["ASSOC"]) for row in rows
        } == {(722825, 7, 22, 825)}

    def test_dtypes(self, backend: str) -> None:
        """Keys match to_long_format's, codes are Int64, and text is String."""
        frame = nw.from_native(gapped().to_dataframe())
        schema = frame.collect_schema()
        assert schema["UNINUM"] == nw.Int64()
        assert schema["period"] == (
            nw.Datetime("us") if backend == "pandas" else nw.Date()
        )
        assert {schema[column] for column in ("SYSTEM", "DIST", "ASSOC")} == {
            nw.Int64()
        }
        assert {schema[column] for column in FRAME_COLUMNS[5:]} == {nw.String()}

    def test_missing_value_stays_missing(self, backend: str) -> None:
        """A blank roster value is a null in every backend."""
        institution = FCAInstitution.from_roster_rows(
            rows=[("2011-09-30", {**ROW, "MAIL_ADDR": None})]
        )
        (row,) = rows_of(institution.to_dataframe())
        assert pd.isna(row["MAIL_ADDR"])
        assert pd.isna(row["most_recent_mail_addr"])

    def test_dataframe_type(self, polars_backend: str) -> None:
        """dataframe_type converts the result whatever the backend."""
        assert isinstance(gapped().to_dataframe(dataframe_type="pandas"), pd.DataFrame)

    def test_backend_override(self, polars_backend: str) -> None:
        """The backend argument picks the library for this call only."""
        assert isinstance(gapped().to_dataframe(backend="pandas"), pd.DataFrame)

    def test_lazy(self, lazy_polars_backend: str) -> None:
        """The configured laziness applies, as for every other reader."""
        assert isinstance(gapped().to_dataframe(), pl.LazyFrame)


class TestJson:
    """The JSON round trip."""

    def test_round_trip(self) -> None:
        """to_json then from_json gives back an equal charter."""
        institution = gapped()
        assert FCAInstitution.from_json(text=institution.to_json()) == institution

    def test_compact(self) -> None:
        """indent=None gives single-line output that still round-trips."""
        text = gapped().to_json(indent=None)
        assert "\n" not in text
        assert FCAInstitution.from_json(text=text) == gapped()

    def test_shape(self) -> None:
        """Attributes are stored by FCA column name as lists of versions."""
        payload = json.loads(gapped().to_json())
        assert payload["periods"] == [
            {"start": "2004-03-31", "end": "2004-06-30"},
            {"start": "2004-12-31", "end": "2005-03-31"},
        ]
        assert payload["attributes"]["SHORTNAME"][-1] == {
            "value": "Farm Credit Mid-America ACA",
            "start": "2005-03-31",
            "end": "2005-03-31",
        }

    @pytest.mark.parametrize(
        ("text", "match"),
        [
            ("{not json", "is not valid JSON"),
            ("[1, 2]", "expected a JSON object, got list"),
            ('{"uninum": 1}', "malformed institution JSON"),
            ('{"attributes": 3}', "malformed institution JSON"),
        ],
        ids=["invalid", "not-object", "missing-key", "wrong-type"],
    )
    def test_malformed_rejected(self, text: str, match: str) -> None:
        """Anything other than to_json's shape raises InstitutionError."""
        with pytest.raises(InstitutionError, match=match):
            FCAInstitution.from_json(text=text)

    def test_bad_period_rejected(self) -> None:
        """A date that is not a quarter end is malformed JSON, not a crash."""
        payload = json.loads(gapped().to_json())
        payload["periods"][0]["start"] = "2004-02-01"
        with pytest.raises(InstitutionError, match="malformed institution JSON"):
            FCAInstitution.from_json(text=json.dumps(payload))

    def test_invalid_history_rejected(self) -> None:
        """Loaded JSON is validated exactly like direct construction."""
        payload = json.loads(gapped().to_json())
        payload["attributes"]["CITY"] = []
        with pytest.raises(InstitutionError, match="CITY history must cover"):
            FCAInstitution.from_json(text=json.dumps(payload))
