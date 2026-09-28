"""Tests for the FCA institution registry generation script.

These run the script's steps on small, made-up rosters. The real archive is
covered by ``tests/fca/test_institution_registry_drift.py``, which rebuilds
the shipped registry from every archived release.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pytest

from call_report.core import PeriodRange, ReportingPeriod
from call_report.exceptions import InstitutionError
from call_report.fca import FCAInstitutionRegistry
from call_report.fca.catalog import LATEST_KNOWN_PERIOD
from tests.fca.institution_rows import ROW
from tests.helpers import load_institution_registry_script

generate = load_institution_registry_script()

Q1 = ReportingPeriod.from_period_end(value="2020-03-31")
Q2 = Q1.next()
Q3 = Q2.next()
TEXAS = {**ROW, "UNINUM": 610000, "SYSTEM": 6, "DIST": 10, "ASSOC": 0}


class FakeArchive:
    """A roster reader serving fixed rosters and recording each quarter read."""

    def __init__(self, rosters: Mapping[ReportingPeriod, list[dict[str, Any]]]) -> None:
        self.rosters = rosters
        self.reads: list[ReportingPeriod] = []

    def __call__(self, period: ReportingPeriod) -> Iterable[Mapping[str, Any]]:
        """Return one quarter's rows, or no rows for a quarter not given."""
        self.reads.append(period)
        return self.rosters.get(period, [])


def registry(*rosters: tuple[ReportingPeriod, list[dict[str, Any]]]) -> Any:
    """Build a registry directly from `(period, rows)` pairs."""
    return FCAInstitutionRegistry.from_rosters(rosters=rosters)


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the script's three files into a temporary directory."""
    monkeypatch.setattr(generate, "_BASE_PATH", tmp_path / "base" / "registry.json")
    monkeypatch.setattr(
        generate, "_OVERRIDES_PATH", tmp_path / "overrides" / "registry.json"
    )
    monkeypatch.setattr(
        generate, "_SHIPPED_PATH", tmp_path / "shipped" / "registry.json"
    )
    return tmp_path


class TestGenerateBase:
    """Building and extending the base registry."""

    def test_from_nothing(self) -> None:
        """With no seed, the registry holds exactly the rosters read."""
        read = FakeArchive({Q1: [ROW, TEXAS], Q2: [ROW]})
        built = generate._generate_base(seed=None, periods=[Q1, Q2], read_roster=read)
        assert built == registry((Q1, [ROW, TEXAS]), (Q2, [ROW]))

    def test_extends_a_seed(self) -> None:
        """Extending a seed equals building from every quarter at once.

        Only the new quarter is read. This is what lets a run skip the
        quarters the base already holds.
        """
        seed = registry((Q1, [ROW, TEXAS]), (Q2, [ROW]))
        renamed = {**ROW, "SHORTNAME": "Farm Credit Mid-America ACA"}
        read = FakeArchive({Q3: [renamed]})
        built = generate._generate_base(seed=seed, periods=[Q3], read_roster=read)
        assert built == registry((Q1, [ROW, TEXAS]), (Q2, [ROW]), (Q3, [renamed]))
        assert read.reads == [Q3]

    def test_rosters_from_registry_round_trip(self) -> None:
        """Unpacking a registry gives back rows that rebuild it exactly."""
        seed = registry((Q1, [ROW, {**TEXAS, "MAIL_ADDR": None}]), (Q3, [ROW]))
        rosters = generate._rosters_from_registry(seed)
        assert sorted(rosters) == [Q1, Q3]
        assert FCAInstitutionRegistry.from_rosters(rosters=rosters.items()) == seed

    def test_quarter_already_in_seed_rejected(self) -> None:
        """Re-reading a quarter the seed holds is an error, not a silent merge."""
        seed = registry((Q1, [ROW]))
        with pytest.raises(InstitutionError, match="2020Q1 is already in the base"):
            generate._generate_base(
                seed=seed, periods=[Q1], read_roster=FakeArchive({Q1: [ROW]})
            )


class TestOverrides:
    """Hand corrections applied on top of the base."""

    def base(self) -> Any:
        """Return a registry whose ZIP changes once, in Q2."""
        return registry(
            (Q1, [ROW, TEXAS]),
            (Q2, [{**ROW, "ZIP": "40223"}, TEXAS]),
            (Q3, [{**ROW, "ZIP": "40223"}]),
        )

    @pytest.mark.parametrize("overrides", [None, {}], ids=["none", "empty"])
    def test_no_overrides(self, overrides: dict[str, Any] | None) -> None:
        """Without corrections, the base is returned unchanged."""
        base = self.base()
        assert generate._apply_overrides(base, overrides) is base

    def test_value_corrected(self) -> None:
        """A correction replaces one entry's value and keeps its quarters."""
        overrides = {
            "institutions": {
                "722825": {"CITY": [{"period_start": "2020-03-31", "value": "X"}]}
            }
        }
        corrected = generate._apply_overrides(self.base(), overrides)
        assert [v.value for v in corrected[722825].city_history] == ["X"]
        assert corrected[610000] == self.base()[610000]

    def test_back_to_back_entries_merged(self) -> None:
        """A correction that makes two entries equal leaves one entry.

        Without the merge, the corrected history would break the rule that
        back-to-back entries differ, and building the registry would fail.
        """
        overrides = {
            "institutions": {
                "722825": {"ZIP": [{"period_start": "2020-06-30", "value": ROW["ZIP"]}]}
            }
        }
        corrected = generate._apply_overrides(self.base(), overrides)
        (entry,) = corrected[722825].zip_history
        assert entry.value == ROW["ZIP"]
        assert entry.periods == PeriodRange(start=Q1, end=Q3)

    @pytest.mark.parametrize(
        ("overrides", "match"),
        [
            (
                {"institutions": {"999999": {"ZIP": []}}},
                "UNINUM 999999, which is not in the registry",
            ),
            (
                {"institutions": {"722825": {"DIST": []}}},
                "names column 'DIST'",
            ),
            (
                {
                    "institutions": {
                        "722825": {"ZIP": [{"period_start": "2020-09-30", "value": ""}]}
                    }
                },
                "period_start '2020-09-30', but no entry starts there",
            ),
        ],
        ids=["unknown-uninum", "not-a-name-or-address-column", "stale-start"],
    )
    def test_bad_override_rejected(self, overrides: dict[str, Any], match: str) -> None:
        """A correction that does not match the base is an error."""
        with pytest.raises(InstitutionError, match=match):
            generate._apply_overrides(self.base(), overrides)


