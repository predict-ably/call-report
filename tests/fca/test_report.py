"""End-to-end tests for the FCACallReport estimator-style entry point."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import patch

import narwhals as nw
import pandas as pd
import polars as pl
import pyarrow as pa
import pytest

from call_report.config import config_context
from call_report.core import FieldSchema, ReportingPeriod
from call_report.core._backend import DataFrameType, date_dtype
from call_report.exceptions import (
    DomainDatasetNotFoundError,
    DownloadError,
    InvalidPeriodError,
    LayoutParseError,
    PeriodNotAvailableError,
    ReshapeError,
    ScheduleNotFoundError,
)
from call_report.fca import (
    FCACallReport,
    FCADomainDataset,
    FCASchedule,
    convert_long_format_to_code_grain_format,
    convert_long_format_to_wide_format,
    convert_wide_format_to_long_format,
    get_fca_domain_dataset,
    get_fca_file_metadata,
)
from call_report.fca.layout import FCALayout
from call_report.fca.transport import (
    LocalDirectoryTransport,
    PackagedArchiveTransport,
)
from tests.fca.layouts import RC_LINES_7COL
from tests.helpers import as_date, is_missing, rows_of, write_data, write_layout

# ---------------------------------------------------------------------------
# __init__ / sklearn-style conventions
# ---------------------------------------------------------------------------


def test_init_stores_params_verbatim_and_does_no_work(tmp_path: Path) -> None:
    """Constructing with an invalid quarter-end does not raise -- only fetch() does."""
    transport = LocalDirectoryTransport(data_dir=tmp_path)
    report = FCACallReport(start="not-a-date", end=None, transport=transport)
    assert report.start == "not-a-date"
    assert report.end is None
    assert report.schema_policy == "union"
    assert report.transport is transport


def test_constructor_is_keyword_only() -> None:
    """FCACallReport takes no positional arguments."""
    with pytest.raises(TypeError):
        FCACallReport("2024-03-31")  # type: ignore[call-arg]


def test_repr_echoes_quarter_end_dates_passed(tmp_path: Path) -> None:
    """__repr__ shows the raw quarter-end date strings the user passed in."""
    report = FCACallReport(
        start="2024-03-31",
        end="2025-12-31",
        transport=LocalDirectoryTransport(data_dir=tmp_path),
    )
    text = repr(report)
    assert "2024-03-31" in text
    assert "2025-12-31" in text


def test_get_params_and_set_params(tmp_path: Path) -> None:
    """get_params/set_params follow the sklearn convention."""
    report = FCACallReport(
        start="2024-03-31",
        end="2025-12-31",
        transport=LocalDirectoryTransport(data_dir=tmp_path),
    )
    params = report.get_params()
    assert params["start"] == "2024-03-31"
    assert params["end"] == "2025-12-31"
    assert params["schema_policy"] == "union"

    same_report = report.set_params(schema_policy="intersection")
    assert same_report is report
    assert report.get_params()["schema_policy"] == "intersection"


def test_set_params_rejects_unknown_key(tmp_path: Path) -> None:
    """set_params raises for a parameter name the estimator doesn't accept."""
    report = FCACallReport(
        start="2024-03-31",
        end="2025-12-31",
        transport=LocalDirectoryTransport(data_dir=tmp_path),
    )
    with pytest.raises(ValueError, match="not_a_real_param"):
        report.set_params(not_a_real_param=True)


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


def test_fetch_missing_end_raises_informative_error(tmp_path: Path) -> None:
    """Omitting end is ambiguous and must raise, not silently mean "one quarter"."""
    report = FCACallReport(
        start="2026-03-31", transport=LocalDirectoryTransport(data_dir=tmp_path)
    )
    with pytest.raises(InvalidPeriodError, match="end"):
        report.fetch()


def test_fetch_invalid_quarter_end_raises(tmp_path: Path) -> None:
    """fetch() validates start/end and raises InvalidPeriodError for a bad date."""
    report = FCACallReport(
        start="2026-05-15",
        end="2026-06-30",
        transport=LocalDirectoryTransport(data_dir=tmp_path),
    )
    with pytest.raises(InvalidPeriodError):
        report.fetch()


def test_fetch_out_of_catalog_bounds_raises(tmp_path: Path) -> None:
    """fetch() raises PeriodNotAvailableError for a range FCA has never published."""
    report = FCACallReport(
        start="1999-12-31",
        end="1999-12-31",
        transport=LocalDirectoryTransport(data_dir=tmp_path),
    )
    with pytest.raises(PeriodNotAvailableError):
        report.fetch()


def test_constructor_requires_transport() -> None:
    """Transport is a required keyword-only parameter with no implicit default."""
    with pytest.raises(TypeError, match="transport"):
        FCACallReport(start="2026-03-31", end="2026-03-31")  # type: ignore[call-arg]


