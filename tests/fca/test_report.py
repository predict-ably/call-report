"""End-to-end tests for the FCACallReport estimator-style entry point."""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path
from typing import Any

import narwhals as nw
import pytest

from call_report.config import config_context
from call_report.core import ReportingPeriod
from call_report.core._backend import DataFrameType
from call_report.exceptions import (
    DownloadError,
    InvalidPeriodError,
    LayoutParseError,
    PeriodNotAvailableError,
    ScheduleNotFoundError,
)
from call_report.fca import FCACallReport, FCASchedule
from call_report.fca.layout import FCALayout
from call_report.fca.transport import LocalDirectoryTransport
from tests.conftest import write_data, write_layout
from tests.fca.conftest import RC_LINES_7COL


def _rows(native_frame: Any) -> list[dict[str, Any]]:
    return nw.from_native(native_frame).rows(named=True)


def _is_missing(value: object) -> bool:
    """Return True for a missing value, however the active backend represents it.

    pandas represents a missing numeric value as NaN (a float); polars and
    pyarrow use an actual None -- both count as "missing" here.
    """
    return value is None or (isinstance(value, float) and math.isnan(value))


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
    rows = _rows(result)
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
    rows = _rows(result)
    assert {"period", "UNINUM"} <= set(rows[0])
    periods_seen = {r["period"] for r in rows}
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
    rows = _rows(report.load(schedule="RC"))
    assert "TOTLIAB" in rows[0]
    q3_rows = [r for r in rows if r["period"] == date(2025, 9, 30)]
    q4_rows = [r for r in rows if r["period"] == date(2025, 12, 31)]
    assert all(_is_missing(r["TOTLIAB"]) for r in q3_rows)
    assert all(not _is_missing(r["TOTLIAB"]) for r in q4_rows)


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
    rows = _rows(report.load(schedule="RC"))
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
    rows = _rows(report.load(schedule="RCR7"))
    q3 = ReportingPeriod.from_period_end(value="2025-09-30")
    q4 = ReportingPeriod.from_period_end(value="2025-12-31")
    assert all(r["period"] == date(2025, 12, 31) for r in rows)
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
    rows = _rows(report.load(schedule="RC"))
    assert len(rows) == 1
    assert rows[0]["period"] == date(2025, 9, 30)

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
    by_string = _rows(report.load(schedule="rcb"))
    report2 = FCACallReport(
        start="2026-03-31",
        end="2026-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    by_enum = _rows(report2.load(schedule=FCASchedule.RCB))
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
    rows = _rows(report.load_institutions())
    assert rows[0]["UNINUM"] == 610000
    assert rows[0]["SHORTNAME"] == "Café Ridge FCB"
    assert rows[0]["period"] == date(2026, 3, 31)


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
    rows = _rows(report.load_institutions())
    assert len(rows) == 1
    assert rows[0]["period"] == date(2025, 9, 30)
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
# Legacy vs. modern naming, backend/lazy configuration
# ---------------------------------------------------------------------------


def test_legacy_naming_resolves(data_dir: Path, release_2003q1: Path) -> None:
    """A legacy-era (pre-2015, no underscore) release directory resolves correctly."""
    report = FCACallReport(
        start="2003-03-31",
        end="2003-03-31",
        transport=LocalDirectoryTransport(data_dir=data_dir),
    )
    rows = _rows(report.load(schedule="RC"))
    assert rows[0]["UNINUM"] == 610000
    assert rows[0]["TOTASSETS"] == 500000


@pytest.mark.parametrize("backend", ["pandas", "polars", "pyarrow"])
def test_load_honors_configured_dataframe_backend(
    data_dir: Path, release_2026q1: Path, backend: str
) -> None:
    """load() returns a native frame of whichever backend is configured."""
    import pandas as pd
    import polars as pl
    import pyarrow as pa

    expected_type = {
        "pandas": pd.DataFrame,
        "polars": pl.DataFrame,
        "pyarrow": pa.Table,
    }[backend]
    with config_context(dataframe_backend=backend):
        report = FCACallReport(
            start="2026-03-31",
            end="2026-03-31",
            transport=LocalDirectoryTransport(data_dir=data_dir),
        )
        result = report.load(schedule="RC")
    assert isinstance(result, expected_type)


def test_load_honors_lazy_config_for_polars(
    data_dir: Path, release_2026q1: Path
) -> None:
    """lazy=True with the polars backend returns a polars.LazyFrame."""
    import polars as pl

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
    import pandas as pd
    import polars as pl
    import pyarrow as pa

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
    import pyarrow as pa

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
    import pandas as pd
    import polars as pl
    import pyarrow as pa

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
    rows = _rows(wide)
    assert len(rows) == 1
    row = rows[0]
    assert row["UNINUM"] == 610000
    assert row["period"] == date(2026, 3, 31)
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
    columns = _rows(wide)[0]
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
    assert "RC__TOTASSETS" in _rows(wide)[0]


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
    rows = {(row["UNINUM"], row["period"]): row for row in _rows(wide)}
    assert len(rows) == 4
    # release_2025q3's RC layout has no TOTLIAB column yet.
    q3_row = rows[(610000, date(2025, 9, 30))]
    assert q3_row["RC__TOTASSETS"] == 1000000.0
    assert _is_missing(q3_row["RC__TOTLIAB"])
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
    import pandas as pd
    import polars as pl
    import pyarrow as pa

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


def test_to_wide_format_collects_a_lazy_loaded_schedule(
    data_dir: Path, release_2026q1: Path
) -> None:
    """lazy=True with the polars backend still reshapes correctly (collects first)."""
    import polars as pl

    with config_context(dataframe_backend="polars", lazy=True):
        report = FCACallReport(
            start="2026-03-31",
            end="2026-03-31",
            transport=LocalDirectoryTransport(data_dir=data_dir),
        )
        wide = report.to_wide_format(schedules=["RC"])
    assert isinstance(wide, pl.LazyFrame)
    assert wide.collect().to_dicts()[0]["RC__TOTASSETS"] == 1100000.0
