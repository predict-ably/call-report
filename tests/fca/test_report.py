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
from call_report.core import ReportingPeriod
from call_report.core._backend import DataFrameType, date_dtype
from call_report.exceptions import (
    DownloadError,
    InvalidPeriodError,
    LayoutParseError,
    PeriodNotAvailableError,
    ReshapeError,
    ScheduleNotFoundError,
)
from call_report.fca import (
    FCACallReport,
    FCASchedule,
    convert_long_format_to_wide_format,
    convert_wide_format_to_long_format,
    get_fca_file_metadata,
)
from call_report.fca.layout import FCALayout
from call_report.fca.transport import LocalDirectoryTransport
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

    def spy(*, frames: dict[str, object], **kwargs: object) -> object:
        captured_frames.update(frames)
        return original(frames=frames, **kwargs)  # type: ignore[arg-type]

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

    def spy(*, frames: dict[str, object], **kwargs: object) -> object:
        captured_frames.update(frames)
        return original(frames=frames, **kwargs)  # type: ignore[arg-type]

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