def test_fetch_returns_self_and_populates_fitted_state(
    data_dir: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """fetch() returns self and sets periods_/releases_/schedules_/errors_."""
    report = FCACallReport(
        start="2025-09-30",
        end="2025-12-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    result = report.fetch()
    assert result is report

    assert len(report.periods_) == 2
    assert len(report.releases_) == 2
    assert report.errors_ == ()
    assert FCASchedule.RC in report.schedules_
    assert FCASchedule.RCB in report.schedules_
    q3 = ReportingPeriod.from_period_end(value="2025-09-30")
    q4 = ReportingPeriod.from_period_end(value="2025-12-31")
    assert set(report.schedules_[FCASchedule.RC]) == {q3, q4}
    # RCR7 only exists in the 2025-Q4 fixture.
    assert report.schedules_[FCASchedule.RCR7] == (q4,)


def test_fetch_records_missing_period_directory_without_failing_whole_request(
    tmp_path: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """Skip period with no resolvable local directory."""
    # tmp_path has Sept & Dec 2025 on disk, but the requested range also
    # includes June 2025, which was never built.
    report = FCACallReport(
        start="2025-06-30",
        end="2025-12-31",
        transport=LocalDirectoryTransport(data_dir=tmp_path),
    )
    report.fetch()

    assert len(report.periods_) == 3
    assert len(report.releases_) == 2
    june = ReportingPeriod.from_period_end(value="2025-06-30")
    assert len(report.errors_) == 1
    issue = report.errors_[0]
    assert issue.period == june
    assert issue.schedule is None
    assert isinstance(issue.error, DownloadError)


def test_fetch_raises_download_error_when_every_period_fails(tmp_path: Path) -> None:
    """If not a single requested period can be resolved, fetch() raises."""
    report = FCACallReport(
        start="2025-09-30",
        end="2025-12-31",
        transport=LocalDirectoryTransport(data_dir=tmp_path),
    )
    with pytest.raises(DownloadError, match="requested period"):
        report.fetch()


def test_fetch_ignores_unrecognized_root_name(
    data_dir: Path, release_2026q1: Path
) -> None:
    """A release file pair whose root isn't a known FCASchedule is silently skipped."""
    write_layout(release_2026q1, root="ZZZTEST", variable_lines=RC_LINES_7COL)
    write_data(
        release_2026q1,
        root="ZZZTEST",
        year=2026,
        month=3,
        rows=["6,10,0,3,2026,610000,1000000"],
    )
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    report.fetch()
    assert all(name != "ZZZTEST" for name in {s.value for s in report.schedules_})


def test_available_periods_and_schedules_do_not_require_fetch(tmp_path: Path) -> None:
    """available_periods()/available_schedules() reflect catalog totals.

    No fetch() needed.
    """
    report = FCACallReport(
        start="2025-09-30",
        end="2025-12-31",
        transport=LocalDirectoryTransport(data_dir=tmp_path),
    )
    periods = report.available_periods()
    assert periods[0] == ReportingPeriod.from_period_end(value="2000-03-31")
    assert periods[-1] == ReportingPeriod.from_period_end(value="2026-03-31")
    assert FCASchedule.RCB in report.available_schedules()


# ---------------------------------------------------------------------------
# load()
# ---------------------------------------------------------------------------


def test_load_auto_fetches_if_needed(data_dir: Path, release_2026q1: Path) -> None:
    """load() calls fetch() automatically when it hasn't run yet."""
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    result = report.load(schedule="RC")
    rows = rows_of(result)
    assert len(rows) == 1
    assert rows[0]["UNINUM"] == 610000


def test_load_result_carries_period_and_uninum_columns(
    data_dir: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """Every stacked frame carries a period column (the quarter-end date) and uninum."""
    report = FCACallReport(
        start="2025-09-30",
        end="2025-12-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    result = report.load(schedule="RC")
    rows = rows_of(result)
    assert {"period", "UNINUM"} <= set(rows[0])
    periods_seen = {as_date(r["period"]) for r in rows}
    assert periods_seen == {date(2025, 9, 30), date(2025, 12, 31)}


def test_load_schema_policy_union_outer_joins_columns(
    data_dir: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """Union (the default) keeps every column, nulling it out where absent."""
    report = FCACallReport(
        start="2025-09-30",
        end="2025-12-31",
        schema_policy="union",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    rows = rows_of(report.load(schedule="RC"))
    assert "TOTLIAB" in rows[0]
    q3_rows = [r for r in rows if as_date(r["period"]) == date(2025, 9, 30)]
    q4_rows = [r for r in rows if as_date(r["period"]) == date(2025, 12, 31)]
    assert all(is_missing(r["TOTLIAB"]) for r in q3_rows)
    assert all(not is_missing(r["TOTLIAB"]) for r in q4_rows)


def test_load_schema_policy_intersection_drops_uncommon_columns(
    data_dir: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """Intersection keeps only columns common to every stacked period."""
    report = FCACallReport(
        start="2025-09-30",
        end="2025-12-31",
        schema_policy="intersection",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    rows = rows_of(report.load(schedule="RC"))
    assert "TOTLIAB" not in rows[0]
    assert "TOTASSETS" in rows[0]


def test_load_schema_policy_strict_raises_on_mismatch(
    data_dir: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """Strict refuses to silently reconcile differing schemas across periods."""
    report = FCACallReport(
        start="2025-09-30",
        end="2025-12-31",
        schema_policy="strict",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    with pytest.raises(LayoutParseError):
        report.load(schedule="RC")


def test_load_schedule_missing_in_some_periods_returns_partial_result(
    data_dir: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """A schedule absent in some, but not all, periods yields a partial result."""
    report = FCACallReport(
        start="2025-09-30",
        end="2025-12-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    rows = rows_of(report.load(schedule="RCR7"))
    q3 = ReportingPeriod.from_period_end(value="2025-09-30")
    q4 = ReportingPeriod.from_period_end(value="2025-12-31")
    assert all(as_date(r["period"]) == date(2025, 12, 31) for r in rows)
    assert report.periods_available(schedule="RCR7") == (q4,)
    assert report.periods_missing(schedule="RCR7") == (q3,)


def test_load_schedule_not_found_when_absent_everywhere(
    data_dir: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """A schedule present in zero requested periods raises ScheduleNotFoundError."""
    report = FCACallReport(
        start="2025-09-30",
        end="2025-12-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    with pytest.raises(ScheduleNotFoundError):
        report.load(schedule="RCF1")


def test_load_records_layout_parse_error_and_continues(tmp_path: Path) -> None:
    """A period whose schedule layout fails to parse is skipped, not fatal.

    The failure is recorded in errors_, and the other period's data still
    loads successfully.
    """
    good_dir = tmp_path / "2025September"
    good_dir.mkdir()
    write_layout(good_dir, root="INST", variable_lines=RC_LINES_7COL)
    write_data(
        good_dir, root="INST", year=2025, month=9, rows=["6,10,0,9,2025,610000,1000000"]
    )
    write_layout(good_dir, root="RC", variable_lines=RC_LINES_7COL)
    write_data(
        good_dir, root="RC", year=2025, month=9, rows=["6,10,0,9,2025,610000,1000000"]
    )

    bad_dir = tmp_path / "2025December"
    bad_dir.mkdir()
    write_layout(bad_dir, root="INST", variable_lines=RC_LINES_7COL)
    write_data(
        bad_dir,
        root="INST",
        year=2025,
        month=12,
        rows=["6,10,0,12,2025,610000,1000000"],
    )
    malformed_lines = [
        "  SYSTEM    Numeric  0  System Code",
        "  **CODE1   Numeric  0  Code One",
        "  MIDFIELD  Numeric  0  Mid Field",
        "  **CODE2   Numeric  0  Code Two",
    ]
    write_layout(bad_dir, root="RC", variable_lines=malformed_lines)
    write_data(
        bad_dir, root="RC", year=2025, month=12, rows=["6,10,0,12,2025,610000,1000000"]
    )

    report = FCACallReport(
        start="2025-09-30",
        end="2025-12-31",
        transport=LocalDirectoryTransport(data_dir=tmp_path),
    )
    rows = rows_of(report.load(schedule="RC"))
    assert len(rows) == 1
    assert as_date(rows[0]["period"]) == date(2025, 9, 30)

    assert len(report.errors_) == 1
    issue = report.errors_[0]
    assert issue.period == ReportingPeriod.from_period_end(value="2025-12-31")
    assert issue.schedule == FCASchedule.RC
    assert isinstance(issue.error, LayoutParseError)


def test_load_all_skips_schedule_that_fails_in_every_period(tmp_path: Path) -> None:
    """A schedule that fails to parse in every period is omitted from load_all()."""
    directory = tmp_path / "2026March"
    directory.mkdir()
    write_layout(directory, root="INST", variable_lines=RC_LINES_7COL)
    write_data(
        directory,
        root="INST",
        year=2026,
        month=3,
        rows=["6,10,0,3,2026,610000,1000000"],
    )
    malformed_lines = [
        "  SYSTEM    Numeric  0  System Code",
        "  **CODE1   Numeric  0  Code One",
        "  MIDFIELD  Numeric  0  Mid Field",
        "  **CODE2   Numeric  0  Code Two",
    ]
    write_layout(directory, root="RC", variable_lines=malformed_lines)
    write_data(
        directory, root="RC", year=2026, month=3, rows=["6,10,0,3,2026,610000,1000000"]
    )

    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=tmp_path),
    )
    result = report.load_all()
    assert FCASchedule.RC not in result
    assert any(issue.schedule == FCASchedule.RC for issue in report.errors_)


def test_load_accepts_case_insensitive_schedule_string(
    data_dir: Path, release_2026q1: Path
) -> None:
    """load() accepts either the FCASchedule enum or a case-insensitive string."""
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    by_string = rows_of(report.load(schedule="rcb"))
    report2 = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    by_enum = rows_of(report2.load(schedule=FCASchedule.RCB))
    assert by_string == by_enum


def test_load_all_returns_every_discovered_schedule(
    data_dir: Path, release_2026q1: Path
) -> None:
    """load_all() returns a dict keyed by every schedule found in range."""
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    result = report.load_all()
    assert set(result) == {FCASchedule.RC, FCASchedule.RCB, FCASchedule.RCR7}


def test_load_institutions(data_dir: Path, release_2026q1: Path) -> None:
    """load_institutions() returns the institution roster with period/uninum."""
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    rows = rows_of(report.load_institutions())
    assert rows[0]["UNINUM"] == 610000
    assert rows[0]["SHORTNAME"] == "Café Ridge FCB"
    assert as_date(rows[0]["period"]) == date(2026, 3, 31)


def test_load_institutions_records_parse_error_and_continues(tmp_path: Path) -> None:
    """A period whose INST layout fails to parse is skipped, not fatal."""
    good_dir = tmp_path / "2025September"
    good_dir.mkdir()
    write_layout(good_dir, root="INST", variable_lines=RC_LINES_7COL)
    write_data(
        good_dir, root="INST", year=2025, month=9, rows=["6,10,0,9,2025,610000,1000000"]
    )

    bad_dir = tmp_path / "2025December"
    bad_dir.mkdir()
    malformed_lines = [
        "  SYSTEM    Numeric  0  System Code",
        "  **CODE1   Numeric  0  Code One",
        "  MIDFIELD  Numeric  0  Mid Field",
        "  **CODE2   Numeric  0  Code Two",
    ]
    write_layout(bad_dir, root="INST", variable_lines=malformed_lines)
    write_data(
        bad_dir,
        root="INST",
        year=2025,
        month=12,
        rows=["6,10,0,12,2025,610000,1000000"],
    )

    report = FCACallReport(
        start="2025-09-30",
        end="2025-12-31",
        transport=LocalDirectoryTransport(data_dir=tmp_path),
    )
    rows = rows_of(report.load_institutions())
    assert len(rows) == 1
    assert as_date(rows[0]["period"]) == date(2025, 9, 30)
    assert len(report.errors_) == 1
    assert isinstance(report.errors_[0].error, LayoutParseError)


def test_load_institutions_raises_when_no_period_has_a_roster(tmp_path: Path) -> None:
    """If not one requested period has an INST file pair, load_institutions() raises."""
    directory = tmp_path / "2026March"
    directory.mkdir()
    write_layout(directory, root="RC", variable_lines=RC_LINES_7COL)
    write_data(
        directory, root="RC", year=2026, month=3, rows=["6,10,0,3,2026,610000,1000000"]
    )

    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=tmp_path),
    )
    with pytest.raises(DownloadError, match="roster"):
        report.load_institutions()


# ---------------------------------------------------------------------------
# get_layout()
# ---------------------------------------------------------------------------


def test_get_layout_with_period_returns_single_layout(
    data_dir: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """Passing a specific period returns just that period's FCALayout."""
    report = FCACallReport(
        start="2025-09-30",
        end="2025-12-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    layout = report.get_layout(schedule="RC", period="2025-09-30")
    assert isinstance(layout, FCALayout)
    assert "TOTLIAB" not in layout.leading_columns


def test_get_layout_without_period_returns_dict_across_range(
    data_dir: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """Omitting period returns a dict showing the layout for each period in range."""
    report = FCACallReport(
        start="2025-09-30",
        end="2025-12-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    layouts = report.get_layout(schedule="RC")
    assert isinstance(layouts, dict)
    q3 = ReportingPeriod.from_period_end(value="2025-09-30")
    q4 = ReportingPeriod.from_period_end(value="2025-12-31")
    assert "TOTLIAB" not in layouts[q3].leading_columns
    assert "TOTLIAB" in layouts[q4].leading_columns


def test_get_layout_with_period_outside_fetched_range_raises(
    data_dir: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """A period outside the instance's fetched range is rejected clearly."""
    report = FCACallReport(
        start="2025-09-30",
        end="2025-12-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    with pytest.raises(InvalidPeriodError, match="outside the fetched range"):
        report.get_layout(schedule="RC", period="2026-03-31")


def test_get_layout_with_period_missing_schedule_raises(
    data_dir: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """A period in range but lacking the requested schedule is rejected clearly."""
    report = FCACallReport(
        start="2025-09-30",
        end="2025-12-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    with pytest.raises(ScheduleNotFoundError):
        report.get_layout(schedule="RCR7", period="2025-09-30")


def test_get_layout_without_period_schedule_not_found_anywhere_raises(
    data_dir: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """A schedule absent from every period in range raises, even without period=."""
    report = FCACallReport(
        start="2025-09-30",
        end="2025-12-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    with pytest.raises(ScheduleNotFoundError):
        report.get_layout(schedule="RCF1")


def test_get_layout_is_keyword_only(data_dir: Path, release_2026q1: Path) -> None:
    """get_layout takes no positional arguments."""
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    with pytest.raises(TypeError):
        report.get_layout("RC")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# get_layout() / get_schema() / to_field_schema() composed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("schedule", ["RC", "RCB", "RCF1"])
def test_a_real_release_layout_matches_the_shipped_metadata(schedule: str) -> None:
    """What 2026Q1 shipped agrees field-for-field with the canonical metadata.

    This is the comparison the three methods exist to make, run against real
    archived data. A non-empty diff means the shipped schedule metadata has
    drifted from the releases it was generated from, which is a defect in the
    metadata rather than a reason to relax this test.
    """
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=PackagedArchiveTransport(),
    )
    layout = report.get_layout(schedule=schedule, period="2026-03-31")
    assert isinstance(layout, FCALayout)
    canonical = report.get_schema(schedule=schedule, period="2026-03-31")
    assert isinstance(canonical, FieldSchema)

    diff = layout.to_field_schema(period="2026-03-31").compare(other=canonical)
    assert diff.added == ()
    assert diff.removed == ()
    assert [change.name for change in diff.changed] == []
    assert diff.is_empty


# ---------------------------------------------------------------------------
# get_schema()
# ---------------------------------------------------------------------------


def test_get_schema_with_period_returns_a_single_schema(
    data_dir: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """Passing a specific period returns just that period's canonical FieldSchema."""
    report = FCACallReport(
        start="2025-09-30",
        end="2025-12-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    schema = report.get_schema(schedule="RC", period="2025-09-30")
    assert isinstance(schema, FieldSchema)
    assert schema.names[:6] == (
        "SYSTEM",
        "DIST",
        "ASSOC",
        "MONTH",
        "YEAR",
        "UNINUM",
    )


def test_get_schema_narrows_each_field_to_the_requested_quarter(
    data_dir: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """as_of collapses every field to one version covering only that quarter."""
    report = FCACallReport(
        start="2025-09-30",
        end="2025-12-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    schema = report.get_schema(schedule="RC", period="2025-09-30")
    assert isinstance(schema, FieldSchema)
    period = ReportingPeriod.from_period_end(value="2025-09-30")
    for field in schema.values():
        assert len(field.versions) == 1
        assert tuple(field.versions[0].periods) == (period,)


def test_get_schema_without_period_returns_dict_across_range(
    data_dir: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """Omitting period returns a dict covering each fetched period with the schedule."""
    report = FCACallReport(
        start="2025-09-30",
        end="2025-12-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    schemas = report.get_schema(schedule="RC")
    assert isinstance(schemas, dict)
    q3 = ReportingPeriod.from_period_end(value="2025-09-30")
    q4 = ReportingPeriod.from_period_end(value="2025-12-31")
    assert set(schemas) == {q3, q4}
    assert schemas[q3]["UNINUM"].versions[0].periods[0] == q3
    assert schemas[q4]["UNINUM"].versions[0].periods[0] == q4


def test_get_schema_without_period_skips_periods_lacking_the_schedule(
    data_dir: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """RCR7 is only in 2025Q4, so the mapping has that one key, not both."""
    report = FCACallReport(
        start="2025-09-30",
        end="2025-12-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    schemas = report.get_schema(schedule="RCR7")
    assert isinstance(schemas, dict)
    assert set(schemas) == {ReportingPeriod.from_period_end(value="2025-12-31")}


def test_get_schema_with_period_outside_fetched_range_raises(
    data_dir: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """A period outside the instance's fetched range is rejected, as in get_layout."""
    report = FCACallReport(
        start="2025-09-30",
        end="2025-12-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    with pytest.raises(InvalidPeriodError, match="outside the fetched range"):
        report.get_schema(schedule="RC", period="2026-03-31")


def test_get_schema_with_period_missing_schedule_raises(
    data_dir: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """A period in range but lacking the requested schedule is rejected clearly."""
    report = FCACallReport(
        start="2025-09-30",
        end="2025-12-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    with pytest.raises(ScheduleNotFoundError):
        report.get_schema(schedule="RCR7", period="2025-09-30")


def test_get_schema_without_period_schedule_not_found_anywhere_raises(
    data_dir: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """A schedule absent from every period in range raises, even without period=."""
    report = FCACallReport(
        start="2025-09-30",
        end="2025-12-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    with pytest.raises(ScheduleNotFoundError):
        report.get_schema(schedule="RCF1")


def test_get_schema_reports_metadata_drift_as_its_own_error(
    data_dir: Path, release_2025q3: Path
) -> None:
    """A release carrying a schedule the shipped metadata retired raises distinctly.

    The canonical metadata has RCI ending at 2017Q4. A 2025Q3 release
    containing it is a disagreement between the release and the shipped
    metadata, which must not be flattened into ScheduleNotFoundError: that
    would read as "this release has no RCI" when the release plainly does.
    """
    write_layout(release_2025q3, root="RCI", variable_lines=RC_LINES_7COL)
    write_data(
        release_2025q3,
        root="RCI",
        year=2025,
        month=9,
        rows=["6,10,0,9,2025,610000,1000000"],
    )
    report = FCACallReport(
        start="2025-09-30",
        end="2025-09-30",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    assert isinstance(report.get_layout(schedule="RCI", period="2025-09-30"), FCALayout)
    with pytest.raises(PeriodNotAvailableError, match="was not published"):
        report.get_schema(schedule="RCI", period="2025-09-30")


def test_get_schema_is_keyword_only(data_dir: Path, release_2026q1: Path) -> None:
    """get_schema takes no positional arguments."""
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    with pytest.raises(TypeError):
        report.get_schema("RC")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# get_file_metadata()
# ---------------------------------------------------------------------------


def test_get_file_metadata_returns_the_canonical_shipped_metadata(
    tmp_path: Path,
) -> None:
    """The estimator hands back exactly what get_fca_file_metadata returns."""
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=tmp_path),
    )
    assert report.get_file_metadata(schedule="RCB") is get_fca_file_metadata(
        schedule=FCASchedule.RCB
    )


@pytest.mark.parametrize("schedule", ["RCB", "rcb", FCASchedule.RCB], ids=str)
def test_get_file_metadata_accepts_a_schedule_or_a_string(
    tmp_path: Path, schedule: FCASchedule | str
) -> None:
    """A string is matched case-insensitively, the same as everywhere else."""
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=tmp_path),
    )
    assert report.get_file_metadata(schedule=schedule).name == "RCB"


def test_get_file_metadata_does_not_require_fetch(tmp_path: Path) -> None:
    """The shipped metadata is fetch-independent, so an empty data_dir is fine.

    fetch() against this transport would raise DownloadError, so this also
    proves get_file_metadata does not call _ensure_fetched on the way through.
    """
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=tmp_path),
    )
    assert report.get_file_metadata(schedule="RC").name == "RC"
    assert not hasattr(report, "periods_")


def test_get_file_metadata_rejects_an_unknown_schedule(tmp_path: Path) -> None:
    """A name that is not an FCA schedule is rejected by the shared coercion."""
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=tmp_path),
    )
    with pytest.raises(ScheduleNotFoundError):
        report.get_file_metadata(schedule="NOPE")


def test_get_file_metadata_is_keyword_only(tmp_path: Path) -> None:
    """get_file_metadata takes no positional arguments."""
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=tmp_path),
    )
    with pytest.raises(TypeError):
        report.get_file_metadata("RCB")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Legacy vs. modern naming, backend/lazy configuration
# ---------------------------------------------------------------------------


def test_legacy_naming_resolves(data_dir: Path, release_2003q1: Path) -> None:
    """A legacy-era (pre-2015, no underscore) release directory resolves correctly."""
    report = FCACallReport(
        start="2003-03-31",
        end="2003-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    rows = rows_of(report.load(schedule="RC"))
    assert rows[0]["UNINUM"] == 610000
    assert rows[0]["TOTASSETS"] == 500000


def test_load_honors_configured_dataframe_backend(
    data_dir: Path, release_2026q1: Path, backend: str
) -> None:
    """load() returns a native frame of whichever backend is configured.

    The `backend` fixture both parametrizes this across all three backends
    and activates each one for the whole test body, so `load` runs under
    the same backend the assertion expects.
    """
    expected_type = {
        "pandas": pd.DataFrame,
        "polars": pl.DataFrame,
        "pyarrow": pa.Table,
    }[backend]
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    assert isinstance(report.load(schedule="RC"), expected_type)


def test_load_honors_lazy_config_for_polars(
    data_dir: Path, release_2026q1: Path
) -> None:
    """lazy=True with the polars backend returns a polars.LazyFrame."""
    with config_context(dataframe_backend="polars", lazy=True):
        report = FCACallReport(
            start="2026-03-31",
            end="2026-03-31",
            transport=LocalDirectoryTransport(data_dir=data_dir),
        )
        result = report.load(schedule="RC")
    assert isinstance(result, pl.LazyFrame)


@pytest.mark.parametrize(
    "dataframe_type",
    ["pandas", "pyarrow_table", "polars_dataframe", "polars_lazyframe"],
)
def test_load_honors_dataframe_type_override(
    data_dir: Path, release_2026q1: Path, dataframe_type: DataFrameType
) -> None:
    """load() converts its result to `dataframe_type` as a final step."""
    expected_type = {
        "pandas": pd.DataFrame,
        "pyarrow_table": pa.Table,
        "polars_dataframe": pl.DataFrame,
        "polars_lazyframe": pl.LazyFrame,
    }[dataframe_type]
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    result = report.load(schedule="RC", dataframe_type=dataframe_type)
    assert isinstance(result, expected_type)


def test_load_all_passes_dataframe_type_through_to_every_schedule(
    data_dir: Path, release_2026q1: Path
) -> None:
    """load_all() applies `dataframe_type` to every schedule in the result."""
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    result = report.load_all(dataframe_type="pyarrow_table")
    assert result
    assert all(isinstance(frame, pa.Table) for frame in result.values())


@pytest.mark.parametrize(
    "dataframe_type",
    ["pandas", "pyarrow_table", "polars_dataframe", "polars_lazyframe"],
)
def test_load_institutions_honors_dataframe_type_override(
    data_dir: Path, release_2026q1: Path, dataframe_type: DataFrameType
) -> None:
    """load_institutions() converts its result to `dataframe_type` as a final step."""
    expected_type = {
        "pandas": pd.DataFrame,
        "pyarrow_table": pa.Table,
        "polars_dataframe": pl.DataFrame,
        "polars_lazyframe": pl.LazyFrame,
    }[dataframe_type]
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    result = report.load_institutions(dataframe_type=dataframe_type)
    assert isinstance(result, expected_type)


# ---------------------------------------------------------------------------
# to_wide_format
# ---------------------------------------------------------------------------


def test_to_wide_format_default_includes_every_discovered_schedule(
    data_dir: Path, release_2026q1: Path
) -> None:
    """schedules=None (the default) includes every schedule found in range."""
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    wide = report.to_wide_format()
    rows = rows_of(wide)
    assert len(rows) == 1
    row = rows[0]
    assert row["UNINUM"] == 610000
    assert as_date(row["period"]) == date(2026, 3, 31)
    assert row["RC__TOTASSETS"] == 1100000.0
    assert row["RC__TOTLIAB"] == 950000.0
    assert row["RCB__INV_CODE_10__AMOUNT"] == 120.0
    assert row["RCB__INV_CODE_20__AMOUNT2"] == 2.70
    assert row["RCR7__CAPCODE_10__VAL1"] == 111.0
    assert row["RCR7__TOTAL"] == 999.0


def test_to_wide_format_explicit_schedules_narrows_the_result(
    data_dir: Path, release_2026q1: Path
) -> None:
    """An explicit `schedules` includes only those schedules' columns."""
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    wide = report.to_wide_format(schedules=["RC"])
    columns = rows_of(wide)[0]
    assert "RC__TOTASSETS" in columns
    assert not any(name.startswith(("RCB__", "RCR7__")) for name in columns)


def test_to_wide_format_accepts_schedule_enum_members(
    data_dir: Path, release_2026q1: Path
) -> None:
    """`schedules` accepts FCASchedule members, not just strings."""
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    wide = report.to_wide_format(schedules=[FCASchedule.RC])
    assert "RC__TOTASSETS" in rows_of(wide)[0]


def test_to_wide_format_multi_period_grain_and_schema_union(
    data_dir: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """Each (UNINUM, period) is its own row; a schema_policy='union' gap is null."""
    report = FCACallReport(
        start="2025-09-30",
        end="2025-12-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    wide = report.to_wide_format(schedules=["RC"])
    rows = {(row["UNINUM"], as_date(row["period"])): row for row in rows_of(wide)}
    assert len(rows) == 4
    # release_2025q3's RC layout has no TOTLIAB column yet.
    q3_row = rows[(610000, date(2025, 9, 30))]
    assert q3_row["RC__TOTASSETS"] == 1000000.0
    assert is_missing(q3_row["RC__TOTLIAB"])
    q4_row = rows[(610000, date(2025, 12, 31))]
    assert q4_row["RC__TOTASSETS"] == 1050000.0
    assert q4_row["RC__TOTLIAB"] == 900000.0


def test_to_wide_format_absent_named_schedule_raises(
    data_dir: Path, release_2025q3: Path
) -> None:
    """A schedule named explicitly but absent from every period raises."""
    report = FCACallReport(
        start="2025-09-30",
        end="2025-09-30",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    with pytest.raises(ScheduleNotFoundError):
        report.to_wide_format(schedules=["RCR7"])


def test_to_wide_format_empty_schedules_raises(
    data_dir: Path, release_2026q1: Path
) -> None:
    """An explicitly empty `schedules` raises rather than reshaping nothing."""
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    with pytest.raises(ScheduleNotFoundError, match="No schedules to reshape"):
        report.to_wide_format(schedules=[])


@pytest.mark.parametrize(
    "dataframe_type",
    ["pandas", "pyarrow_table", "polars_dataframe", "polars_lazyframe"],
)
def test_to_wide_format_honors_dataframe_type_override(
    data_dir: Path, release_2026q1: Path, dataframe_type: DataFrameType
) -> None:
    """to_wide_format() converts its result to `dataframe_type` as a final step."""
    expected_type = {
        "pandas": pd.DataFrame,
        "pyarrow_table": pa.Table,
        "polars_dataframe": pl.DataFrame,
        "polars_lazyframe": pl.LazyFrame,
    }[dataframe_type]
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    result = report.to_wide_format(dataframe_type=dataframe_type)
    assert isinstance(result, expected_type)


def test_to_wide_format_is_keyword_only(data_dir: Path, release_2026q1: Path) -> None:
    """to_wide_format takes no positional arguments."""
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    with pytest.raises(TypeError):
        report.to_wide_format(["RC"])  # type: ignore[call-overload]


def test_to_wide_format_reshapes_a_lazy_loaded_schedule_correctly(
    data_dir: Path, release_2026q1: Path
) -> None:
    """lazy=True with the polars backend still reshapes correctly end to end."""
    with config_context(dataframe_backend="polars", lazy=True):
        report = FCACallReport(
            start="2026-03-31",
            end="2026-03-31",
            transport=LocalDirectoryTransport(data_dir=data_dir),
        )
        wide = report.to_wide_format(schedules=["RC"])
    assert isinstance(wide, pl.LazyFrame)
    assert wide.collect().to_dicts()[0]["RC__TOTASSETS"] == 1100000.0


def test_to_wide_format_does_not_collect_before_reshaping(
    data_dir: Path, release_2026q1: Path
) -> None:
    """A lazily-loaded schedule is passed to _reshape.to_wide_format still lazy.

    `_to_wide_format` used to call `.collect()` on each schedule
    immediately after loading it, before any melt/concat/pivot work
    started. It no longer does -- this confirms `_reshape.to_wide_format`
    receives a genuine, uncollected `narwhals.LazyFrame` per schedule, so
    the melt/concat/column-key steps can run as one query instead of N
    separate eager materializations.
    """
    from call_report.fca import _reshape

    captured_frames: dict[str, object] = {}
    original = _reshape.to_wide_format

    def spy(*, inputs: dict[str, Any], **kwargs: object) -> object:
        captured_frames.update(
            {schedule: item.frame for schedule, item in inputs.items()}
        )
        return original(inputs=inputs, **kwargs)

    with config_context(dataframe_backend="polars", lazy=True):
        report = FCACallReport(
            start="2026-03-31",
            end="2026-03-31",
            transport=LocalDirectoryTransport(data_dir=data_dir),
        )
        with patch.object(_reshape, "to_wide_format", spy):
            report.to_wide_format(schedules=["RC", "RCB"])

    assert set(captured_frames) == {"RC", "RCB"}
    for frame in captured_frames.values():
        assert isinstance(frame, nw.LazyFrame)


# ---------------------------------------------------------------------------
# to_long_format
# ---------------------------------------------------------------------------


def test_to_long_format_default_includes_every_discovered_schedule(
    data_dir: Path, release_2026q1: Path
) -> None:
    """schedules=None (the default) includes every schedule found in range."""
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    long_ = report.to_long_format()
    rows = rows_of(long_)
    assert len(rows) == 11
    rc_row = next(
        r for r in rows if r["schedule"] == "RC" and r["variable_name"] == "TOTASSETS"
    )
    assert rc_row["UNINUM"] == 610000
    assert as_date(rc_row["period"]) == date(2026, 3, 31)
    assert rc_row["value"] == 1100000.0
    assert rc_row["is_multiple"] is False
    assert is_missing(rc_row["code_column"])
    rcb_row = next(
        r
        for r in rows
        if r["schedule"] == "RCB"
        and r["variable_name"] == "AMOUNT2"
        and r["code_value"] == 20.0
    )
    assert rcb_row["code_column"] == "INV_CODE"
    assert rcb_row["is_multiple"] is True
    assert rcb_row["value"] == 2.70
    rcr7_total = next(
        r for r in rows if r["schedule"] == "RCR7" and r["variable_name"] == "TOTAL"
    )
    assert rcr7_total["is_multiple"] is False
    assert rcr7_total["value"] == 999.0


def test_to_long_format_explicit_schedules_narrows_the_result(
    data_dir: Path, release_2026q1: Path
) -> None:
    """An explicit `schedules` includes only those schedules' rows."""
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    long_ = report.to_long_format(schedules=["RC"])
    rows = rows_of(long_)
    assert {row["schedule"] for row in rows} == {"RC"}
    assert len(rows) == 2


def test_to_long_format_accepts_schedule_enum_members(
    data_dir: Path, release_2026q1: Path
) -> None:
    """`schedules` accepts FCASchedule members, not just strings."""
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    long_ = report.to_long_format(schedules=[FCASchedule.RC])
    assert {row["schedule"] for row in rows_of(long_)} == {"RC"}


def test_to_long_format_multi_period_grain_and_schema_union(
    data_dir: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """Each (UNINUM, period, schedule, variable) is its own row."""
    report = FCACallReport(
        start="2025-09-30",
        end="2025-12-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    long_ = report.to_long_format(schedules=["RC"])
    rows = rows_of(long_)
    # release_2025q3's RC layout has no TOTLIAB column yet, but _load's own
    # schema_policy="union" already null-fills it across periods before
    # to_long_format ever melts -- so q3 still gets a (null-valued) TOTLIAB
    # row: 2 institutions x 2 periods x (TOTASSETS + TOTLIAB) = 8 rows.
    assert len(rows) == 8
    q3_totliab = next(
        r
        for r in rows
        if as_date(r["period"]) == date(2025, 9, 30)
        and r["variable_name"] == "TOTLIAB"
        and r["UNINUM"] == 610000
    )
    assert is_missing(q3_totliab["value"])
    q4_totassets = next(
        r
        for r in rows
        if as_date(r["period"]) == date(2025, 12, 31)
        and r["variable_name"] == "TOTASSETS"
        and r["UNINUM"] == 610000
    )
    assert q4_totassets["value"] == 1050000.0


def test_to_long_format_absent_named_schedule_raises(
    data_dir: Path, release_2025q3: Path
) -> None:
    """A schedule named explicitly but absent from every period raises."""
    report = FCACallReport(
        start="2025-09-30",
        end="2025-09-30",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    with pytest.raises(ScheduleNotFoundError):
        report.to_long_format(schedules=["RCR7"])


def test_to_long_format_empty_schedules_raises(
    data_dir: Path, release_2026q1: Path
) -> None:
    """An explicitly empty `schedules` raises rather than reshaping nothing."""
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    with pytest.raises(ScheduleNotFoundError, match="No schedules to reshape"):
        report.to_long_format(schedules=[])


def test_to_long_format_duplicate_grain_raises(tmp_path: Path) -> None:
    """A genuinely duplicated source row raises ReshapeError."""
    directory = tmp_path / "2026March"
    directory.mkdir()
    write_layout(directory, root="RC", variable_lines=RC_LINES_7COL)
    write_data(
        directory,
        root="RC",
        year=2026,
        month=3,
        rows=[
            "6,10,0,3,2026,610000,1000000",
            "6,10,0,3,2026,610000,9999999",
        ],
    )
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=tmp_path),
    )
    with pytest.raises(ReshapeError, match="not a unique grain"):
        report.to_long_format(schedules=["RC"])


@pytest.mark.parametrize(
    "dataframe_type",
    ["pandas", "pyarrow_table", "polars_dataframe", "polars_lazyframe"],
)
def test_to_long_format_honors_dataframe_type_override(
    data_dir: Path, release_2026q1: Path, dataframe_type: DataFrameType
) -> None:
    """to_long_format() converts its result to `dataframe_type` as a final step."""
    expected_type = {
        "pandas": pd.DataFrame,
        "pyarrow_table": pa.Table,
        "polars_dataframe": pl.DataFrame,
        "polars_lazyframe": pl.LazyFrame,
    }[dataframe_type]
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    result = report.to_long_format(dataframe_type=dataframe_type)
    assert isinstance(result, expected_type)


def test_to_long_format_is_keyword_only(data_dir: Path, release_2026q1: Path) -> None:
    """to_long_format takes no positional arguments."""
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    with pytest.raises(TypeError):
        report.to_long_format(["RC"])  # type: ignore[call-overload]


def test_to_long_format_reshapes_a_lazy_loaded_schedule_correctly(
    data_dir: Path, release_2026q1: Path
) -> None:
    """lazy=True with the polars backend still reshapes correctly end to end."""
    with config_context(dataframe_backend="polars", lazy=True):
        report = FCACallReport(
            start="2026-03-31",
            end="2026-03-31",
            transport=LocalDirectoryTransport(data_dir=data_dir),
        )
        long_ = report.to_long_format(schedules=["RC"])
    assert isinstance(long_, pl.LazyFrame)
    rows = long_.collect().to_dicts()
    row = next(r for r in rows if r["variable_name"] == "TOTASSETS")
    assert row["value"] == 1100000.0


def test_to_long_format_does_not_collect_before_the_grain_check(
    data_dir: Path, release_2026q1: Path
) -> None:
    """A lazily-loaded schedule is passed to _reshape.to_long_format still lazy.

    Mirrors `test_to_wide_format_does_not_collect_before_reshaping`: the
    melt/concat/flag steps stay lazy, with `assert_unique_grain` (inside
    `_reshape.to_long_format`) as the one place a collect happens.
    """
    from call_report.fca import _reshape

    captured_frames: dict[str, object] = {}
    original = _reshape.to_long_format

    def spy(*, inputs: dict[str, Any], **kwargs: object) -> object:
        captured_frames.update(
            {schedule: item.frame for schedule, item in inputs.items()}
        )
        return original(inputs=inputs, **kwargs)

    with config_context(dataframe_backend="polars", lazy=True):
        report = FCACallReport(
            start="2026-03-31",
            end="2026-03-31",
            transport=LocalDirectoryTransport(data_dir=data_dir),
        )
        with patch.object(_reshape, "to_long_format", spy):
            report.to_long_format(schedules=["RC", "RCB"])

    assert set(captured_frames) == {"RC", "RCB"}
    for frame in captured_frames.values():
        assert isinstance(frame, nw.LazyFrame)


# ---------------------------------------------------------------------------
# to_code_grain_format
# ---------------------------------------------------------------------------


def _code_grain_report(data_dir: Path) -> FCACallReport:
    """Build a 2026Q1 report over the fixture releases in `data_dir`."""
    return FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )


def _comparable(code_grain: Any) -> list[dict[str, Any]]:
    """Return a code-grain frame's rows sorted, with missing values normalized.

    Two frames holding the same information can still compare unequal
    because ``nan != nan`` and because each backend spells a missing value
    differently. Both are normalized to ``None`` here.
    """
    rows = [
        {name: None if is_missing(value) else value for name, value in row.items()}
        for row in rows_of(code_grain)
    ]
    return sorted(rows, key=lambda row: (row["code_column"], row["code_value"]))


def test_to_code_grain_format_default_skips_schedules_with_no_code(
    data_dir: Path, release_2026q1: Path
) -> None:
    """schedules=None includes every code-bearing schedule and skips the rest.

    RC reports no code, and RCR7's TOTAL is a trailing single-occurrence
    field, so neither has a code grain. Both are dropped rather than
    adding null-coded rows an aggregation would silently pick up.
    """
    code_grain = _code_grain_report(data_dir).to_code_grain_format()
    rows = rows_of(code_grain)
    assert set(rows[0]) == {
        "UNINUM",
        "period",
        "code_column",
        "code_value",
        "RCB__AMOUNT",
        "RCB__AMOUNT2",
        "RCR7__VAL1",
        "RCR7__VAL2",
    }
    keyed = {(row["code_column"], row["code_value"]): row for row in rows}
    assert set(keyed) == {
        ("INV_CODE", 10.0),
        ("INV_CODE", 20.0),
        ("INV_CODE", 30.0),
        ("CAPCODE", 10.0),
    }
    assert keyed[("INV_CODE", 20.0)]["RCB__AMOUNT"] == 220.0
    assert keyed[("CAPCODE", 10.0)]["RCR7__VAL1"] == 111.0
    assert is_missing(keyed[("CAPCODE", 10.0)]["RCB__AMOUNT"])
    for row in rows:
        assert as_date(row["period"]) == date(2026, 3, 31)
        assert row["UNINUM"] == 610000


def test_to_code_grain_format_explicit_schedules_narrows_the_result(
    data_dir: Path, release_2026q1: Path
) -> None:
    """An explicit `schedules` includes only those schedules' columns."""
    code_grain = _code_grain_report(data_dir).to_code_grain_format(schedules=["RCB"])
    columns = rows_of(code_grain)[0]
    assert "RCB__AMOUNT" in columns
    assert not any(name.startswith("RCR7__") for name in columns)


def test_to_code_grain_format_accepts_schedule_enum_members(
    data_dir: Path, release_2026q1: Path
) -> None:
    """`schedules` accepts FCASchedule members, not just strings."""
    code_grain = _code_grain_report(data_dir).to_code_grain_format(
        schedules=[FCASchedule.RCB]
    )
    assert "RCB__AMOUNT" in rows_of(code_grain)[0]


def test_to_code_grain_format_multi_period_keys_each_period_separately(
    data_dir: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """Each (UNINUM, period, code) is its own row across a multi-quarter range."""
    report = FCACallReport(
        start="2025-09-30",
        end="2025-12-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    code_grain = report.to_code_grain_format(schedules=["RCB"])
    rows = {
        (row["UNINUM"], as_date(row["period"]), row["code_value"]): row
        for row in rows_of(code_grain)
    }
    assert rows[(610000, date(2025, 9, 30), 10.0)]["RCB__AMOUNT"] == 100.0
    assert rows[(610000, date(2025, 12, 31), 10.0)]["RCB__AMOUNT"] == 110.0
    # release_2025q3's RCB is ragged: UNINUM 620000 never reported code 20.
    assert (620000, date(2025, 9, 30), 20.0) not in rows
    assert rows[(620000, date(2025, 12, 31), 20.0)]["RCB__AMOUNT"] == 160.0


def test_to_code_grain_format_named_schedule_with_no_code_raises(
    data_dir: Path, release_2026q1: Path
) -> None:
    """Naming a "single"-scenario schedule is an error, not a silent drop.

    Skipping is what ``schedules=None`` means. An explicit request names a
    schedule the caller expects columns from, and that cannot be honored.
    """
    with pytest.raises(ReshapeError, match=r"\['RC'\] report no code"):
        _code_grain_report(data_dir).to_code_grain_format(schedules=["RC", "RCB"])


def test_to_code_grain_format_absent_named_schedule_raises(
    data_dir: Path, release_2025q3: Path
) -> None:
    """A schedule named explicitly but absent from every period raises.

    The absent schedule has no layout to read a code column from, so it
    must reach `_load` and raise there rather than being misreported as a
    schedule that reports no code.
    """
    report = FCACallReport(
        start="2025-09-30",
        end="2025-09-30",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    with pytest.raises(ScheduleNotFoundError, match="RCR7 was not found"):
        report.to_code_grain_format(schedules=["RCR7"])


def test_to_code_grain_format_empty_schedules_raises(
    data_dir: Path, release_2026q1: Path
) -> None:
    """An explicitly empty `schedules` raises rather than reshaping nothing."""
    with pytest.raises(ScheduleNotFoundError, match="No schedules to reshape"):
        _code_grain_report(data_dir).to_code_grain_format(schedules=[])


def test_to_code_grain_format_no_coded_schedule_in_range_raises(
    data_dir: Path, release_2003q1: Path
) -> None:
    """A range whose only schedules report no code raises under schedules=None.

    The 2003Q1 fixture has RC and the institution roster only. Skipping
    every schedule would leave nothing to build, so this is an error
    rather than an empty frame.
    """
    report = FCACallReport(
        start="2003-03-31",
        end="2003-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    with pytest.raises(ScheduleNotFoundError, match="no code-bearing schedule"):
        report.to_code_grain_format()


@pytest.mark.parametrize(
    "dataframe_type",
    ["pandas", "pyarrow_table", "polars_dataframe", "polars_lazyframe"],
)
def test_to_code_grain_format_honors_dataframe_type_override(
    data_dir: Path, release_2026q1: Path, dataframe_type: DataFrameType
) -> None:
    """to_code_grain_format() converts its result to `dataframe_type` at the end."""
    expected_type = {
        "pandas": pd.DataFrame,
        "pyarrow_table": pa.Table,
        "polars_dataframe": pl.DataFrame,
        "polars_lazyframe": pl.LazyFrame,
    }[dataframe_type]
    result = _code_grain_report(data_dir).to_code_grain_format(
        dataframe_type=dataframe_type
    )
    assert isinstance(result, expected_type)


def test_to_code_grain_format_is_keyword_only(
    data_dir: Path, release_2026q1: Path
) -> None:
    """to_code_grain_format takes no positional arguments."""
    with pytest.raises(TypeError):
        _code_grain_report(data_dir).to_code_grain_format(  # type: ignore[call-overload]
            ["RCB"]
        )


def test_to_code_grain_format_reshapes_a_lazy_loaded_schedule_correctly(
    data_dir: Path, release_2026q1: Path
) -> None:
    """lazy=True with the polars backend still reshapes correctly end to end."""
    with config_context(dataframe_backend="polars", lazy=True):
        code_grain = _code_grain_report(data_dir).to_code_grain_format(
            schedules=["RCB"]
        )
    assert isinstance(code_grain, pl.LazyFrame)
    rows = code_grain.collect().sort("code_value").to_dicts()
    assert [row["RCB__AMOUNT"] for row in rows] == [120.0, 220.0, 320.0]


def test_to_code_grain_format_matches_the_long_format_converter(
    data_dir: Path, release_2026q1: Path
) -> None:
    """The method and the standalone converter agree on real fixture data.

    They share the pivot but not the path to it, so this pins that
    building the code grain from the schedules matches deriving it from
    an already-built long frame.
    """
    report = _code_grain_report(data_dir)
    direct = report.to_code_grain_format(schedules=["RCB", "RCR7"])
    long_ = report.to_long_format(schedules=["RC", "RCB", "RCR7"])
    converted = convert_long_format_to_code_grain_format(long=long_)
    assert _comparable(direct) == _comparable(converted)


# ---------------------------------------------------------------------------
# to_domain_dataset
# ---------------------------------------------------------------------------


def _archive_report(start: str, end: str) -> FCACallReport:
    """Build a report over real packaged releases, for the curated dataset tests.

    The loan portfolio dataset draws on RCF1 and RIE, which the hand-built
    release fixtures elsewhere in this module do not contain. Curation is
    only meaningful against the real schedules it curates.
    """
    return FCACallReport(start=start, end=end, transport=PackagedArchiveTransport())


def _normalize_missing(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace every missing value with None so row dicts compare equal.

    The default `dataframe_type` spells a missing numeric value as
    `float("nan")`, which is never equal to itself, so two row lists that
    are otherwise identical still fail a plain `==` comparison.
    """
    return [
        {key: (None if is_missing(value) else value) for key, value in row.items()}
        for row in rows
    ]


def test_to_domain_dataset_curated_columns_and_grain() -> None:
    """The curated frame is keyed by portfolio and named for what it measures.

    No column carries a schedule prefix, which is the difference from
    `to_code_grain_format` and the reason a series survives a schedule
    split.
    """
    loans = _archive_report("2026-03-31", "2026-03-31").to_domain_dataset(
        domain_dataset="loan_portfolio"
    )
    columns = list(rows_of(loans)[0])
    assert columns[:4] == ["UNINUM", "period", "code_column", "code_value"]
    assert not any("__" in name for name in columns)
    assert {"charge_off", "recovery", "allowance", "accruing"} <= set(columns)
    assert {"non_performing", "net_charge_off"} <= set(columns)


def test_to_domain_dataset_wide_keys_one_row_per_institution_and_period() -> None:
    """wide=True pivots every code into its own {code_value}__{measure} column.

    The value in a wide column must match the same institution/code's
    value on the narrow (default) frame, so the two are just different
    shapes of the same underlying result.
    """
    report = _archive_report("2026-03-31", "2026-03-31")
    narrow = report.to_domain_dataset(domain_dataset="loan_portfolio")
    wide = report.to_domain_dataset(domain_dataset="loan_portfolio", wide=True)

    assert "code_column" not in wide.columns
    assert "code_value" not in wide.columns
    assert "110__accruing" in wide.columns
    narrow_row_list = rows_of(narrow)
    narrow_rows = {(row["UNINUM"], row["code_value"]): row for row in narrow_row_list}
    assert len(rows_of(wide)) == len({row["UNINUM"] for row in narrow_row_list})
    wide_row = next(row for row in rows_of(wide) if row["UNINUM"] == 620000)
    assert wide_row["110__accruing"] == narrow_rows[(620000, 110.0)]["accruing"]


def test_to_domain_dataset_wide_respects_include_totals() -> None:
    """wide=True still honors include_totals, keying the total's own code."""
    report = _archive_report("2026-03-31", "2026-03-31")
    without = report.to_domain_dataset(domain_dataset="loan_portfolio", wide=True)
    with_totals = report.to_domain_dataset(
        domain_dataset="loan_portfolio", include_totals=True, wide=True
    )
    assert "155__total_loans" not in without.columns
    assert "155__total_loans" in with_totals.columns


def test_to_domain_dataset_accepts_an_enum_member() -> None:
    """`domain_dataset` accepts FCADomainDataset members, not just strings."""
    loans = _archive_report("2026-03-31", "2026-03-31").to_domain_dataset(
        domain_dataset=FCADomainDataset.LOAN_PORTFOLIO
    )
    assert "accruing" in rows_of(loans)[0]


def test_to_domain_dataset_unknown_name_raises() -> None:
    """An unknown dataset raises before any release is read."""
    with pytest.raises(DomainDatasetNotFoundError):
        _archive_report("2026-03-31", "2026-03-31").to_domain_dataset(
            domain_dataset="not_a_dataset"
        )


def test_to_domain_dataset_spans_a_schedule_split_in_one_column() -> None:
    """A series crosses the 2023 RIE split without changing column.

    FCA renamed RIE to RIE.2 at 2023Q1 while keeping every field name.
    Naming the output for the schedule would end one series at 2022Q4 and
    start another at 2023Q1. This is the property the curation exists for,
    so it is pinned against real releases on both sides of the boundary.
    """
    loans = _archive_report("2022-09-30", "2023-06-30").to_domain_dataset(
        domain_dataset="loan_portfolio"
    )
    rows = [
        row
        for row in rows_of(loans)
        if row["UNINUM"] == 620000 and row["code_value"] == 110.0
    ]
    by_period = {as_date(row["period"]): row["allowance"] for row in rows}
    assert set(by_period) == {
        date(2022, 9, 30),
        date(2022, 12, 31),
        date(2023, 3, 31),
        date(2023, 6, 30),
    }
    assert all(not is_missing(value) for value in by_period.values())


def test_to_domain_dataset_excludes_totals_by_default() -> None:
    """include_totals defaults to False, so a naive aggregation cannot double count.

    Code 155 is a total RC-F.1 reports rather than a portfolio. Including
    it alongside the portfolios it totals by default would make the most
    natural first operation on the frame, summing a measure per
    institution, silently return roughly double the correct figure.
    """
    report = _archive_report("2026-03-31", "2026-03-31")
    default = report.to_domain_dataset(domain_dataset="loan_portfolio")
    without = report.to_domain_dataset(
        domain_dataset="loan_portfolio", include_totals=False
    )
    assert 155.0 not in {row["code_value"] for row in rows_of(default)}
    assert _normalize_missing(rows_of(default)) == _normalize_missing(rows_of(without))


def test_to_domain_dataset_include_totals_true_adds_the_reported_subtotal() -> None:
    """include_totals=True opts into the source's own reported subtotal rows."""
    report = _archive_report("2026-03-31", "2026-03-31")
    without = report.to_domain_dataset(domain_dataset="loan_portfolio")
    with_totals = report.to_domain_dataset(
        domain_dataset="loan_portfolio", include_totals=True
    )
    assert 155.0 in {row["code_value"] for row in rows_of(with_totals)}
    assert len(rows_of(without)) < len(rows_of(with_totals))


def test_to_domain_dataset_reported_and_derived_net_charge_offs() -> None:
    """One column carries both the reported and the computed net charge-off.

    RI-E reports charge-offs net of recoveries for portfolios 145 and 150,
    which have no gross or recovery figure at all, and gross elsewhere.
    The reported value is kept and the rest are computed, so a null gross
    charge-off never means a null net one.
    """
    loans = _archive_report("2026-03-31", "2026-03-31").to_domain_dataset(
        domain_dataset="loan_portfolio"
    )
    rows = {row["code_value"]: row for row in rows_of(loans) if row["UNINUM"] == 620000}
    reported = rows[145.0]
    assert is_missing(reported["charge_off"])
    assert is_missing(reported["recovery"])
    assert not is_missing(reported["net_charge_off"])

    computed = rows[130.0]
    assert computed["net_charge_off"] == computed["charge_off"] - computed["recovery"]


def test_to_domain_dataset_nonperforming_pair() -> None:
    """The wide nonperforming total exceeds the narrow one by restructured loans.

    Both definitions ship because both are in use. This pins that they
    differ by exactly the one term, rather than by an accident of which
    components each was written with.
    """
    loans = _archive_report("2026-03-31", "2026-03-31").to_domain_dataset(
        domain_dataset="loan_portfolio"
    )
    checked = 0
    for row in rows_of(loans):
        if any(
            is_missing(row[name])
            for name in (
                "non_performing",
                "non_performing_with_restructured",
                "restructured_accruing",
            )
        ):
            continue
        assert (
            row["non_performing_with_restructured"] - row["non_performing"]
            == row["restructured_accruing"]
        )
        checked += 1
    assert checked > 0


def test_to_domain_dataset_reports_null_before_the_data_begins() -> None:
    """A period FCA had not yet collected reports null, never zero.

    RC-F.1 and RI-E carry rows from 2000Q1 but no values until 2005Q1.
    A derived column summing those nulls as zero would claim a measured
    zero where the truth is that nothing was reported.
    """
    loans = _archive_report("2000-03-31", "2000-03-31").to_domain_dataset(
        domain_dataset="loan_portfolio"
    )
    row = next(row for row in rows_of(loans) if row["code_value"] == 110.0)
    assert is_missing(row["accruing"])
    assert is_missing(row["non_performing"])


@pytest.mark.parametrize(
    "dataframe_type",
    ["pandas", "pyarrow_table", "polars_dataframe", "polars_lazyframe"],
)
def test_to_domain_dataset_honors_dataframe_type(
    dataframe_type: DataFrameType,
) -> None:
    """dataframe_type converts the result as a final step."""
    expected_type = {
        "pandas": pd.DataFrame,
        "pyarrow_table": pa.Table,
        "polars_dataframe": pl.DataFrame,
        "polars_lazyframe": pl.LazyFrame,
    }[dataframe_type]
    result = _archive_report("2026-03-31", "2026-03-31").to_domain_dataset(
        domain_dataset="loan_portfolio", dataframe_type=dataframe_type
    )
    assert isinstance(result, expected_type)


def test_to_domain_dataset_is_keyword_only() -> None:
    """to_domain_dataset takes no positional arguments."""
    with pytest.raises(TypeError):
        _archive_report("2026-03-31", "2026-03-31").to_domain_dataset(  # type: ignore[call-overload]
            "loan_portfolio"
        )


def test_to_domain_dataset_under_lazy_polars() -> None:
    """lazy=True with the polars backend still builds the dataset end to end."""
    with config_context(dataframe_backend="polars", lazy=True):
        loans = _archive_report("2026-03-31", "2026-03-31").to_domain_dataset(
            domain_dataset="loan_portfolio"
        )
    assert isinstance(loans, pl.LazyFrame)
    assert "non_performing" in loans.collect().columns


def test_to_domain_dataset_no_declared_schedule_in_range_raises(
    data_dir: Path, release_2026q1: Path
) -> None:
    """A range with none of the dataset's schedules raises rather than empty.

    The hand-built fixture releases carry RC, RCB, and RCR7, none of which
    the loan portfolio dataset draws on. An empty frame would look like a
    quarter with no lending.
    """
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    with pytest.raises(ScheduleNotFoundError, match="loan_portfolio"):
        report.to_domain_dataset(domain_dataset="loan_portfolio")


# ---------------------------------------------------------------------------
# to_domain_dataset: loan_performance
# ---------------------------------------------------------------------------


def test_to_domain_dataset_loan_performance_curated_columns_and_grain() -> None:
    """The curated frame is keyed by performance status and named for aging.

    Unlike loan_portfolio, this bundle draws on one schedule (RC-F) with
    no join, so the point of the test is that the grain and column names
    still come out curated (not schedule-prefixed) even so.
    """
    performance = _archive_report("2026-03-31", "2026-03-31").to_domain_dataset(
        domain_dataset="loan_performance"
    )
    columns = list(rows_of(performance)[0])
    assert columns[:4] == ["UNINUM", "period", "code_column", "code_value"]
    assert not any("__" in name for name in columns)
    assert {"not_past_due", "past_due_30", "past_due_90", "total_past_due"} <= set(
        columns
    )


def test_to_domain_dataset_loan_performance_wide_matches_narrow() -> None:
    """wide=True and wide=False carry exactly the same cells, reshaped.

    The wide frame is re-pivoted from the narrow one after every derived
    row and column is added, so each wide cell equals its narrow
    counterpart exactly.
    """
    report = _archive_report("2026-03-31", "2026-03-31")
    narrow = report.to_domain_dataset(domain_dataset="loan_performance")
    wide = report.to_domain_dataset(domain_dataset="loan_performance", wide=True)

    assert "code_column" not in wide.columns
    assert "code_value" not in wide.columns
    assert "10__not_past_due" in wide.columns

    narrow_rows = {(row["UNINUM"], row["code_value"]): row for row in rows_of(narrow)}
    wide_row = next(row for row in rows_of(wide) if row["UNINUM"] == 620000)
    for code, measure in [
        (10.0, "not_past_due"),
        (54.0, "total"),
        (54.0, "total_past_due"),
        (80.0, "total"),
    ]:
        assert (
            wide_row[f"{int(code)}__{measure}"] == narrow_rows[(620000, code)][measure]
        )


def test_to_domain_dataset_loan_performance_excludes_totals_by_default() -> None:
    """include_totals defaults to False, dropping code 60's reported subtotal."""
    report = _archive_report("2026-03-31", "2026-03-31")
    default = report.to_domain_dataset(domain_dataset="loan_performance")
    with_totals = report.to_domain_dataset(
        domain_dataset="loan_performance", include_totals=True
    )
    assert 60.0 not in {row["code_value"] for row in rows_of(default)}
    assert 60.0 in {row["code_value"] for row in rows_of(with_totals)}


def test_to_domain_dataset_loan_performance_number_of_loans_is_a_count() -> None:
    """Code 80 is the total number of loans, reported in the `total` column.

    RC-F reports only one figure for code 80 and leaves every aging bucket
    empty, so the count is a whole-book total rather than a past due
    count. Its total_past_due is null for the same reason.
    """
    performance = _archive_report("2026-03-31", "2026-03-31").to_domain_dataset(
        domain_dataset="loan_performance", include_totals=True
    )
    rows = {
        row["code_value"]: row
        for row in rows_of(performance)
        if row["UNINUM"] == 620000
    }
    assert is_missing(rows[80.0]["not_past_due"])
    assert is_missing(rows[80.0]["total_past_due"])
    assert not is_missing(rows[80.0]["total"])
    assert rows[80.0]["total"] < rows[60.0]["total"]


def test_to_domain_dataset_loan_performance_total_is_the_row_total() -> None:
    """`total` is not past due plus both past due buckets, on every row.

    RC-F's TOTPDUE includes loans that are not past due. It is off by at
    most a dollar of rounding, so the check allows for that.
    """
    performance = _archive_report("2025-12-31", "2025-12-31").to_domain_dataset(
        domain_dataset="loan_performance", include_totals=True
    )
    dollar_rows = [row for row in rows_of(performance) if row["code_value"] != 80.0]
    assert dollar_rows
    for row in dollar_rows:
        parts = row["not_past_due"] + row["past_due_30"] + row["past_due_90"]
        assert abs(parts - row["total"]) <= 1


def test_to_domain_dataset_loan_performance_total_past_due_excludes_current() -> None:
    """total_past_due is the two past due buckets and nothing else."""
    performance = _archive_report("2025-12-31", "2025-12-31").to_domain_dataset(
        domain_dataset="loan_performance"
    )
    for row in rows_of(performance):
        if row["code_value"] == 80.0:
            continue
        assert row["total_past_due"] == row["past_due_30"] + row["past_due_90"]


def test_to_domain_dataset_loan_performance_nonaccrual_total_sums_its_members() -> None:
    """Code 57 equals codes 54 and 56 added together, on every measure.

    Checked for every institution, not a sample, since the sum is built
    per institution and period.
    """
    performance = _archive_report("2025-12-31", "2025-12-31").to_domain_dataset(
        domain_dataset="loan_performance", include_totals=True
    )
    by_key = {(row["UNINUM"], row["code_value"]): row for row in rows_of(performance)}
    institutions = {uninum for uninum, _ in by_key}
    for uninum in institutions:
        total = by_key[(uninum, 57.0)]
        cash, other = by_key[(uninum, 54.0)], by_key[(uninum, 56.0)]
        for measure in ("not_past_due", "past_due_30", "past_due_90", "total"):
            assert total[measure] == cash[measure] + other[measure], (uninum, measure)
        assert total["total_past_due"] == total["past_due_30"] + total["past_due_90"]


def test_to_domain_dataset_loan_performance_nonaccrual_total_is_a_subtotal() -> None:
    """Code 57 is excluded by default and present with include_totals=True.

    Both member rows stay in the result either way.
    """
    report = _archive_report("2025-12-31", "2025-12-31")
    default = {
        row["code_value"]
        for row in rows_of(report.to_domain_dataset(domain_dataset="loan_performance"))
    }
    with_totals = {
        row["code_value"]
        for row in rows_of(
            report.to_domain_dataset(
                domain_dataset="loan_performance", include_totals=True
            )
        )
    }
    assert 57.0 not in default
    assert {54.0, 56.0} <= default
    assert {54.0, 56.0, 57.0} <= with_totals


@pytest.mark.parametrize(
    "dataframe_type",
    ["pandas", "pyarrow_table", "polars_dataframe", "polars_lazyframe"],
)
def test_to_domain_dataset_loan_performance_nonaccrual_total_every_backend(
    dataframe_type: DataFrameType,
) -> None:
    """The computed row is built under every backend, lazy included."""
    result = _archive_report("2025-12-31", "2025-12-31").to_domain_dataset(
        domain_dataset="loan_performance",
        include_totals=True,
        dataframe_type=dataframe_type,
    )
    assert 57.0 in {row["code_value"] for row in rows_of(result)}


def test_to_domain_dataset_loan_performance_accepts_an_enum_member() -> None:
    """`domain_dataset` accepts the FCADomainDataset member, not just the string."""
    performance = _archive_report("2026-03-31", "2026-03-31").to_domain_dataset(
        domain_dataset=FCADomainDataset.LOAN_PERFORMANCE
    )
    assert "not_past_due" in rows_of(performance)[0]


@pytest.mark.parametrize(
    "dataframe_type",
    ["pandas", "pyarrow_table", "polars_dataframe", "polars_lazyframe"],
)
def test_to_domain_dataset_loan_performance_honors_dataframe_type(
    dataframe_type: DataFrameType,
) -> None:
    """dataframe_type converts the result as a final step, single-source too."""
    expected_type = {
        "pandas": pd.DataFrame,
        "pyarrow_table": pa.Table,
        "polars_dataframe": pl.DataFrame,
        "polars_lazyframe": pl.LazyFrame,
    }[dataframe_type]
    result = _archive_report("2026-03-31", "2026-03-31").to_domain_dataset(
        domain_dataset="loan_performance", dataframe_type=dataframe_type
    )
    assert isinstance(result, expected_type)


def test_to_domain_dataset_loan_performance_no_declared_schedule_in_range_raises(
    data_dir: Path, release_2026q1: Path
) -> None:
    """A range with none of the dataset's schedules raises rather than empty.

    The hand-built fixture releases carry RC, RCB, and RCR7, none of which
    loan_performance draws on.
    """
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    with pytest.raises(ScheduleNotFoundError, match="loan_performance"):
        report.to_domain_dataset(domain_dataset="loan_performance")


_CAPITAL_CONTINUOUS_COLUMNS = (
    "capital_stock",
    "paid_in_capital",
    "allocated_surplus_qualified",
    "allocated_surplus_nonqualified",
    "unallocated_retained_earnings",
    "accumulated_other_comprehensive_income",
    "total_net_worth",
)
"""tuple[str, ...]: The capital columns RI-D reports on both sides of 2017Q1."""


_CAPITAL_BOUNDARY_RESTATEMENTS: dict[int, dict[str, float]] = {
    710056: {
        "unallocated_retained_earnings": -179.0,
        "accumulated_other_comprehensive_income": 4.0,
        "total_net_worth": -175.0,
    },
    710862: {
        "allocated_surplus_nonqualified": -14816.0,
        "unallocated_retained_earnings": 14816.0,
    },
    720186: {
        "allocated_surplus_nonqualified": -1.0,
        "total_net_worth": -1.0,
    },
}
"""dict[int, dict[str, float]]: Institutions that restated across 2017Q1.

Each maps a UNINUM to the change from its 2016Q4 ending balance to its
2017Q1 beginning balance. 710862 moved an amount between two equity
components and left total net worth alone. The other two restated their
opening position outright.
"""


def _capital_boundary_rows() -> tuple[
    dict[int, dict[str, Any]], dict[int, dict[str, Any]]
]:
    """Return every institution's balances on each side of the 2017Q1 boundary.

    Returns
    -------
    tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]
        The 2016Q4 ending balance rows and the 2017Q1 beginning balance
        rows, each keyed by UNINUM.
    """
    rows = rows_of(
        _archive_report("2016-12-31", "2017-03-31").to_domain_dataset(
            domain_dataset="capital"
        )
    )
    ending = {
        int(row["UNINUM"]): row
        for row in rows
        if as_date(row["period"]) == date(2016, 12, 31) and row["code_value"] == 130.0
    }
    beginning = {
        int(row["UNINUM"]): row
        for row in rows
        if as_date(row["period"]) == date(2017, 3, 31) and row["code_value"] == 10.0
    }
    return ending, beginning


def _capital_row(report: FCACallReport, *, period: date, code: float) -> dict[str, Any]:
    """Return one institution's capital row for a period and curated code.

    UNINUM 620000 is used throughout the capital tests because it reports
    every curated code on both sides of the 2017 renumbering.

    Parameters
    ----------
    report : FCACallReport
        The report to read.
    period : datetime.date
        The reporting period the row belongs to.
    code : float
        The curated code the row is keyed by.

    Returns
    -------
    dict[str, Any]
        The matching row.
    """
    rows = [
        row
        for row in rows_of(report.to_domain_dataset(domain_dataset="capital"))
        if row["UNINUM"] == 620000
        and row["code_value"] == code
        and as_date(row["period"]) == period
    ]
    return rows[0]


def test_to_domain_dataset_capital_curated_columns_and_grain() -> None:
    """The capital dataset keys rows by the curated net worth change code."""
    capital = _archive_report("2026-03-31", "2026-03-31").to_domain_dataset(
        domain_dataset="capital"
    )
    row = rows_of(capital)[0]
    assert row["code_column"] == "NET_WORTH_CHANGE"
    for column in ("capital_stock", "paid_in_capital", "total_net_worth"):
        assert column in row


def test_to_domain_dataset_capital_spans_the_2017_renumbering() -> None:
    """Every continuous column carries across the 2017Q1 boundary unchanged.

    RI-D renumbered its codes and renamed or split most of its columns at
    2017Q1 while keeping its root name, so a reader following the raw
    schedule sees a series break there. The ending balance of 2016Q4 and
    the beginning balance of 2017Q1 are the same position stated twice,
    so they pin the crosswalk from both sides.
    """
    report = _archive_report("2016-12-31", "2017-03-31")
    ending = _capital_row(report, period=date(2016, 12, 31), code=130.0)
    beginning = _capital_row(report, period=date(2017, 3, 31), code=10.0)
    for column in _CAPITAL_CONTINUOUS_COLUMNS:
        assert ending[column] == beginning[column], column
    assert ending["total_net_worth"] == 2225248.0


def test_to_domain_dataset_capital_keeps_every_institution_across_the_boundary() -> (
    None
):
    """No institution loses a continuous column at the renumbering.

    A column mapped on only one side of 2017Q1 leaves a null on the
    other, ending or starting that institution's series without saying
    so. Checking one institution cannot catch a mapping that is wrong
    only for the institutions that report the column differently, so
    every institution present on both sides is checked.
    """
    ending, beginning = _capital_boundary_rows()
    shared = sorted(set(ending) & set(beginning))
    assert len(shared) == 81
    unpopulated = [
        (uninum, label, column)
        for uninum in shared
        for label, row in (("2016Q4", ending[uninum]), ("2017Q1", beginning[uninum]))
        for column in _CAPITAL_CONTINUOUS_COLUMNS
        if is_missing(row[column])
    ]
    assert unpopulated == []


def test_to_domain_dataset_capital_carries_every_institution_across_the_boundary() -> (
    None
):
    """Each institution's 2016Q4 ending balance is its 2017Q1 beginning balance.

    Three institutions restated their opening position between the two
    filings. Their changes are stated in
    `_CAPITAL_BOUNDARY_RESTATEMENTS` rather than absorbed by a tolerance,
    so a crosswalk change that moved them shows up as a failure. A column
    or code mapped wrongly would move every institution rather than three.
    """
    ending, beginning = _capital_boundary_rows()
    moved = {}
    for uninum in sorted(set(ending) & set(beginning)):
        changes = {
            column: beginning[uninum][column] - ending[uninum][column]
            for column in _CAPITAL_CONTINUOUS_COLUMNS
            if beginning[uninum][column] != ending[uninum][column]
        }
        if changes:
            moved[uninum] = changes
    assert moved == _CAPITAL_BOUNDARY_RESTATEMENTS


def test_to_domain_dataset_capital_gives_every_institution_the_same_codes() -> None:
    """The code crosswalk resolves to one vocabulary for every institution.

    Both eras have to land on the same codes for every institution, not
    just for one that happens to report every code.
    """
    rows = rows_of(
        _archive_report("2016-12-31", "2017-03-31").to_domain_dataset(
            domain_dataset="capital"
        )
    )
    codes_by_institution: dict[tuple[int, date], set[int]] = {}
    for row in rows:
        key = (int(row["UNINUM"]), as_date(row["period"]))
        codes_by_institution.setdefault(key, set()).add(int(row["code_value"]))
    expected = {10, 25, 35, 70, 75, 80, 85, 120, 130}
    assert {frozenset(codes) for codes in codes_by_institution.values()} == {
        frozenset(expected)
    }


def test_to_domain_dataset_capital_roll_ups_hold_for_every_institution() -> None:
    """Each derived roll-up equals its parts on every post-2017 row.

    The roll-up is what keeps the two split columns continuous, so it has
    to hold for every institution and every code, not only the beginning
    balance of a sampled one.
    """
    rows = rows_of(
        _archive_report("2017-03-31", "2017-03-31").to_domain_dataset(
            domain_dataset="capital"
        )
    )
    roll_ups = {
        "capital_stock": (
            "capital_stock_purchased",
            "capital_stock_allocated",
            "preferred_stock_perpetual",
            "preferred_stock_other",
        ),
        "allocated_surplus_nonqualified": (
            "allocated_surplus_nonqualified_subject_to_retirement",
            "allocated_surplus_nonqualified_not_subject_to_retirement",
        ),
    }
    mismatched = []
    for row in rows:
        for column, parts in roll_ups.items():
            reported = [row[part] for part in parts if not is_missing(row[part])]
            # A row where every part is null is a measure the source did
            # not report, and stays null rather than becoming zero.
            expected = sum(reported) if reported else None
            actual = None if is_missing(row[column]) else row[column]
            if actual != expected:
                mismatched.append((int(row["UNINUM"]), int(row["code_value"]), column))
    assert mismatched == []


def test_to_domain_dataset_capital_rolls_up_the_split_columns() -> None:
    """The post-2017 stock columns roll up into the pre-2017 aggregate column.

    RI-D replaced ``CAP`` with four separate stock columns at 2017Q1. The
    derived roll-up is what keeps `capital_stock` continuous, and it must
    equal the parts it is computed from.
    """
    report = _archive_report("2017-03-31", "2017-03-31")
    row = _capital_row(report, period=date(2017, 3, 31), code=10.0)
    assert row["capital_stock"] == 351155.0
    assert (
        row["capital_stock_purchased"]
        + row["capital_stock_allocated"]
        + row["preferred_stock_perpetual"]
        + row["preferred_stock_other"]
        == row["capital_stock"]
    )


def test_to_domain_dataset_capital_does_not_overwrite_a_reported_column() -> None:
    """Before 2017 `capital_stock` is RI-D's own figure, not the derived sum.

    The four columns the roll-up is computed from do not exist yet, so a
    derived column that overwrote rather than filled would blank the
    series out for the whole pre-2017 era.
    """
    report = _archive_report("2016-12-31", "2017-03-31")
    row = _capital_row(report, period=date(2016, 12, 31), code=130.0)
    assert row["capital_stock"] == 351155.0
    assert is_missing(row["capital_stock_purchased"])


def test_to_domain_dataset_capital_merges_the_codes_2017_split() -> None:
    """Codes RI-D split in 2017 are summed back onto one curated code.

    Net income (35) and other comprehensive income (45) populate disjoint
    columns, so summing them restores exactly the shape old code 60 had:
    the earnings movement in one column and the OCI movement in another.
    """
    report = _archive_report("2016-12-31", "2017-03-31")
    before = _capital_row(report, period=date(2016, 12, 31), code=35.0)
    after = _capital_row(report, period=date(2017, 3, 31), code=35.0)
    assert before["unallocated_retained_earnings"] == 101154.0
    assert before["accumulated_other_comprehensive_income"] == -50277.0
    assert after["unallocated_retained_earnings"] == 82925.0
    assert after["accumulated_other_comprehensive_income"] == -4238.0
    assert after["total_net_worth"] == 78687.0


def test_to_domain_dataset_capital_merges_two_old_codes_into_one() -> None:
    """Retirements of stock and of preferred stock sum onto one curated code.

    Pre-2017 RI-D reported them as codes 100 and 117. Their ``CAP``
    figures for this institution and period are -19357 and -20000, and the
    curated row has to carry the total rather than either one.
    """
    report = _archive_report("2016-12-31", "2016-12-31")
    row = _capital_row(report, period=date(2016, 12, 31), code=85.0)
    assert row["capital_stock"] == -39357.0
    assert row["total_net_worth"] == -39392.0


def test_to_domain_dataset_capital_drops_the_amended_beginning_balance() -> None:
    """Old code 30 never reaches the output, in any era.

    It duplicates the beginning balance whenever there is no restatement
    and has no counterpart from 2017Q1 onward.
    """
    report = _archive_report("2000-03-31", "2026-03-31")
    codes = {
        row["code_value"]
        for row in rows_of(report.to_domain_dataset(domain_dataset="capital"))
    }
    assert 30.0 not in codes


def test_to_domain_dataset_capital_reports_only_its_declared_codes() -> None:
    """Every code in the output is one the bundle declares, across all history.

    A code_map states only what changes, so a code FCA adds later would
    pass through unmapped and reach the output with no label. Running the
    whole archive is what catches that.
    """
    report = _archive_report("2000-03-31", "2026-03-31")
    codes = {
        int(row["code_value"])
        for row in rows_of(report.to_domain_dataset(domain_dataset="capital"))
    }
    dataset = get_fca_domain_dataset(domain_dataset="capital")
    assert codes == {item.code for item in dataset.codes}


def test_to_domain_dataset_capital_keeps_an_unreported_measure_null() -> None:
    """Summing merged codes leaves a measure the source never reported null.

    A measure of zero and a measure the source did not report say
    different things, and the aggregation that merges codes must not turn
    the second into the first. RI-D leaves every stock column blank on
    its net income row.
    """
    report = _archive_report("2016-12-31", "2016-12-31")
    row = _capital_row(report, period=date(2016, 12, 31), code=35.0)
    assert is_missing(row["capital_stock"])
    assert row["unallocated_retained_earnings"] == 101154.0


def test_to_domain_dataset_capital_omits_a_retired_column_after_its_last_period() -> (
    None
):
    """`surplus_reserve` is absent from a frame covering only later periods.

    RI-D reported it through 2016Q4 and no column replaced it.
    """
    report = _archive_report("2025-03-31", "2025-03-31")
    row = _capital_row(report, period=date(2025, 3, 31), code=130.0)
    assert "surplus_reserve" not in row
    report = _archive_report("2016-12-31", "2016-12-31")
    row = _capital_row(report, period=date(2016, 12, 31), code=130.0)
    assert "surplus_reserve" in row


def test_to_domain_dataset_capital_wide_matches_narrow() -> None:
    """The wide frame is a deterministic re-pivot of the narrow one."""
    report = _archive_report("2026-03-31", "2026-03-31")
    narrow = rows_of(report.to_domain_dataset(domain_dataset="capital"))
    wide = rows_of(report.to_domain_dataset(domain_dataset="capital", wide=True))
    by_uninum = {row["UNINUM"]: row for row in wide}
    for row in narrow:
        code = int(row["code_value"])
        wide_row = by_uninum[row["UNINUM"]]
        assert wide_row[f"{code}__total_net_worth"] == row["total_net_worth"]


@pytest.mark.parametrize(
    "dataframe_type",
    ["pandas", "pyarrow_table", "polars_dataframe", "polars_lazyframe"],
)
def test_to_domain_dataset_capital_honors_dataframe_type(
    dataframe_type: DataFrameType,
) -> None:
    """A remapped, aggregated dataset converts under every backend.

    The code remap joins on a float code value and the merge groups and
    sums, neither of which the other bundles exercise.
    """
    expected_type = {
        "pandas": pd.DataFrame,
        "pyarrow_table": pa.Table,
        "polars_dataframe": pl.DataFrame,
        "polars_lazyframe": pl.LazyFrame,
    }[dataframe_type]
    result = _archive_report("2026-03-31", "2026-03-31").to_domain_dataset(
        domain_dataset="capital", dataframe_type=dataframe_type
    )
    assert isinstance(result, expected_type)


def test_to_domain_dataset_capital_under_lazy_polars() -> None:
    """The remap and the merge both stay lazy until the pivot collects."""
    with config_context(dataframe_backend="polars", lazy=True):
        capital = _archive_report("2016-12-31", "2017-03-31").to_domain_dataset(
            domain_dataset="capital"
        )
    rows = [row for row in rows_of(capital) if row["UNINUM"] == 620000]
    assert {int(row["code_value"]) for row in rows} == {
        10,
        25,
        35,
        70,
        75,
        80,
        85,
        120,
        130,
    }


# ---------------------------------------------------------------------------
# to_domain_dataset: allowance_for_credit_losses
# ---------------------------------------------------------------------------


def test_to_domain_dataset_allowance_curated_columns_and_grain() -> None:
    """The curated frame is keyed by rollforward stage and named for asset class."""
    allowance = _archive_report("2026-03-31", "2026-03-31").to_domain_dataset(
        domain_dataset="allowance_for_credit_losses"
    )
    columns = list(rows_of(allowance)[0])
    assert columns[:4] == ["UNINUM", "period", "code_column", "code_value"]
    assert not any("__" in name for name in columns)
    assert {"loans_and_leases", "htm_debt_securities", "afs_debt_securities"} <= set(
        columns
    )


def test_to_domain_dataset_allowance_spans_the_2023_split_via_continues() -> None:
    """loans_and_leases is one continuous column across the RI-E to RI-E.1 split.

    RI-E1 replaced RI-E's flat institution-level fields with a coded
    rollforward at 2023Q1. RI-E's `continues` declaration is what keeps
    this series whole rather than truncating it at the boundary.
    """
    allowance = _archive_report("2022-09-30", "2023-06-30").to_domain_dataset(
        domain_dataset="allowance_for_credit_losses"
    )
    rows = [
        row
        for row in rows_of(allowance)
        if row["UNINUM"] == 620000 and row["code_value"] == 10.0
    ]
    by_period = {as_date(row["period"]): row["loans_and_leases"] for row in rows}
    assert set(by_period) == {
        date(2022, 9, 30),
        date(2022, 12, 31),
        date(2023, 3, 31),
        date(2023, 6, 30),
    }
    assert all(not is_missing(value) for value in by_period.values())
    # The ending balance one quarter must equal the next quarter's beginning.
    ending = {
        as_date(row["period"]): row["loans_and_leases"]
        for row in rows_of(allowance)
        if row["UNINUM"] == 620000 and row["code_value"] == 70.0
    }
    assert ending[date(2022, 12, 31)] == by_period[date(2023, 3, 31)]


def test_to_domain_dataset_allowance_asset_class_detail_null_before_2023() -> None:
    """htm_debt_securities/afs_debt_securities are new RI-E1 detail, null before it.

    RI-E never reported an asset-class breakdown, only loans, so those two
    columns have nothing to report before 2023Q1, over a range that spans
    the split so both columns exist in the result at all.
    """
    allowance = _archive_report("2022-09-30", "2023-03-31").to_domain_dataset(
        domain_dataset="allowance_for_credit_losses"
    )
    before = next(
        row
        for row in rows_of(allowance)
        if row["UNINUM"] == 620000
        and row["code_value"] == 10.0
        and as_date(row["period"]) == date(2022, 9, 30)
    )
    after = next(
        row
        for row in rows_of(allowance)
        if row["UNINUM"] == 620000
        and row["code_value"] == 10.0
        and as_date(row["period"]) == date(2023, 3, 31)
    )
    assert is_missing(before["htm_debt_securities"])
    assert is_missing(before["afs_debt_securities"])
    assert not is_missing(before["loans_and_leases"])
    assert not is_missing(after["htm_debt_securities"])


def test_to_domain_dataset_allowance_wide_matches_narrow() -> None:
    """wide=True and wide=False carry exactly the same cells, reshaped."""
    report = _archive_report("2026-03-31", "2026-03-31")
    narrow = report.to_domain_dataset(domain_dataset="allowance_for_credit_losses")
    wide = report.to_domain_dataset(
        domain_dataset="allowance_for_credit_losses", wide=True
    )

    assert "code_column" not in wide.columns
    assert "code_value" not in wide.columns
    assert "10__loans_and_leases" in wide.columns

    narrow_rows = {(row["UNINUM"], row["code_value"]): row for row in rows_of(narrow)}
    wide_row = next(row for row in rows_of(wide) if row["UNINUM"] == 620000)
    for code, measure in [
        (10.0, "loans_and_leases"),
        (70.0, "loans_and_leases"),
        (10.0, "htm_debt_securities"),
    ]:
        assert (
            wide_row[f"{int(code)}__{measure}"] == narrow_rows[(620000, code)][measure]
        )


def test_to_domain_dataset_allowance_accepts_an_enum_member() -> None:
    """`domain_dataset` accepts the FCADomainDataset member, not just the string."""
    allowance = _archive_report("2026-03-31", "2026-03-31").to_domain_dataset(
        domain_dataset=FCADomainDataset.ALLOWANCE_FOR_CREDIT_LOSSES
    )
    assert "loans_and_leases" in rows_of(allowance)[0]


@pytest.mark.parametrize(
    "dataframe_type",
    ["pandas", "pyarrow_table", "polars_dataframe", "polars_lazyframe"],
)
def test_to_domain_dataset_allowance_honors_dataframe_type(
    dataframe_type: DataFrameType,
) -> None:
    """dataframe_type converts the result as a final step, continues source too."""
    expected_type = {
        "pandas": pd.DataFrame,
        "pyarrow_table": pa.Table,
        "polars_dataframe": pl.DataFrame,
        "polars_lazyframe": pl.LazyFrame,
    }[dataframe_type]
    result = _archive_report("2026-03-31", "2026-03-31").to_domain_dataset(
        domain_dataset="allowance_for_credit_losses", dataframe_type=dataframe_type
    )
    assert isinstance(result, expected_type)


def test_to_domain_dataset_allowance_no_declared_schedule_in_range_raises(
    data_dir: Path, release_2026q1: Path
) -> None:
    """A range with none of the dataset's schedules raises rather than empty.

    The hand-built fixture releases carry RC, RCB, and RCR7, none of which
    allowance_for_credit_losses draws on.
    """
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    with pytest.raises(ScheduleNotFoundError, match="allowance_for_credit_losses"):
        report.to_domain_dataset(domain_dataset="allowance_for_credit_losses")


_ASSET_TRANSFER_PAIRS = ((10, 20), (30, 40), (50, 60), (70, 80), (90, 100), (110, 120))
"""tuple[tuple[int, int], ...]: RC-O's codes, paired as (purchased, sold)."""


def test_to_domain_dataset_asset_transfers_curated_columns_and_grain() -> None:
    """Rows are keyed by asset type and direction, with two named measures."""
    transfers = _archive_report("2026-03-31", "2026-03-31").to_domain_dataset(
        domain_dataset="asset_transfers"
    )
    row = rows_of(transfers)[0]
    assert row["code_column"] == "ASSET_TYPE"
    assert row["DIRECTION"] in {"Purchased", "Sold"}
    assert "amortized_cost" in row
    assert "fair_value" in row


def test_to_domain_dataset_asset_transfers_keeps_every_code_pair_apart() -> None:
    """Each RC-O code lands on its own asset type and direction, unsummed.

    Two codes mapping to one curated code are summed everywhere else, so
    the direction has to be part of the row key or a purchased balance
    and a sold balance would be added together.
    """
    report = _archive_report("2025-03-31", "2025-03-31")
    raw = {
        int(row["code_value"]): row["RCO__TRANSWFCI"]
        for row in rows_of(report.to_code_grain_format(schedules=["RCO"]))
        if row["UNINUM"] == 620000
    }
    curated = {
        (int(row["code_value"]), row["DIRECTION"]): row["amortized_cost"]
        for row in rows_of(report.to_domain_dataset(domain_dataset="asset_transfers"))
        if row["UNINUM"] == 620000
    }
    for purchased, sold in _ASSET_TRANSFER_PAIRS:
        assert curated[(purchased, "Purchased")] == raw[purchased]
        assert curated[(purchased, "Sold")] == raw[sold]


def test_to_domain_dataset_asset_transfers_covers_every_institution() -> None:
    """Every institution gets all six asset types in both directions.

    A direction supplied per code rather than per row would leave some
    institutions short a row, which one sampled institution would not
    show.
    """
    rows = rows_of(
        _archive_report("2025-03-31", "2025-03-31").to_domain_dataset(
            domain_dataset="asset_transfers"
        )
    )
    by_institution: dict[int, set[tuple[int, str]]] = {}
    for row in rows:
        key = (int(row["code_value"]), row["DIRECTION"])
        by_institution.setdefault(int(row["UNINUM"]), set()).add(key)
    expected = {
        (code, direction)
        for pair in _ASSET_TRANSFER_PAIRS
        for code in (pair[0],)
        for direction in ("Purchased", "Sold")
    }
    assert {frozenset(keys) for keys in by_institution.values()} == {
        frozenset(expected)
    }


def test_to_domain_dataset_asset_transfers_later_codes_start_when_they_start() -> None:
    """RC-O added code pairs over time, and the dataset does not invent them.

    Codes 90 and 100 begin at 2007Q1 and 110 and 120 at 2013Q1, so an
    earlier period carries only the asset types RC-O reported then.
    """
    early = rows_of(
        _archive_report("2005-03-31", "2005-03-31").to_domain_dataset(
            domain_dataset="asset_transfers"
        )
    )
    assert {int(row["code_value"]) for row in early} == {10, 30, 50, 70}


def test_to_domain_dataset_asset_transfers_wide_names_both_dimensions() -> None:
    """The wide column key carries the direction as well as the asset type."""
    report = _archive_report("2025-03-31", "2025-03-31")
    narrow = rows_of(report.to_domain_dataset(domain_dataset="asset_transfers"))
    wide = rows_of(
        report.to_domain_dataset(domain_dataset="asset_transfers", wide=True)
    )
    by_uninum = {row["UNINUM"]: row for row in wide}
    for row in narrow:
        key = f"{int(row['code_value'])}_{row['DIRECTION']}__amortized_cost"
        assert by_uninum[row["UNINUM"]][key] == row["amortized_cost"]


@pytest.mark.parametrize(
    "dataframe_type",
    ["pandas", "pyarrow_table", "polars_dataframe", "polars_lazyframe"],
)
def test_to_domain_dataset_asset_transfers_honors_dataframe_type(
    dataframe_type: DataFrameType,
) -> None:
    """A dataset with a second row key column converts under every backend."""
    expected_type = {
        "pandas": pd.DataFrame,
        "pyarrow_table": pa.Table,
        "polars_dataframe": pl.DataFrame,
        "polars_lazyframe": pl.LazyFrame,
    }[dataframe_type]
    result = _archive_report("2026-03-31", "2026-03-31").to_domain_dataset(
        domain_dataset="asset_transfers", dataframe_type=dataframe_type
    )
    assert isinstance(result, expected_type)


def test_to_domain_dataset_asset_transfers_under_lazy_polars() -> None:
    """The split join and the grouped merge both stay lazy until the pivot."""
    with config_context(dataframe_backend="polars", lazy=True):
        transfers = _archive_report("2025-03-31", "2025-03-31").to_domain_dataset(
            domain_dataset="asset_transfers"
        )
    rows = [row for row in rows_of(transfers) if row["UNINUM"] == 620000]
    assert len(rows) == 12
    assert {row["DIRECTION"] for row in rows} == {"Purchased", "Sold"}


_INVESTMENT_MEASURES = (
    "amortized_cost",
    "fair_value",
    "available_for_sale_amortized_cost",
    "available_for_sale_fair_value",
)
"""tuple[str, ...]: The four measure columns of the investments dataset."""


def _raw_investment_codes(
    report: FCACallReport, uninum: int
) -> dict[int, dict[str, Any]]:
    """Return one institution's raw RC-B rows, keyed by INV_CODE.

    Parameters
    ----------
    report : FCACallReport
        A report covering a single period.
    uninum : int
        The institution to select.

    Returns
    -------
    dict[int, dict[str, Any]]
        Each raw code's row from `to_code_grain_format`.
    """
    return {
        int(row["code_value"]): row
        for row in rows_of(report.to_code_grain_format(schedules=["RCB"]))
        if row["UNINUM"] == uninum
    }


def _investments_by_code(
    report: FCACallReport, uninum: int
) -> dict[int, dict[str, Any]]:
    """Return one institution's curated investments rows, totals included.

    Parameters
    ----------
    report : FCACallReport
        A report covering a single period.
    uninum : int
        The institution to select.

    Returns
    -------
    dict[int, dict[str, Any]]
        Each curated code's row from `to_domain_dataset`.
    """
    return {
        int(row["code_value"]): row
        for row in rows_of(
            report.to_domain_dataset(domain_dataset="investments", include_totals=True)
        )
        if row["UNINUM"] == uninum
    }


def test_to_domain_dataset_investments_curated_columns_and_grain() -> None:
    """Rows are keyed by security type, with four named measures."""
    investments = _archive_report("2026-03-31", "2026-03-31").to_domain_dataset(
        domain_dataset="investments"
    )
    row = rows_of(investments)[0]
    assert row["code_column"] == "INVESTMENT_TYPE"
    assert set(_INVESTMENT_MEASURES) <= set(row)


def test_to_domain_dataset_investments_total_sums_its_members() -> None:
    """Code 98 equals the sum of every security type, for every institution.

    Checked at 2026Q1, where the allowance exists, and at 2008Q4, where
    diversified investment funds do. Neither is part of the sum.
    """
    for period in ("2026-03-31", "2008-12-31"):
        rows = rows_of(
            _archive_report(period, period).to_domain_dataset(
                domain_dataset="investments", include_totals=True
            )
        )
        totals = {row["UNINUM"]: row for row in rows if row["code_value"] == 98.0}
        members: dict[int, float] = {}
        for row in rows:
            if row["code_value"] in (85.0, 98.0, 180.0):
                continue
            value = 0.0 if is_missing(row["amortized_cost"]) else row["amortized_cost"]
            members[row["UNINUM"]] = members.get(row["UNINUM"], 0.0) + value
        assert totals
        for uninum, total in totals.items():
            assert total["amortized_cost"] == members[uninum], (period, uninum)


def test_to_domain_dataset_investments_total_backs_out_diversified_funds() -> None:
    """Before 2015, RC-B's total included funds, and code 98 does not.

    UNINUM 610000 is the one institution that reported diversified
    investment funds (code 85). Its code 98 is its reported total 80 less
    those funds, while code 85 stays in the result as its own row.
    """
    report = _archive_report("2008-12-31", "2008-12-31")
    raw = _raw_investment_codes(report, 610000)
    curated = _investments_by_code(report, 610000)
    for measure, variable in (("amortized_cost", "BKVAL"), ("fair_value", "MKTVAL")):
        reported = raw[80][f"RCB__{variable}"]
        funds = raw[85][f"RCB__{variable}"]
        assert funds > 0
        assert curated[98][measure] == reported - funds
        assert curated[85][measure] == funds


def test_to_domain_dataset_investments_total_is_gross_of_the_allowance() -> None:
    """From 2023Q1, RC-B's total 99 is net of the allowance, and code 98 is not.

    UNINUM 722918 reported an allowance (code 180) at 2025Q3. Its code 98
    is its reported total plus that allowance, which stays in the result
    as its own row.
    """
    report = _archive_report("2025-09-30", "2025-09-30")
    raw = _raw_investment_codes(report, 722918)
    curated = _investments_by_code(report, 722918)
    allowance = raw[180]["RCB__BKVAL"]
    assert allowance > 0
    assert curated[98]["amortized_cost"] == raw[99]["RCB__BKVAL"] + allowance
    assert curated[180]["amortized_cost"] == allowance


def test_to_domain_dataset_investments_crosswalks_code_11_to_17() -> None:
    """A balance under retired code 11 continues as code 17 across 2015Q1."""
    before = _archive_report("2014-12-31", "2014-12-31")
    after = _archive_report("2015-03-31", "2015-03-31")
    raw_before = _raw_investment_codes(before, 722502)
    curated_before = _investments_by_code(before, 722502)
    curated_after = _investments_by_code(after, 722502)
    assert 11 not in curated_before
    assert curated_before[17]["amortized_cost"] == raw_before[11]["RCB__BKVAL"]
    assert curated_after[17]["amortized_cost"] > 0


def test_to_domain_dataset_investments_never_reports_the_dropped_codes() -> None:
    """RC-B's reported totals and summary codes never reach the curated frame.

    2008Q4 carries codes 80, 100, 120, and 130, 2013Q4 carries the
    150-157 summary codes, and 2026Q1 carries code 99.
    """
    dropped = {80, 99, 100, 120, 130, *range(150, 159), *range(171, 175)}
    for period in ("2008-12-31", "2013-12-31", "2026-03-31"):
        report = _archive_report(period, period)
        raw = {
            int(row["code_value"])
            for row in rows_of(report.to_code_grain_format(schedules=["RCB"]))
        }
        curated = {
            int(row["code_value"])
            for row in rows_of(
                report.to_domain_dataset(
                    domain_dataset="investments", include_totals=True
                )
            )
        }
        assert raw & dropped, period
        assert not curated & dropped, period


def test_to_domain_dataset_investments_excludes_the_total_by_default() -> None:
    """Code 98 is a subtotal, so it appears only with include_totals=True."""
    report = _archive_report("2026-03-31", "2026-03-31")
    default = {
        row["code_value"]
        for row in rows_of(report.to_domain_dataset(domain_dataset="investments"))
    }
    assert 98.0 not in default
    assert 180.0 in default


@pytest.mark.parametrize(
    "dataframe_type",
    ["pandas", "pyarrow_table", "polars_dataframe", "polars_lazyframe"],
)
def test_to_domain_dataset_investments_every_backend(
    dataframe_type: DataFrameType,
) -> None:
    """The remapped, dropped, and computed codes all work under every backend."""
    result = _archive_report("2014-12-31", "2014-12-31").to_domain_dataset(
        domain_dataset="investments",
        include_totals=True,
        dataframe_type=dataframe_type,
    )
    codes = {row["code_value"] for row in rows_of(result)}
    assert {17.0, 98.0} <= codes
    assert not codes & {11.0, 80.0}


# ---------------------------------------------------------------------------
# FCACallReport.available_domain_datasets
# ---------------------------------------------------------------------------


def test_available_domain_datasets_lists_every_member() -> None:
    """available_domain_datasets returns every FCADomainDataset member.

    The domain-dataset counterpart to `available_schedules`, and does not
    require `fetch` to have run.
    """
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=PackagedArchiveTransport(),
    )
    assert report.available_domain_datasets() == tuple(FCADomainDataset)
    assert FCADomainDataset.LOAN_PORTFOLIO in report.available_domain_datasets()


# ---------------------------------------------------------------------------
# to_wide_format / to_long_format round-trip equivalence
# ---------------------------------------------------------------------------


def test_wide_and_long_format_carry_the_same_information(
    data_dir: Path, release_2026q1: Path
) -> None:
    """to_wide_format and to_long_format, converted into each other, agree.

    `release_2026q1` has no gaps (every institution has every code it
    reports), so this fixture doesn't hit the pivot grid-completion
    null-row case documented on `convert_wide_format_to_long_format` --
    see `test_release_archive.py` for that with real, gappy data.
    """
    report = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    wide = report.to_wide_format()
    long_ = report.to_long_format()

    converted_long = nw.from_native(convert_wide_format_to_long_format(wide=wide))
    converted_wide = nw.from_native(convert_long_format_to_wide_format(long=long_))
    assert isinstance(converted_long, nw.DataFrame)
    assert isinstance(converted_wide, nw.DataFrame)

    long_frame = nw.from_native(long_)
    wide_frame = nw.from_native(wide)
    assert isinstance(long_frame, nw.DataFrame)
    assert isinstance(wide_frame, nw.DataFrame)

    long_cols = sorted(long_frame.columns)
    converted_long_rows = (
        converted_long.select(long_cols).sort(long_cols).rows(named=True)
    )
    long_rows = long_frame.select(long_cols).sort(long_cols).rows(named=True)
    assert _normalizerows_of(converted_long_rows) == _normalizerows_of(long_rows)

    wide_cols = sorted(wide_frame.columns)
    converted_wide_rows = (
        converted_wide.select(wide_cols).sort(wide_cols).rows(named=True)
    )
    wide_rows = wide_frame.select(wide_cols).sort(wide_cols).rows(named=True)
    assert _normalizerows_of(converted_wide_rows) == _normalizerows_of(wide_rows)


def _normalizerows_of(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace any NaN value with None, so row-dict equality isn't NaN != NaN."""
    return [
        {key: (None if is_missing(value) else value) for key, value in row.items()}
        for row in rows
    ]


def test_period_column_carries_a_date_dtype_on_every_output(
    backend: str, data_dir: Path, release_2025q4: Path
) -> None:
    """Every frame a report returns carries a typed period, not an object column.

    `period` holds a `datetime.date`, which pandas has no dtype for, so it
    used to land in an object column under the default backend. That
    supports no ``.dt`` accessor and does not survive a parquet round
    trip. `_with_period_column` is the one place period is built, so
    every entry point below is fixed by the same cast.
    """
    report = FCACallReport(
        start="2025-12-31",
        end="2025-12-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    frames = {
        "load": report.load(schedule="RC"),
        "load_institutions": report.load_institutions(),
        "to_wide_format": report.to_wide_format(schedules=["RC"]),
        "to_long_format": report.to_long_format(schedules=["RC"]),
    }
    for label, frame in frames.items():
        dtype = nw.from_native(frame).collect_schema()["period"]
        assert dtype == date_dtype(), f"{label}: period is {dtype}."


@pytest.mark.parametrize(
    "dataframe_type",
    ["pandas", "polars_dataframe", "polars_lazyframe", "pyarrow_table"],
)
def test_period_stays_typed_when_converted_to_another_dataframe_type(
    backend: str, dataframe_type: DataFrameType, data_dir: Path, release_2025q4: Path
) -> None:
    """A dataframe_type's period dtype depends on that type, not on the backend.

    Conversion does not translate between the two representations of a
    calendar date on its own. A Date handed to pandas becomes an object
    column of `datetime.date` values, and a Datetime handed to polars
    stays a Datetime, so the same requested type used to produce
    different dtypes depending on which backend was configured.

    pandas is Datetime because it has no date dtype. Every other type is
    Date. Both hold the same quarter end.
    """
    report = FCACallReport(
        start="2025-12-31",
        end="2025-12-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    result = report.load(schedule="RC", dataframe_type=dataframe_type)
    dtype = nw.from_native(result).collect_schema()["period"]
    expected = nw.Datetime("us") if dataframe_type == "pandas" else nw.Date()
    assert dtype == expected
