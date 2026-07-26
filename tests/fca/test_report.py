"""End-to-end tests for the FCACallReport estimator-style entry point."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import narwhals as nw
import pytest

from call_report.config import config_context
from call_report.exceptions import (
    DownloadError,
    InvalidPeriodError,
    LayoutParseError,
    PeriodNotAvailableError,
    ScheduleNotFoundError,
)
from call_report.fca import FCACallReport, FCASchedule
from call_report.fca.layout import FCALayout
from call_report.periods import ReportingPeriod


def _rows(native_frame: object) -> list[dict[str, object]]:
    return nw.from_native(native_frame).rows(named=True)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# __init__ / sklearn-style conventions
# ---------------------------------------------------------------------------


def test_init_stores_params_verbatim_and_does_no_work() -> None:
    """Constructing with an invalid quarter-end does not raise -- only fetch() does."""
    report = FCACallReport(start="not-a-date", end=None)
    assert report.start == "not-a-date"
    assert report.end is None
    assert report.schema_policy == "union"


def test_constructor_is_keyword_only() -> None:
    """FCACallReport takes no positional arguments."""
    with pytest.raises(TypeError):
        FCACallReport("2024-03-31")  # type: ignore[misc]


def test_repr_echoes_quarter_end_dates_passed() -> None:
    """__repr__ shows the raw quarter-end date strings the user passed in."""
    report = FCACallReport(start="2024-03-31", end="2025-12-31")
    text = repr(report)
    assert "2024-03-31" in text
    assert "2025-12-31" in text


def test_get_params_and_set_params() -> None:
    """get_params/set_params follow the sklearn convention."""
    report = FCACallReport(start="2024-03-31", end="2025-12-31")
    params = report.get_params()
    assert params["start"] == "2024-03-31"
    assert params["end"] == "2025-12-31"
    assert params["schema_policy"] == "union"

    same_report = report.set_params(schema_policy="intersection")
    assert same_report is report
    assert report.get_params()["schema_policy"] == "intersection"


def test_set_params_rejects_unknown_key() -> None:
    """set_params raises for a parameter name the estimator doesn't accept."""
    report = FCACallReport(start="2024-03-31", end="2025-12-31")
    with pytest.raises(ValueError, match="not_a_real_param"):
        report.set_params(not_a_real_param=True)


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


def test_fetch_missing_end_raises_informative_error(tmp_path: Path) -> None:
    """Omitting end is ambiguous and must raise, not silently mean "one quarter"."""
    report = FCACallReport(start="2026-03-31", data_dir=tmp_path)
    with pytest.raises(InvalidPeriodError, match="end"):
        report.fetch()


def test_fetch_invalid_quarter_end_raises(tmp_path: Path) -> None:
    """fetch() validates start/end and raises InvalidPeriodError for a bad date."""
    report = FCACallReport(start="2026-05-15", end="2026-06-30", data_dir=tmp_path)
    with pytest.raises(InvalidPeriodError):
        report.fetch()


def test_fetch_out_of_catalog_bounds_raises(tmp_path: Path) -> None:
    """fetch() raises PeriodNotAvailableError for a range FCA has never published."""
    report = FCACallReport(start="1999-12-31", end="1999-12-31", data_dir=tmp_path)
    with pytest.raises(PeriodNotAvailableError):
        report.fetch()


def test_fetch_without_data_dir_or_transport_raises_download_error() -> None:
    """With neither data_dir nor transport supplied, fetch() fails clearly."""
    report = FCACallReport(start="2026-03-31", end="2026-03-31")
    with pytest.raises(DownloadError, match=r"data_dir|transport"):
        report.fetch()