class TestAudit:
    """Comparing a full rebuild with the existing base."""

    def test_equal(self) -> None:
        """Identical registries pass."""
        assert generate._audit(registry((Q1, [ROW])), registry((Q1, [ROW])))

    def test_different(self, caplog: pytest.LogCaptureFixture) -> None:
        """A difference fails and names the UNINUMs involved."""
        rebuilt = registry((Q1, [{**ROW, "CITY": "X"}, TEXAS]))
        previous = registry((Q1, [ROW]))
        assert not generate._audit(rebuilt, previous)
        assert "Only in the rebuild: [610000]" in caplog.text
        assert "Different histories: [722825]" in caplog.text


class TestMain:
    """The whole pipeline, run against temporary files."""

    def rosters(self) -> dict[ReportingPeriod, list[dict[str, Any]]]:
        """Return a roster for every quarter up to LATEST_KNOWN_PERIOD."""
        periods = [LATEST_KNOWN_PERIOD.previous(), LATEST_KNOWN_PERIOD]
        return {period: [ROW] for period in periods}

    def seed_base(self, paths: Path) -> Any:
        """Write a base holding every quarter but the latest."""
        base = registry((LATEST_KNOWN_PERIOD.previous(), [ROW]))
        generate._write_json(generate._BASE_PATH, base)
        return base

    def test_incremental_run(self, paths: Path) -> None:
        """A run reads only the quarters after the base, and writes both files."""
        self.seed_base(paths)
        read = FakeArchive(self.rosters())
        assert generate.main([], read_roster=read) == 0
        assert read.reads == [LATEST_KNOWN_PERIOD]
        shipped = FCAInstitutionRegistry.from_json(
            text=generate._SHIPPED_PATH.read_text(encoding="utf-8")
        )
        assert shipped.last_period == LATEST_KNOWN_PERIOD
        assert generate._BASE_PATH.read_text(encoding="utf-8") == (
            generate._SHIPPED_PATH.read_text(encoding="utf-8")
        )

    def test_up_to_date_run_reads_nothing(self, paths: Path) -> None:
        """When the base already reaches the latest quarter, nothing is read."""
        generate._write_json(generate._BASE_PATH, registry(*self.rosters().items()))
        read = FakeArchive({})
        assert generate.main([], read_roster=read) == 0
        assert read.reads == []

    def test_overrides_reach_the_shipped_file_only(self, paths: Path) -> None:
        """Corrections change the shipped file and leave the base as read."""
        self.seed_base(paths)
        generate._OVERRIDES_PATH.parent.mkdir(parents=True)
        start = LATEST_KNOWN_PERIOD.previous().period_end.isoformat()
        generate._OVERRIDES_PATH.write_text(
            json.dumps(
                {
                    "institutions": {
                        "722825": {"CITY": [{"period_start": start, "value": "X"}]}
                    }
                }
            ),
            encoding="utf-8",
        )
        assert generate.main([], read_roster=FakeArchive(self.rosters())) == 0
        base = FCAInstitutionRegistry.from_json(
            text=generate._BASE_PATH.read_text(encoding="utf-8")
        )
        shipped = FCAInstitutionRegistry.from_json(
            text=generate._SHIPPED_PATH.read_text(encoding="utf-8")
        )
        assert base[722825].most_recent_city == ROW["CITY"]
        assert shipped[722825].most_recent_city == "X"

    def test_full_run_matching_base(self, paths: Path) -> None:
        """--full reads every quarter and passes when it matches the base."""
        generate._write_json(generate._BASE_PATH, registry(*self.rosters().items()))
        read = FakeArchive(self.rosters())
        assert generate.main(["--full"], read_roster=read) == 0
        assert read.reads[0] == generate.EARLIEST_PERIOD
        assert read.reads[-1] == LATEST_KNOWN_PERIOD

    def test_full_run_drift(self, paths: Path) -> None:
        """--full returns 1 and writes nothing when the base differs."""
        stale = registry((LATEST_KNOWN_PERIOD, [{**ROW, "CITY": "X"}]))
        generate._write_json(generate._BASE_PATH, stale)
        assert generate.main(["--full"], read_roster=FakeArchive(self.rosters())) == 1
        assert not generate._SHIPPED_PATH.exists()

    def test_first_run(self, paths: Path) -> None:
        """Before any base exists, every quarter is read."""
        assert generate.main([], read_roster=FakeArchive(self.rosters())) == 0
        assert generate._BASE_PATH.is_file()