def test_fetch_returns_self_and_populates_fitted_state(
    data_dir: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """fetch() returns self and sets periods_/releases_/schedules_/errors_."""
    report = FCACallReport(start="2025-09-30", end="2025-12-31", data_dir=data_dir)
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
    # RCX only exists in the 2025-Q4 fixture.
    assert report.schedules_[FCASchedule.RCX] == (q4,)


def test_fetch_records_missing_period_directory_without_failing_whole_request(
    tmp_path: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """Skip period with no resolvable local directory."""
    # data_dir has Sept & Dec 2025 on disk, but the requested range also
    # includes June 2025, which was never built.
    report = FCACallReport(start="2025-06-30", end="2025-12-31", data_dir=tmp_path)
    report.fetch()

    assert len(report.periods_) == 3
    assert len(report.releases_) == 2
    june = ReportingPeriod.from_period_end(value="2025-06-30")
    assert len(report.errors_) == 1
    issue = report.errors_[0]
    assert issue.period == june
    assert issue.schedule is None
    assert isinstance(issue.error, DownloadError)


def test_available_periods_and_schedules_do_not_require_fetch(tmp_path: Path) -> None:
    """available_periods()/available_schedules() reflect catalog totals.

    No fetch() needed.
    """
    report = FCACallReport(start="2025-09-30", end="2025-12-31", data_dir=tmp_path)
    periods = report.available_periods()
    assert periods[0] == ReportingPeriod.from_period_end(value="2000-03-31")
    assert periods[-1] == ReportingPeriod.from_period_end(value="2026-03-31")
    assert FCASchedule.RCB in report.available_schedules()


# ---------------------------------------------------------------------------
# load()
# ---------------------------------------------------------------------------


def test_load_auto_fetches_if_needed(data_dir: Path, release_2026q1: Path) -> None:
    """load() calls fetch() automatically when it hasn't run yet."""
    report = FCACallReport(start="2026-03-31", end="2026-03-31", data_dir=data_dir)
    result = report.load(schedule="RC")
    rows = _rows(result)
    assert len(rows) == 1
    assert rows[0]["UNINUM"] == 610000


def test_load_result_carries_period_and_uninum_columns(
    data_dir: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """Every stacked frame carries a period column (the quarter-end date) and uninum."""
    report = FCACallReport(start="2025-09-30", end="2025-12-31", data_dir=data_dir)
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
        start="2025-09-30", end="2025-12-31", schema_policy="union", data_dir=data_dir
    )
    rows = _rows(report.load(schedule="RC"))
    assert "TOTLIAB" in rows[0]
    q3_rows = [r for r in rows if r["period"] == date(2025, 9, 30)]
    q4_rows = [r for r in rows if r["period"] == date(2025, 12, 31)]
    assert all(r["TOTLIAB"] is None for r in q3_rows)
    assert all(r["TOTLIAB"] is not None for r in q4_rows)


def test_load_schema_policy_intersection_drops_uncommon_columns(
    data_dir: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """Intersection keeps only columns common to every stacked period."""
    report = FCACallReport(
        start="2025-09-30",
        end="2025-12-31",
        schema_policy="intersection",
        data_dir=data_dir,
    )
    rows = _rows(report.load(schedule="RC"))
    assert "TOTLIAB" not in rows[0]
    assert "TOTASSETS" in rows[0]


def test_load_schema_policy_strict_raises_on_mismatch(
    data_dir: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """Strict refuses to silently reconcile differing schemas across periods."""
    report = FCACallReport(
        start="2025-09-30", end="2025-12-31", schema_policy="strict", data_dir=data_dir
    )
    with pytest.raises(LayoutParseError):
        report.load(schedule="RC")


def test_load_schedule_missing_in_some_periods_returns_partial_result(
    data_dir: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """A schedule absent in some, but not all, periods yields a partial result."""
    report = FCACallReport(start="2025-09-30", end="2025-12-31", data_dir=data_dir)
    rows = _rows(report.load(schedule="RCX"))
    q3 = ReportingPeriod.from_period_end(value="2025-09-30")
    q4 = ReportingPeriod.from_period_end(value="2025-12-31")
    assert all(r["period"] == date(2025, 12, 31) for r in rows)
    assert report.periods_available(schedule="RCX") == (q4,)
    assert report.periods_missing(schedule="RCX") == (q3,)


def test_load_schedule_not_found_when_absent_everywhere(
    data_dir: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """A schedule present in zero requested periods raises ScheduleNotFoundError."""
    report = FCACallReport(start="2025-09-30", end="2025-12-31", data_dir=data_dir)
    with pytest.raises(ScheduleNotFoundError):
        report.load(schedule="RCF1")


def test_load_accepts_case_insensitive_schedule_string(
    data_dir: Path, release_2026q1: Path
) -> None:
    """load() accepts either the FCASchedule enum or a case-insensitive string."""
    report = FCACallReport(start="2026-03-31", end="2026-03-31", data_dir=data_dir)
    by_string = _rows(report.load(schedule="rcb"))
    report2 = FCACallReport(start="2026-03-31", end="2026-03-31", data_dir=data_dir)
    by_enum = _rows(report2.load(schedule=FCASchedule.RCB))
    assert by_string == by_enum


def test_load_all_returns_every_discovered_schedule(
    data_dir: Path, release_2026q1: Path
) -> None:
    """load_all() returns a dict keyed by every schedule found in range."""
    report = FCACallReport(start="2026-03-31", end="2026-03-31", data_dir=data_dir)
    result = report.load_all()
    assert set(result) == {FCASchedule.RC, FCASchedule.RCB, FCASchedule.RCX}


def test_load_institutions(data_dir: Path, release_2026q1: Path) -> None:
    """load_institutions() returns the institution roster with period/uninum."""
    report = FCACallReport(start="2026-03-31", end="2026-03-31", data_dir=data_dir)
    rows = _rows(report.load_institutions())
    assert rows[0]["UNINUM"] == 610000
    assert rows[0]["SHORTNAME"] == "Café Ridge FCB"
    assert rows[0]["period"] == date(2026, 3, 31)


# ---------------------------------------------------------------------------
# get_layout()
# ---------------------------------------------------------------------------


def test_get_layout_with_period_returns_single_layout(
    data_dir: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """Passing a specific period returns just that period's FCALayout."""
    report = FCACallReport(start="2025-09-30", end="2025-12-31", data_dir=data_dir)
    layout = report.get_layout(schedule="RC", period="2025-09-30")
    assert isinstance(layout, FCALayout)
    assert "TOTLIAB" not in layout.leading_columns


def test_get_layout_without_period_returns_dict_across_range(
    data_dir: Path, release_2025q3: Path, release_2025q4: Path
) -> None:
    """Omitting period returns a dict showing the layout for each period in range."""
    report = FCACallReport(start="2025-09-30", end="2025-12-31", data_dir=data_dir)
    layouts = report.get_layout(schedule="RC")
    assert isinstance(layouts, dict)
    q3 = ReportingPeriod.from_period_end(value="2025-09-30")
    q4 = ReportingPeriod.from_period_end(value="2025-12-31")
    assert "TOTLIAB" not in layouts[q3].leading_columns
    assert "TOTLIAB" in layouts[q4].leading_columns


def test_get_layout_is_keyword_only(data_dir: Path, release_2026q1: Path) -> None:
    """get_layout takes no positional arguments."""
    report = FCACallReport(start="2026-03-31", end="2026-03-31", data_dir=data_dir)
    with pytest.raises(TypeError):
        report.get_layout("RC")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Legacy vs. modern naming, backend/lazy configuration
# ---------------------------------------------------------------------------


def test_legacy_naming_resolves(data_dir: Path, release_2003q1: Path) -> None:
    """A legacy-era (pre-2015, no underscore) release directory resolves correctly."""
    report = FCACallReport(start="2003-03-31", end="2003-03-31", data_dir=data_dir)
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
        report = FCACallReport(start="2026-03-31", end="2026-03-31", data_dir=data_dir)
        result = report.load(schedule="RC")
    assert isinstance(result, expected_type)


def test_load_honors_lazy_config_for_polars(
    data_dir: Path, release_2026q1: Path
) -> None:
    """lazy=True with the polars backend returns a polars.LazyFrame."""
    import polars as pl

    with config_context(dataframe_backend="polars", lazy=True):
        report = FCACallReport(start="2026-03-31", end="2026-03-31", data_dir=data_dir)
        result = report.load(schedule="RC")
    assert isinstance(result, pl.LazyFrame)
