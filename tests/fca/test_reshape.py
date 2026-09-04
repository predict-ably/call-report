"""Tests for the private wide-format reshaping helpers (call_report.fca._reshape)."""

from __future__ import annotations

from datetime import date
from typing import Any

import narwhals as nw
import polars as pl
import pyarrow as pa
import pytest

from call_report.config import config_context
from call_report.core._backend import build_frame, concat
from call_report.exceptions import ReshapeError
from call_report.fca._domain_datasets import (
    DomainDataset,
    DomainDatasetColumn,
    DomainDatasetDerived,
    DomainDatasetSource,
)
from call_report.fca._reshape import (
    CODE_GRAIN_INDEX,
    LONG_FORMAT_COLUMNS,
    NO_DOMAIN_DATASET_MEASUREMENTS,
    _cast_numeric_to_float64,
    _parse_wide_column_key,
    _with_column_key,
    _with_is_multiple_flag,
    _with_plain_column_key,
    add_derived_columns,
    apply_domain_dataset_decoding,
    assert_pivot_has_measurements,
    convert_long_format_to_code_grain_format,
    convert_long_format_to_wide_format,
    convert_wide_format_to_long_format,
    exclude_reported_totals,
    melt_schedule_frame,
    pivot_domain_dataset_wide,
    to_code_grain_format,
    to_long_format,
    to_wide_format,
)
from tests.helpers import is_missing, rows_of, sorted_rows

# ---------------------------------------------------------------------------
# melt_schedule_frame
# ---------------------------------------------------------------------------


def test_melt_schedule_frame_no_code_column() -> None:
    """A plain (no-code) schedule melts every non-identifier column."""
    frame = build_frame(
        data={
            "SYSTEM": [6, 6],
            "UNINUM": [1, 2],
            "period": ["2026-03-31", "2026-03-31"],
            "TOTASSETS": [100, 200],
            "TOTLIAB": [50, 90],
        }
    )
    melted = melt_schedule_frame(frame=frame, schedule="RC", code_column=None)
    assert isinstance(melted, nw.DataFrame)
    assert set(melted.columns) == {
        "UNINUM",
        "period",
        "schedule",
        "variable_name",
        "value",
    }
    assert melted.shape[0] == 4
    rows = melted.rows(named=True)
    assert {row["schedule"] for row in rows} == {"RC"}
    assert {row["variable_name"] for row in rows} == {"TOTASSETS", "TOTLIAB"}


def test_melt_schedule_frame_with_code_column() -> None:
    """A code-bearing schedule keeps the code as `code_value`, not a melted row."""
    frame = build_frame(
        data={
            "SYSTEM": [6, 6],
            "UNINUM": [1, 1],
            "period": ["2026-03-31", "2026-03-31"],
            "INV_CODE": [10, 20],
            "BKVAL": [100, 150],
            "MKTVAL": [110.0, 160.0],
        }
    )
    melted = melt_schedule_frame(frame=frame, schedule="RCB", code_column="INV_CODE")
    assert isinstance(melted, nw.DataFrame)
    assert set(melted.columns) == {
        "UNINUM",
        "period",
        "schedule",
        "variable_name",
        "value",
        "code_column",
        "code_value",
    }
    assert melted.shape[0] == 4
    assert set(melted["code_column"].to_list()) == {"INV_CODE"}
    assert set(melted["code_value"].to_list()) == {10, 20}


def test_melt_schedule_frame_trailing_columns_not_duplicated_per_code() -> None:
    """A trailing (single_multiple_single) column produces one row per grain."""
    frame = build_frame(
        data={
            "UNINUM": [1, 1, 2],
            "period": ["2026-03-31"] * 3,
            "CAPCODE": [10, 20, 10],
            "VAL1": [100, 150, 300],
            "VAL2": [200, 250, 400],
            "TOTAL": [999, 999, 888],
        }
    )
    melted = melt_schedule_frame(
        frame=frame,
        schedule="RCR7",
        code_column="CAPCODE",
        trailing_columns=("TOTAL",),
    )
    assert isinstance(melted, nw.DataFrame)
    # 4 coded rows (2 measures x 2 code-rows for UNINUM 1) + 2 for UNINUM 2's
    # single code-row, plus exactly one TOTAL row per (UNINUM, period) grain.
    coded_rows = melted.filter(nw.col("variable_name") != "TOTAL")
    trailing_rows = melted.filter(nw.col("variable_name") == "TOTAL")
    assert coded_rows.shape[0] == 6
    assert trailing_rows.shape[0] == 2
    trailing_values = {
        row["UNINUM"]: row["value"] for row in trailing_rows.rows(named=True)
    }
    assert trailing_values == {1: 999.0, 2: 888.0}
    code_column_values = trailing_rows["code_column"].to_list()
    assert all(value is None or value != value for value in code_column_values)


def test_melt_schedule_frame_is_keyword_only() -> None:
    """melt_schedule_frame takes no positional arguments."""
    frame = build_frame(data={"UNINUM": [1], "period": ["2026-03-31"], "A": [1]})
    with pytest.raises(TypeError):
        melt_schedule_frame(frame, "RC", None)  # type: ignore[call-arg]


def test_melt_schedule_frame_empty_input_produces_float64_value(
    polars_backend: str,
) -> None:
    """A zero-row schedule still gets Float64 `value`, not `Unknown`.

    RCO has zero rows at 2000Q1, so this is a real shape. An uncast
    `Unknown` column can raise `polars.exceptions.SchemaError` when later
    concatenated against a populated schedule's numeric `value` column.
    See `test_cast_numeric_to_float64_casts_an_unknown_dtype_column`.
    """
    frame = build_frame(data={"UNINUM": [], "period": [], "TOTASSETS": []})
    melted = melt_schedule_frame(frame=frame, schedule="RCO", code_column=None)
    assert melted.collect_schema()["value"] == nw.Float64()


def test_melt_schedule_frame_empty_coded_input_produces_float64_code_value(
    polars_backend: str,
) -> None:
    """A zero-row *coded* schedule gets Float64 `value` and `code_value`."""
    frame = build_frame(data={"UNINUM": [], "period": [], "INV_CODE": [], "BKVAL": []})
    melted = melt_schedule_frame(frame=frame, schedule="RCB", code_column="INV_CODE")
    assert melted.collect_schema()["value"] == nw.Float64()
    assert melted.collect_schema()["code_value"] == nw.Float64()


# ---------------------------------------------------------------------------
# _cast_numeric_to_float64
# ---------------------------------------------------------------------------


def test_cast_numeric_to_float64_casts_an_int_column() -> None:
    """An Int64 column is cast to Float64."""
    frame = build_frame(data={"value": [1, 2, 3]})
    assert frame.schema["value"] == nw.Int64()
    result = _cast_numeric_to_float64(frame, column="value")
    assert result.schema["value"] == nw.Float64()


def test_cast_numeric_to_float64_leaves_non_numeric_untouched() -> None:
    """A String column is left as-is."""
    frame = build_frame(data={"value": ["a", "b"]})
    result = _cast_numeric_to_float64(frame, column="value")
    assert result.schema["value"] == nw.String()


def test_cast_numeric_to_float64_works_on_a_different_column_name() -> None:
    """The column to normalize is a parameter, not hardcoded to "value"."""
    frame = build_frame(data={"code_value": [10, 20]})
    result = _cast_numeric_to_float64(frame, column="code_value")
    assert result.schema["code_value"] == nw.Float64()


def test_cast_numeric_to_float64_casts_an_unknown_dtype_column(
    polars_backend: str,
) -> None:
    """An empty column, whose dtype is Unknown, is cast to Float64 too.

    Regression test for ``polars.exceptions.SchemaError: type Int64 is
    incompatible with expected type Null``.

    Two real shapes leave narwhals unable to infer a concrete dtype: a
    schedule with zero rows for a period, such as RCO at 2000Q1, and a
    field that is entirely null though rows exist, such as RCF1's
    ``value`` at 2000Q1. Both yield `Unknown`, which `is_numeric()`
    reports False for. Left uncast, concatenating that against a real
    Int64 or Float64 piece can raise, depending on the installed polars
    version and on concat order.

    This test uses polars specifically: pandas infers Float64 directly
    for an empty column, so it never produces the `Unknown` this guards.
    """
    frame = build_frame(data={"value": []})
    dtype = frame.collect_schema()["value"]
    assert isinstance(dtype, nw.Unknown)
    result = _cast_numeric_to_float64(frame, column="value")
    assert result.collect_schema()["value"] == nw.Float64()


# ---------------------------------------------------------------------------
# _with_column_key
# ---------------------------------------------------------------------------


def test_with_column_key_plain_naming_when_no_code_column_present() -> None:
    """A frame with no `code_column` at all gets `{schedule}__{variable}` keys."""
    frame = build_frame(
        data={
            "UNINUM": [1],
            "period": ["2026-03-31"],
            "schedule": ["RC"],
            "variable_name": ["TOTASSETS"],
            "value": [100.0],
        }
    )
    result = _with_column_key(frame)
    assert result["column_key"].to_list() == ["RC__TOTASSETS"]


def test_with_column_key_coded_naming() -> None:
    """A coded row gets `{schedule}__{code_column}_{code_value}__{variable}`."""
    frame = build_frame(
        data={
            "UNINUM": [1],
            "period": ["2026-03-31"],
            "schedule": ["RCB"],
            "variable_name": ["BKVAL"],
            "value": [100.0],
            "code_column": ["INV_CODE"],
            "code_value": [15],
        }
    )
    result = _with_column_key(frame)
    assert result["column_key"].to_list() == ["RCB__INV_CODE_15__BKVAL"]


def test_with_column_key_mixed_null_and_codedsorted_rows() -> None:
    """A union of coded and non-coded schedules keys each row correctly."""
    coded = build_frame(
        data={
            "UNINUM": [1],
            "period": ["2026-03-31"],
            "schedule": ["RCB"],
            "variable_name": ["BKVAL"],
            "value": [100.0],
            "code_column": ["INV_CODE"],
            "code_value": [15],
        }
    )
    plain = build_frame(
        data={
            "UNINUM": [1],
            "period": ["2026-03-31"],
            "schedule": ["RC"],
            "variable_name": ["TOTASSETS"],
            "value": [200.0],
            "code_column": [None],
            "code_value": [None],
        }
    )
    combined = concat(frames=[coded, plain], how="union")
    result = _with_column_key(combined)
    keys = dict(
        zip(
            result["schedule"].to_list(),
            result["column_key"].to_list(),
            strict=True,
        )
    )
    assert keys["RCB"] == "RCB__INV_CODE_15__BKVAL"
    assert keys["RC"] == "RC__TOTASSETS"


# ---------------------------------------------------------------------------
# to_wide_format
# ---------------------------------------------------------------------------


def test_to_wide_format_combines_coded_and_plain_schedules() -> None:
    """to_wide_format stacks a code-bearing and a plain schedule correctly."""
    rc = build_frame(
        data={
            "UNINUM": [1, 2],
            "period": ["2026-03-31", "2026-03-31"],
            "TOTASSETS": [1000, 2000],
        }
    )
    rcb = build_frame(
        data={
            "UNINUM": [1, 1],
            "period": ["2026-03-31", "2026-03-31"],
            "INV_CODE": [10, 20],
            "BKVAL": [100, 150],
        }
    )
    result = to_wide_format(
        frames={"RC": rc, "RCB": rcb},
        code_columns={"RC": None, "RCB": "INV_CODE"},
        trailing_columns={"RC": (), "RCB": ()},
    )
    rows = {row["UNINUM"]: row for row in sorted_rows(result)}
    assert rows[1]["RC__TOTASSETS"] == 1000.0
    assert rows[1]["RCB__INV_CODE_10__BKVAL"] == 100.0
    assert rows[1]["RCB__INV_CODE_20__BKVAL"] == 150.0
    assert rows[2]["RC__TOTASSETS"] == 2000.0


def test_to_wide_format_duplicate_grain_raises_reshape_error() -> None:
    """A genuinely duplicated (UNINUM, period, variable) row raises ReshapeError."""
    rc = build_frame(
        data={
            "UNINUM": [1, 1],
            "period": ["2026-03-31", "2026-03-31"],
            "TOTASSETS": [1000, 9999],
        }
    )
    with pytest.raises(ReshapeError):
        to_wide_format(
            frames={"RC": rc},
            code_columns={"RC": None},
            trailing_columns={"RC": ()},
        )


def test_to_wide_format_empty_schedule_alongside_a_populated_one() -> None:
    """A zero-row schedule concatenated with a real one doesn't raise.

    Regression test for a real CI failure:
    ``polars.exceptions.SchemaError: type Int64 is incompatible with
    expected type Null`` -- triggered by a genuinely empty schedule (RCO
    at 2000Q1) whose `value` column has no data to infer a dtype from
    (`Unknown`), concatenated against a populated schedule's real
    Int64/Float64 `value` column.

    `period` is attached via `nw.lit(...)` rather than passed as raw
    frame data, matching how `FCACallReport._load` builds it.
    `_with_period_column` always writes a concrete date literal regardless
    of row count, so unlike `UNINUM` and `value`, `period` can never be
    `Unknown` by the time `melt_schedule_frame` sees it.
    """
    period_end = date(2026, 3, 31)
    with config_context(dataframe_backend="polars"):
        empty = build_frame(data={"UNINUM": [], "TOTASSETS": []}).with_columns(
            nw.lit(period_end).alias("period")
        )
        populated = build_frame(data={"UNINUM": [1], "BKVAL": [100]}).with_columns(
            nw.lit(period_end).alias("period")
        )
    result = to_wide_format(
        frames={"RCO": empty, "RC": populated},
        code_columns={"RCO": None, "RC": None},
        trailing_columns={"RCO": (), "RC": ()},
    )
    assert isinstance(result, nw.DataFrame)
    assert result.collect_schema()["RC__BKVAL"] == nw.Float64()


def test_to_wide_format_is_keyword_only() -> None:
    """to_wide_format takes no positional arguments."""
    rc = build_frame(data={"UNINUM": [1], "period": ["2026-03-31"], "A": [1]})
    with pytest.raises(TypeError):
        to_wide_format({"RC": rc}, {"RC": None}, {"RC": ()})  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# laziness (polars.LazyFrame input): nothing before pivot should collect
# ---------------------------------------------------------------------------


def _lazy_frame(*, data: dict[str, Any]) -> nw.LazyFrame[Any]:
    with config_context(dataframe_backend="polars"):
        frame = build_frame(data=data)
    result = frame.lazy()
    assert isinstance(result, nw.LazyFrame)
    return result


def test_melt_schedule_frame_preserves_laziness() -> None:
    """A LazyFrame input stays lazy through melt -- nothing forces a collect."""
    frame = _lazy_frame(
        data={
            "UNINUM": [1, 2],
            "period": ["2026-03-31", "2026-03-31"],
            "TOTASSETS": [100, 200],
        }
    )
    melted = melt_schedule_frame(frame=frame, schedule="RC", code_column=None)
    assert isinstance(melted, nw.LazyFrame)
    rows = melted.collect().rows(named=True)
    assert {row["variable_name"] for row in rows} == {"TOTASSETS"}


def test_melt_schedule_frame_with_trailing_columns_preserves_laziness() -> None:
    """The coded-vs-trailing split (its own internal concat) also stays lazy."""
    frame = _lazy_frame(
        data={
            "UNINUM": [1, 1],
            "period": ["2026-03-31"] * 2,
            "CAPCODE": [10, 20],
            "VAL1": [100, 150],
            "TOTAL": [999, 999],
        }
    )
    melted = melt_schedule_frame(
        frame=frame,
        schedule="RCR7",
        code_column="CAPCODE",
        trailing_columns=("TOTAL",),
    )
    assert isinstance(melted, nw.LazyFrame)
    rows = melted.collect().rows(named=True)
    assert any(row["variable_name"] == "TOTAL" for row in rows)


def test_with_column_key_preserves_laziness() -> None:
    """_with_column_key stays lazy given a LazyFrame input."""
    frame = _lazy_frame(
        data={
            "UNINUM": [1],
            "period": ["2026-03-31"],
            "schedule": ["RC"],
            "variable_name": ["TOTASSETS"],
            "value": [100.0],
        }
    )
    result = _with_column_key(frame)
    assert isinstance(result, nw.LazyFrame)
    assert result.collect()["column_key"].to_list() == ["RC__TOTASSETS"]


def test_to_wide_format_full_pipeline_stays_lazy_until_pivot() -> None:
    """Every step before the final pivot stays lazy; only pivot collects.

    Replicates `to_wide_format`'s own melt/concat/column-key steps
    manually to check each intermediate result's type, then calls
    `to_wide_format` itself to confirm its (necessarily eager, since
    pivot forces materialization) result is still correct.
    """
    rc = _lazy_frame(
        data={
            "UNINUM": [1, 2],
            "period": ["2026-03-31", "2026-03-31"],
            "TOTASSETS": [1000, 2000],
        }
    )
    rcb = _lazy_frame(
        data={
            "UNINUM": [1, 1],
            "period": ["2026-03-31", "2026-03-31"],
            "INV_CODE": [10, 20],
            "BKVAL": [100, 150],
        }
    )
    melted_rc = melt_schedule_frame(frame=rc, schedule="RC", code_column=None)
    melted_rcb = melt_schedule_frame(frame=rcb, schedule="RCB", code_column="INV_CODE")
    assert isinstance(melted_rc, nw.LazyFrame)
    assert isinstance(melted_rcb, nw.LazyFrame)

    combined = concat(frames=[melted_rc, melted_rcb], how="union")
    assert isinstance(combined, nw.LazyFrame)
    keyed = _with_column_key(combined)
    assert isinstance(keyed, nw.LazyFrame)

    result = to_wide_format(
        frames={"RC": rc, "RCB": rcb},
        code_columns={"RC": None, "RCB": "INV_CODE"},
        trailing_columns={"RC": (), "RCB": ()},
    )
    assert isinstance(result, nw.DataFrame)
    rows = {row["UNINUM"]: row for row in sorted_rows(result)}
    assert rows[1]["RC__TOTASSETS"] == 1000.0
    assert rows[1]["RCB__INV_CODE_10__BKVAL"] == 100.0
    assert rows[2]["RC__TOTASSETS"] == 2000.0


# ---------------------------------------------------------------------------
# _with_is_multiple_flag
# ---------------------------------------------------------------------------


def test_with_is_multiple_flag_true_for_a_coded_row() -> None:
    """A row with a real code gets is_multiple=True."""
    frame = build_frame(
        data={
            "schedule": ["RCB"],
            "code_column": ["INV_CODE"],
            "code_value": [15.0],
        }
    )
    result = _with_is_multiple_flag(frame)
    assert result["is_multiple"].to_list() == [True]


def test_with_is_multiple_flag_false_for_a_null_code_row() -> None:
    """A row with code_column present but null gets is_multiple=False."""
    coded = build_frame(
        data={"schedule": ["RCB"], "code_column": ["INV_CODE"], "code_value": [15]}
    )
    plain = build_frame(
        data={"schedule": ["RC"], "code_column": [None], "code_value": [None]}
    )
    combined = concat(frames=[coded, plain], how="union")
    result = _with_is_multiple_flag(combined)
    flags = dict(
        zip(result["schedule"].to_list(), result["is_multiple"].to_list(), strict=True)
    )
    assert flags == {"RCB": True, "RC": False}


def test_with_is_multiple_flag_adds_null_code_columns_when_entirely_absent() -> None:
    """No coded schedule at all: code_column/code_value are added, all null."""
    frame = build_frame(data={"schedule": ["RC"]})
    assert "code_column" not in frame.columns
    result = _with_is_multiple_flag(frame)
    assert isinstance(result, nw.DataFrame)
    assert set(result.columns) >= {"code_column", "code_value", "is_multiple"}
    assert result["is_multiple"].to_list() == [False]
    row = result.rows(named=True)[0]
    assert is_missing(row["code_column"])
    assert is_missing(row["code_value"])


# ---------------------------------------------------------------------------
# to_long_format
# ---------------------------------------------------------------------------


def test_to_long_format_combines_coded_and_plain_schedules() -> None:
    """to_long_format tags plain and coded schedules' rows correctly."""
    rc = build_frame(
        data={
            "UNINUM": [1, 2],
            "period": ["2026-03-31", "2026-03-31"],
            "TOTASSETS": [1000, 2000],
        }
    )
    rcb = build_frame(
        data={
            "UNINUM": [1, 1],
            "period": ["2026-03-31", "2026-03-31"],
            "INV_CODE": [10, 20],
            "BKVAL": [100, 150],
        }
    )
    result = to_long_format(
        frames={"RC": rc, "RCB": rcb},
        code_columns={"RC": None, "RCB": "INV_CODE"},
        trailing_columns={"RC": (), "RCB": ()},
    )
    assert isinstance(result, nw.DataFrame)
    rows = result.rows(named=True)
    rc_row = next(r for r in rows if r["schedule"] == "RC" and r["UNINUM"] == 1)
    assert rc_row["is_multiple"] is False
    assert is_missing(rc_row["code_column"])
    assert rc_row["value"] == 1000.0
    rcb_row = next(
        r for r in rows if r["schedule"] == "RCB" and r["code_value"] == 10.0
    )
    assert rcb_row["is_multiple"] is True
    assert rcb_row["code_column"] == "INV_CODE"
    assert rcb_row["value"] == 100.0


def test_to_long_format_trailing_column_is_single() -> None:
    """A single_multiple_single schedule's trailing field is tagged not-multiple."""
    rcr7 = build_frame(
        data={
            "UNINUM": [1, 1],
            "period": ["2026-03-31"] * 2,
            "CAPCODE": [10, 20],
            "VAL1": [100, 150],
            "TOTAL": [999, 999],
        }
    )
    result = to_long_format(
        frames={"RCR7": rcr7},
        code_columns={"RCR7": "CAPCODE"},
        trailing_columns={"RCR7": ("TOTAL",)},
    )
    rows = result.rows(named=True)
    total_row = next(r for r in rows if r["variable_name"] == "TOTAL")
    assert total_row["is_multiple"] is False
    assert is_missing(total_row["code_column"])
    assert total_row["value"] == 999.0
    val1_row = next(
        r for r in rows if r["variable_name"] == "VAL1" and r["code_value"] == 10.0
    )
    assert val1_row["is_multiple"] is True


def test_to_long_format_no_coded_schedule_still_has_code_columns() -> None:
    """Schedules with zero code columns still produce a complete long schema."""
    rc = build_frame(
        data={"UNINUM": [1], "period": ["2026-03-31"], "TOTASSETS": [1000]}
    )
    result = to_long_format(
        frames={"RC": rc}, code_columns={"RC": None}, trailing_columns={"RC": ()}
    )
    assert set(result.columns) == {
        "UNINUM",
        "period",
        "schedule",
        "code_column",
        "code_value",
        "is_multiple",
        "variable_name",
        "value",
    }
    assert result["is_multiple"].to_list() == [False]


def test_to_long_format_duplicate_grain_raises_reshape_error() -> None:
    """A genuinely duplicated (UNINUM, period, schedule, ..., variable) row raises."""
    rc = build_frame(
        data={
            "UNINUM": [1, 1],
            "period": ["2026-03-31", "2026-03-31"],
            "TOTASSETS": [1000, 9999],
        }
    )
    with pytest.raises(ReshapeError, match="not a unique grain"):
        to_long_format(
            frames={"RC": rc}, code_columns={"RC": None}, trailing_columns={"RC": ()}
        )


def test_to_long_format_empty_schedule_alongside_a_populated_one() -> None:
    """A zero-row schedule concatenated with a real one doesn't raise.

    Same regression as `test_to_wide_format_empty_schedule_alongside_a_populated_one`,
    for the long-format path.
    """
    period_end = date(2026, 3, 31)
    with config_context(dataframe_backend="polars"):
        empty = build_frame(data={"UNINUM": [], "TOTASSETS": []}).with_columns(
            nw.lit(period_end).alias("period")
        )
        populated = build_frame(data={"UNINUM": [1], "BKVAL": [100]}).with_columns(
            nw.lit(period_end).alias("period")
        )
    result = to_long_format(
        frames={"RCO": empty, "RC": populated},
        code_columns={"RCO": None, "RC": None},
        trailing_columns={"RCO": (), "RC": ()},
    )
    assert isinstance(result, nw.DataFrame)
    assert result.collect_schema()["value"] == nw.Float64()


def test_to_long_format_is_keyword_only() -> None:
    """to_long_format takes no positional arguments."""
    rc = build_frame(data={"UNINUM": [1], "period": ["2026-03-31"], "A": [1]})
    with pytest.raises(TypeError):
        to_long_format({"RC": rc}, {"RC": None}, {"RC": ()})  # type: ignore[call-arg]


def test_to_long_format_stays_lazy_until_grain_check() -> None:
    """Melt/concat/flag steps stay lazy; only assert_unique_grain collects."""
    rc = _lazy_frame(
        data={
            "UNINUM": [1, 2],
            "period": ["2026-03-31", "2026-03-31"],
            "TOTASSETS": [1000, 2000],
        }
    )
    rcb = _lazy_frame(
        data={
            "UNINUM": [1, 1],
            "period": ["2026-03-31", "2026-03-31"],
            "INV_CODE": [10, 20],
            "BKVAL": [100, 150],
        }
    )
    melted_rc = melt_schedule_frame(frame=rc, schedule="RC", code_column=None)
    melted_rcb = melt_schedule_frame(frame=rcb, schedule="RCB", code_column="INV_CODE")
    combined = concat(frames=[melted_rc, melted_rcb], how="union")
    assert isinstance(combined, nw.LazyFrame)
    flagged = _with_is_multiple_flag(combined)
    assert isinstance(flagged, nw.LazyFrame)

    result = to_long_format(
        frames={"RC": rc, "RCB": rcb},
        code_columns={"RC": None, "RCB": "INV_CODE"},
        trailing_columns={"RC": (), "RCB": ()},
    )
    assert isinstance(result, nw.DataFrame)
    # 2 RC rows (one TOTASSETS per UNINUM) + 2 RCB rows (one BKVAL per code).
    assert result.shape[0] == 4


# ---------------------------------------------------------------------------
# _parse_wide_column_key
# ---------------------------------------------------------------------------


def test_parse_wide_column_key_plain() -> None:
    """A plain column name parses to a single-variable tuple."""
    assert _parse_wide_column_key("RC__TOTASSETS") == (
        "RC",
        None,
        None,
        False,
        "TOTASSETS",
    )


def test_parse_wide_column_key_coded_with_underscore_in_code_column() -> None:
    """A code column name containing its own underscore still parses correctly."""
    assert _parse_wide_column_key("RCB__INV_CODE_15__BKVAL") == (
        "RCB",
        "INV_CODE",
        15.0,
        True,
        "BKVAL",
    )


@pytest.mark.parametrize(
    "column_key",
    [
        "NODUNDERSCOREATALL",
        "RC__CODE_abc__VAR",
        "RC__A__B__C",
    ],
)
def test_parse_wide_column_key_rejects_malformed_names(column_key: str) -> None:
    """A column name matching neither the plain nor coded pattern raises."""
    with pytest.raises(ReshapeError):
        _parse_wide_column_key(column_key)


# ---------------------------------------------------------------------------
# convert_wide_format_to_long_format / convert_long_format_to_wide_format
# ---------------------------------------------------------------------------


def test_convert_wide_format_to_long_format_basic() -> None:
    """A wide frame converts to the same information in long form."""
    wide = build_frame(
        data={
            "UNINUM": [1, 2],
            "period": ["2026-03-31", "2026-03-31"],
            "RC__TOTASSETS": [1000.0, 2000.0],
            "RCB__INV_CODE_15__BKVAL": [9.0, 8.0],
        }
    )
    result = convert_wide_format_to_long_format(wide=wide.to_native())
    frame = nw.from_native(result)
    assert isinstance(frame, nw.DataFrame)
    rows = frame.rows(named=True)
    assert len(rows) == 4
    rc_row = next(
        r for r in rows if r["UNINUM"] == 1 and r["variable_name"] == "TOTASSETS"
    )
    assert rc_row["schedule"] == "RC"
    assert rc_row["is_multiple"] is False
    assert rc_row["value"] == 1000.0
    rcb_row = next(
        r for r in rows if r["UNINUM"] == 1 and r["variable_name"] == "BKVAL"
    )
    assert rcb_row["schedule"] == "RCB"
    assert rcb_row["code_column"] == "INV_CODE"
    assert rcb_row["code_value"] == 15.0
    assert rcb_row["is_multiple"] is True
    assert rcb_row["value"] == 9.0


def test_convert_wide_format_to_long_format_rejects_a_malformed_column_name() -> None:
    """A wide column not matching the naming convention raises ReshapeError."""
    wide = build_frame(
        data={"UNINUM": [1], "period": ["2026-03-31"], "NOTVALIDATALL": [1.0]}
    )
    with pytest.raises(ReshapeError):
        convert_wide_format_to_long_format(wide=wide.to_native())


def test_convert_wide_format_to_long_format_is_keyword_only() -> None:
    """convert_wide_format_to_long_format takes no positional arguments."""
    wide = build_frame(data={"UNINUM": [1], "period": ["2026-03-31"]})
    with pytest.raises(TypeError):
        convert_wide_format_to_long_format(  # type: ignore[call-overload]
            wide.to_native()
        )


def test_convert_wide_format_to_long_format_stays_lazy() -> None:
    """A LazyFrame input produces a genuinely uncollected LazyFrame output."""
    wide_native = _lazy_frame(
        data={
            "UNINUM": [1],
            "period": ["2026-03-31"],
            "RC__TOTASSETS": [1000.0],
        }
    ).to_native()
    assert isinstance(wide_native, pl.LazyFrame)
    result = convert_wide_format_to_long_format(wide=wide_native)
    assert isinstance(result, pl.LazyFrame)
    assert result.collect().to_dicts()[0]["value"] == 1000.0


def test_convert_wide_format_to_long_format_honors_dataframe_type() -> None:
    """dataframe_type converts the result as a final step."""
    wide = build_frame(
        data={"UNINUM": [1], "period": ["2026-03-31"], "RC__TOTASSETS": [1000.0]}
    )
    result = convert_wide_format_to_long_format(
        wide=wide.to_native(), dataframe_type="pyarrow_table"
    )
    assert isinstance(result, pa.Table)


def test_convert_long_format_to_wide_format_basic() -> None:
    """A long frame converts to the same information in wide form."""
    long_ = build_frame(
        data={
            "UNINUM": [1, 1],
            "period": ["2026-03-31", "2026-03-31"],
            "schedule": ["RC", "RCB"],
            "code_column": [None, "INV_CODE"],
            "code_value": [None, 15.0],
            "is_multiple": [False, True],
            "variable_name": ["TOTASSETS", "BKVAL"],
            "value": [1000.0, 9.0],
        }
    )
    result = convert_long_format_to_wide_format(long=long_.to_native())
    frame = nw.from_native(result)
    assert isinstance(frame, nw.DataFrame)
    row = frame.rows(named=True)[0]
    assert row["RC__TOTASSETS"] == 1000.0
    assert row["RCB__INV_CODE_15__BKVAL"] == 9.0


def test_convert_long_format_to_wide_format_duplicate_grain_raises() -> None:
    """A duplicated (UNINUM, period, column_key) grain raises via pivot."""
    long_ = build_frame(
        data={
            "UNINUM": [1, 1],
            "period": ["2026-03-31", "2026-03-31"],
            "schedule": ["RC", "RC"],
            "code_column": [None, None],
            "code_value": [None, None],
            "is_multiple": [False, False],
            "variable_name": ["TOTASSETS", "TOTASSETS"],
            "value": [1000.0, 9999.0],
        }
    )
    with pytest.raises(ReshapeError):
        convert_long_format_to_wide_format(long=long_.to_native())


def test_convert_long_format_to_wide_format_is_keyword_only() -> None:
    """convert_long_format_to_wide_format takes no positional arguments."""
    long_ = build_frame(
        data={
            "UNINUM": [1],
            "period": ["2026-03-31"],
            "schedule": ["RC"],
            "code_column": [None],
            "code_value": [None],
            "is_multiple": [False],
            "variable_name": ["TOTASSETS"],
            "value": [1000.0],
        }
    )
    with pytest.raises(TypeError):
        convert_long_format_to_wide_format(  # type: ignore[call-overload]
            long_.to_native()
        )


# ---------------------------------------------------------------------------
# round-trip equivalence (hermetic)
# ---------------------------------------------------------------------------


def test_wide_to_long_to_wide_round_trip_matches_original() -> None:
    """Wide -> long -> wide reproduces the original wide frame exactly.

    No gaps in this fixture (every UNINUM has every code), so pivot's
    grid-completion introduces no extra rows either way -- an exact match
    is expected here (see the real-archive test for the with-gaps case).
    """
    wide = build_frame(
        data={
            "UNINUM": [1, 2],
            "period": ["2026-03-31", "2026-03-31"],
            "RC__TOTASSETS": [1000.0, 2000.0],
            "RCB__INV_CODE_15__BKVAL": [9.0, 8.0],
        }
    ).to_native()
    long_ = convert_wide_format_to_long_format(wide=wide)
    round_tripped = nw.from_native(convert_long_format_to_wide_format(long=long_))
    original = nw.from_native(wide)
    assert isinstance(round_tripped, nw.DataFrame)
    assert isinstance(original, nw.DataFrame)
    cols = sorted(original.columns)
    round_tripped_rows = round_tripped.select(cols).sort(cols).rows(named=True)
    original_rows = original.select(cols).sort(cols).rows(named=True)
    assert round_tripped_rows == original_rows


def test_long_to_wide_to_long_round_trip_matches_non_nullsorted_rows() -> None:
    """Long -> wide -> long reproduces every non-null-value row exactly.

    This fixture has a genuine gap (UNINUM 2 has no INV_CODE=20 row at
    all), so the round-tripped long frame has one extra, structurally
    null row that the original never had -- documented behavior, not a
    bug (see convert_wide_format_to_long_format's docstring).
    """
    original = build_frame(
        data={
            "UNINUM": [1, 1, 2],
            "period": ["2026-03-31"] * 3,
            "schedule": ["RCB", "RCB", "RCB"],
            "code_column": ["INV_CODE"] * 3,
            "code_value": [10.0, 20.0, 10.0],
            "is_multiple": [True, True, True],
            "variable_name": ["BKVAL"] * 3,
            "value": [100.0, 150.0, 300.0],
        }
    ).to_native()
    wide = convert_long_format_to_wide_format(long=original)
    round_tripped = nw.from_native(convert_wide_format_to_long_format(wide=wide))
    original_frame = nw.from_native(original)
    assert isinstance(round_tripped, nw.DataFrame)
    assert isinstance(original_frame, nw.DataFrame)
    round_tripped_non_null = round_tripped.filter(~nw.col("value").is_null())

    cols = sorted(original_frame.columns)
    non_null_rows = round_tripped_non_null.select(cols).sort(cols).rows(named=True)
    original_rows = original_frame.select(cols).sort(cols).rows(named=True)
    assert non_null_rows == original_rows
    assert round_tripped.shape[0] == original_frame.shape[0] + 1


# ---------------------------------------------------------------------------
# Canonical long-format column order (issue #46)
# ---------------------------------------------------------------------------


def _reshape_inputs(*, coded_first: bool = False, coded: bool = True) -> dict[str, Any]:
    """Build one plain and (optionally) one coded schedule's melt inputs.

    `coded_first` controls which schedule the reshape sees first, and
    `coded` whether a coded schedule is present at all. Both change the
    column order the concat would otherwise produce, so a test can pin the
    canonical order against every shape that used to diverge.
    """
    rc = build_frame(
        data={
            "UNINUM": [1, 2],
            "period": ["2026-03-31", "2026-03-31"],
            "TOTASSETS": [1000, 2000],
        }
    )
    if not coded:
        return {
            "frames": {"RC": rc},
            "code_columns": {"RC": None},
            "trailing_columns": {"RC": ()},
        }
    rcb = build_frame(
        data={
            "UNINUM": [1, 1],
            "period": ["2026-03-31", "2026-03-31"],
            "INV_CODE": [10, 20],
            "BKVAL": [100.5, 150.5],
        }
    )
    frames = {"RCB": rcb, "RC": rc} if coded_first else {"RC": rc, "RCB": rcb}
    return {
        "frames": frames,
        "code_columns": {"RC": None, "RCB": "INV_CODE"},
        "trailing_columns": {"RC": (), "RCB": ()},
    }


@pytest.mark.parametrize(
    ("coded_first", "coded"),
    [(False, True), (True, True), (False, False)],
    ids=["plain-first", "coded-first", "no-coded-schedule"],
)
def test_to_long_format_column_order_is_canonical(
    backend: str, coded_first: bool, coded: bool
) -> None:
    """to_long_format returns LONG_FORMAT_COLUMNS whatever its input looks like.

    Without the final select, the order came from a diagonal concat of the
    melted pieces, so it depended on whether a coded schedule was melted
    first, last, or not at all. Three inputs that produced three different
    orders must now produce one.
    """
    result = to_long_format(**_reshape_inputs(coded_first=coded_first, coded=coded))
    assert tuple(result.columns) == LONG_FORMAT_COLUMNS


@pytest.mark.parametrize(
    ("coded_first", "coded"),
    [(False, True), (True, True), (False, False)],
    ids=["plain-first", "coded-first", "no-coded-schedule"],
)
def test_both_long_format_routes_agree_on_columns_and_dtypes(
    backend: str, coded_first: bool, coded: bool
) -> None:
    """The two public routes to a long frame return one layout and one schema.

    `to_long_format` and `convert_wide_format_to_long_format` returned the
    same eight columns in different orders, so a positional read of one
    did not match the other. Their dtypes diverged too when no schedule
    was coded, because the wide-to-long lookup inferred rather than
    declared them.
    """
    inputs = _reshape_inputs(coded_first=coded_first, coded=coded)
    direct = to_long_format(**inputs)
    wide = to_wide_format(**inputs)
    converted = nw.from_native(
        convert_wide_format_to_long_format(wide=wide.to_native())
    )

    assert tuple(direct.columns) == LONG_FORMAT_COLUMNS
    assert tuple(converted.columns) == LONG_FORMAT_COLUMNS
    assert dict(converted.collect_schema()) == dict(direct.collect_schema())


def test_convert_wide_format_to_long_format_without_a_coded_schedule(
    backend: str,
) -> None:
    """A wide frame with no coded column converts without inferring its lookup.

    The lookup frame's `code_column` and `code_value` are entirely null in
    this case. Left to inference, pandas read `code_value` as String and
    polars as Unknown, while pyarrow built a null-typed column that raised
    ``pyarrow.lib.ArrowInvalid`` as soon as the lookup was joined.
    """
    wide = to_wide_format(**_reshape_inputs(coded=False))
    result = nw.from_native(convert_wide_format_to_long_format(wide=wide.to_native()))
    schema = result.collect_schema()
    assert schema["code_column"] == nw.String
    assert schema["code_value"] == nw.Float64
    assert schema["is_multiple"] == nw.Boolean
    assert result["is_multiple"].to_list() == [False, False]


def test_long_format_column_order_holds_when_lazy(lazy_polars_backend: str) -> None:
    """The canonical select stays lazy-safe on a polars LazyFrame source."""
    inputs = _reshape_inputs()
    lazy_inputs = {
        **inputs,
        "frames": {name: frame.lazy() for name, frame in inputs["frames"].items()},
    }
    result = to_long_format(**lazy_inputs)
    assert tuple(result.columns) == LONG_FORMAT_COLUMNS


# ---------------------------------------------------------------------------
# Reshaped dtypes (issue #44)
# ---------------------------------------------------------------------------


def test_reshaped_value_columns_are_float64(backend: str) -> None:
    """Neither long nor wide output carries an untyped measure column.

    Under pandas these used to land in Object columns, which support no
    numeric aggregation and cost far more memory than a float column of
    the same length. A single wide frame has one such column per variable,
    so the whole frame was effectively untyped.
    """
    inputs = _reshape_inputs()
    long_schema = to_long_format(**inputs).collect_schema()
    wide_schema = to_wide_format(**inputs).collect_schema()

    assert long_schema["value"] == nw.Float64
    value_columns = [name for name in wide_schema.names() if "__" in name]
    assert value_columns
    assert all(wide_schema[name] == nw.Float64 for name in value_columns)


# ---------------------------------------------------------------------------
# Code grain (issue #62)
# ---------------------------------------------------------------------------


def _rcr7_frame() -> Any:
    """Build a single_multiple_single frame: two codes plus one trailing total."""
    return build_frame(
        data={
            "UNINUM": [1, 1, 2],
            "period": ["2026-03-31"] * 3,
            "RegCapCode": [10, 20, 10],
            "CreditEquil": [100.0, 150.0, 300.0],
            "AvgDailyRWARegCap": [999.0, 999.0, 888.0],
        }
    )


def test_with_plain_column_key_ignores_the_code() -> None:
    """A coded row still keys as `{schedule}__{variable}` in the code grain.

    The code stays a row key there, so it must not reach the column name
    the way `_with_column_key` puts it there for the wide format.
    """
    frame = build_frame(
        data={
            "UNINUM": [1],
            "period": ["2026-03-31"],
            "schedule": ["RCB"],
            "variable_name": ["BKVAL"],
            "value": [100.0],
            "code_column": ["INV_CODE"],
            "code_value": [15],
        }
    )
    assert _with_plain_column_key(frame)["column_key"].to_list() == ["RCB__BKVAL"]


def test_to_code_grain_format_keys_rows_by_code(backend: str) -> None:
    """The code becomes a row key and the variable becomes a column."""
    result = to_code_grain_format(**_reshape_inputs())
    assert tuple(result.columns) == (*CODE_GRAIN_INDEX, "RCB__BKVAL")
    rows = sorted_rows(result, by=["UNINUM", "code_value"])
    assert [row["code_value"] for row in rows] == [10.0, 20.0]
    assert [row["code_column"] for row in rows] == ["INV_CODE", "INV_CODE"]
    assert [row["RCB__BKVAL"] for row in rows] == [100.5, 150.5]


def test_to_code_grain_format_drops_a_schedule_with_no_code(backend: str) -> None:
    """A "single"-scenario schedule contributes no row and no column.

    RC reports no code, so it has no code grain. Its rows melt with a null
    `code_column`, and the one filter that removes them is what keeps the
    grain honest: without it every institution would gain a null-coded row.
    """
    result = to_code_grain_format(**_reshape_inputs())
    assert "RC__TOTASSETS" not in result.columns
    assert result.shape[0] == 2
    assert all(row["code_column"] == "INV_CODE" for row in sorted_rows(result))


def test_to_code_grain_format_stacks_different_code_columns(backend: str) -> None:
    """Two schedules with different code columns stack rather than join.

    `code_column` is part of the grain, so RCB's INV_CODE rows and RCF's
    LOANSTATUS rows coexist, each populating only its own schedule's
    columns. The result is deliberately sparse, not a join.
    """
    rcb = build_frame(
        data={
            "UNINUM": [1],
            "period": ["2026-03-31"],
            "INV_CODE": [10],
            "BKVAL": [100.0],
        }
    )
    rcf = build_frame(
        data={
            "UNINUM": [1],
            "period": ["2026-03-31"],
            "LOANSTATUS": [54],
            "TOTPDUE": [7.0],
        }
    )
    result = to_code_grain_format(
        frames={"RCB": rcb, "RCF": rcf},
        code_columns={"RCB": "INV_CODE", "RCF": "LOANSTATUS"},
        trailing_columns={"RCB": (), "RCF": ()},
    )
    rows = {row["code_column"]: row for row in sorted_rows(result)}
    assert set(rows) == {"INV_CODE", "LOANSTATUS"}
    assert rows["INV_CODE"]["RCB__BKVAL"] == 100.0
    assert is_missing(rows["INV_CODE"]["RCF__TOTPDUE"])
    assert rows["LOANSTATUS"]["RCF__TOTPDUE"] == 7.0
    assert is_missing(rows["LOANSTATUS"]["RCB__BKVAL"])


def test_to_code_grain_format_drops_trailing_columns(backend: str) -> None:
    """A single_multiple_single schedule's trailing fields contribute no column.

    RCR7's ``AvgDailyRWARegCap`` is a single-occurrence field the loader
    repeats on every code-row, so it is institution-level and has no code
    grain, exactly like a "single"-scenario schedule's fields.
    """
    result = to_code_grain_format(
        frames={"RCR7": _rcr7_frame()},
        code_columns={"RCR7": "RegCapCode"},
        trailing_columns={"RCR7": ("AvgDailyRWARegCap",)},
    )
    assert tuple(result.columns) == (*CODE_GRAIN_INDEX, "RCR7__CreditEquil")
    rows = sorted_rows(result, by=["UNINUM", "code_value"])
    assert [(row["UNINUM"], row["code_value"]) for row in rows] == [
        (1, 10.0),
        (1, 20.0),
        (2, 10.0),
    ]


def test_to_code_grain_format_without_a_coded_schedule_raises(backend: str) -> None:
    """A selection of only non-coded schedules has no grain to build at all.

    `code_column` is absent from the melted schema entirely rather than
    merely null, so this is caught before the filter rather than silently
    producing an empty frame.
    """
    with pytest.raises(ReshapeError, match="reports a code"):
        to_code_grain_format(**_reshape_inputs(coded=False))


def test_to_code_grain_format_without_coded_rows_raises(backend: str) -> None:
    """A coded schedule with zero rows raises rather than returning bare keys.

    The schedule declares a code column, so the "reports a code" guard
    passes, but the filter to coded rows leaves nothing and the pivot
    returns just the four index columns. Without this check that silently
    useless frame is returned, the asymmetry issue #84 reported between
    this entry point and `convert_long_format_to_code_grain_format`.
    """
    empty = build_frame(
        data={
            "UNINUM": [],
            "period": [],
            "INV_CODE": [],
            "BKVAL": [],
        }
    )
    with pytest.raises(ReshapeError, match="no coded measurement columns"):
        to_code_grain_format(
            frames={"RCB": empty},
            code_columns={"RCB": "INV_CODE"},
            trailing_columns={"RCB": ()},
        )


def test_to_code_grain_format_duplicate_grain_raises(backend: str) -> None:
    """A duplicated (UNINUM, period, code, variable) row raises, never aggregates."""
    rcb = build_frame(
        data={
            "UNINUM": [1, 1],
            "period": ["2026-03-31", "2026-03-31"],
            "INV_CODE": [10, 10],
            "BKVAL": [100.0, 9999.0],
        }
    )
    with pytest.raises(ReshapeError):
        to_code_grain_format(
            frames={"RCB": rcb},
            code_columns={"RCB": "INV_CODE"},
            trailing_columns={"RCB": ()},
        )


def test_to_code_grain_format_stays_lazy_until_the_pivot(
    lazy_polars_backend: str,
) -> None:
    """A LazyFrame source reaches the pivot uncollected and pivots correctly."""
    inputs = _reshape_inputs()
    lazy_inputs = {
        **inputs,
        "frames": {name: frame.lazy() for name, frame in inputs["frames"].items()},
    }
    result = to_code_grain_format(**lazy_inputs)
    assert isinstance(result, nw.DataFrame)
    assert tuple(result.columns) == (*CODE_GRAIN_INDEX, "RCB__BKVAL")


def test_code_grain_value_columns_are_float64(backend: str) -> None:
    """Every measure column is Float64, matching the other two architectures."""
    schema = to_code_grain_format(**_reshape_inputs()).collect_schema()
    assert schema["code_value"] == nw.Float64
    assert schema["RCB__BKVAL"] == nw.Float64


def test_both_code_grain_routes_agree(backend: str) -> None:
    """Building the code grain directly matches converting a long frame to it.

    The two routes share the pivot but not the path to it: one melts the
    schedules, the other reads `code_column`/`code_value` back off an
    already-built long frame.
    """
    inputs = _reshape_inputs()
    direct = to_code_grain_format(**inputs)
    long_ = to_long_format(**inputs)
    converted = nw.from_native(
        convert_long_format_to_code_grain_format(long=long_.to_native())
    )
    assert tuple(converted.columns) == tuple(direct.columns)
    assert sorted_rows(converted, by=["UNINUM", "code_value"]) == sorted_rows(
        direct, by=["UNINUM", "code_value"]
    )


def test_convert_long_format_to_code_grain_format_without_coded_rows_raises(
    backend: str,
) -> None:
    """A long frame of only single-occurrence rows raises rather than returning keys.

    Filtering to coded rows leaves nothing, and every backend's pivot then
    returns a frame of just the four index columns. Returning that would
    be a silently useless result.
    """
    long_ = to_long_format(**_reshape_inputs(coded=False))
    with pytest.raises(ReshapeError, match="no multiple-occurrence rows"):
        convert_long_format_to_code_grain_format(long=long_.to_native())


def test_convert_long_format_to_code_grain_format_honors_dataframe_type() -> None:
    """dataframe_type converts the result as a final step."""
    long_ = to_long_format(**_reshape_inputs())
    result = convert_long_format_to_code_grain_format(
        long=long_.to_native(), dataframe_type="pyarrow_table"
    )
    assert isinstance(result, pa.Table)


def test_convert_long_format_to_code_grain_format_is_keyword_only() -> None:
    """convert_long_format_to_code_grain_format takes no positional arguments."""
    long_ = to_long_format(**_reshape_inputs())
    with pytest.raises(TypeError):
        convert_long_format_to_code_grain_format(  # type: ignore[call-overload]
            long_.to_native()
        )


# ---------------------------------------------------------------------------
# Curated domain datasets (issue #63)
# ---------------------------------------------------------------------------


def _coded_source() -> DomainDatasetSource:
    """Build a source whose schedule reports its own code column."""
    return DomainDatasetSource(
        schedules=("RCF1",),
        code_column="LOANSTATUS",
        columns={
            "ACCR": DomainDatasetColumn(column="accruing", code=None),
            "NONCSH": DomainDatasetColumn(column="nonaccrual_cash", code=None),
        },
    )


def _name_encoded_source() -> DomainDatasetSource:
    """Build a source whose breakdown is encoded in its variable names."""
    return DomainDatasetSource(
        schedules=("RIE", "RIE2"),
        code_column=None,
        columns={
            "CHGOFFAGBUS": DomainDatasetColumn(column="charge_off", code=110),
            "RECAGBUS": DomainDatasetColumn(column="recovery", code=110),
            "CHGOFFDLN": DomainDatasetColumn(column="net_charge_off", code=145),
        },
    )


def _dataset(*sources: DomainDatasetSource) -> DomainDataset:
    """Wrap source groups in the smallest dataset the decoding step needs."""
    return DomainDataset(
        name="example",
        code_column="LOAN_PORTFOLIO",
        codes=(),
        sources=sources,
        derived=(),
    )


def _melted_coded() -> Any:
    """Melt a small RCF1-shaped frame, including a variable no dataset declares."""
    frame = build_frame(
        data={
            "UNINUM": [1, 1],
            "period": ["2026-03-31", "2026-03-31"],
            "LOANSTATUS": [110, 110],
            "ACCR": [100.0, 100.0],
            "UNDECLARED": [9.0, 9.0],
        }
    ).unique(subset=["UNINUM", "period", "LOANSTATUS"])
    return melt_schedule_frame(frame=frame, schedule="RCF1", code_column="LOANSTATUS")


def _melted_name_encoded() -> Any:
    """Melt a small RIE-shaped frame, which has no code column at all."""
    frame = build_frame(
        data={
            "UNINUM": [1],
            "period": ["2026-03-31"],
            "CHGOFFAGBUS": [83.0],
            "RECAGBUS": [49.0],
            "UNDECLARED": [9.0],
        }
    )
    return melt_schedule_frame(frame=frame, schedule="RIE", code_column=None)


def test_decoding_a_coded_source_keeps_its_own_code(backend: str) -> None:
    """A code-bearing source keeps code_value and gets the curated code_column.

    Its code already came from the data during the melt, so decoding only
    has to rename the variable and relabel which vocabulary the code is
    expressed in.
    """
    decoded = apply_domain_dataset_decoding(
        frame=_melted_coded(), dataset=_dataset(_coded_source())
    )
    rows = sorted_rows(decoded, by=["variable_name"])
    assert [row["variable_name"] for row in rows] == ["accruing"]
    assert rows[0]["code_column"] == "LOAN_PORTFOLIO"
    assert rows[0]["code_value"] == 110.0
    assert rows[0]["value"] == 100.0


def test_decoding_a_name_encoded_source_supplies_the_code(backend: str) -> None:
    """A source with no code column gets both code_column and code_value.

    RI-E reports one column per portfolio rather than one row per code, so
    the code exists only in the dataset's declaration until this step
    puts it in the data.
    """
    decoded = apply_domain_dataset_decoding(
        frame=_melted_name_encoded(), dataset=_dataset(_name_encoded_source())
    )
    rows = sorted_rows(decoded, by=["variable_name"])
    assert [(row["variable_name"], row["code_value"]) for row in rows] == [
        ("charge_off", 110.0),
        ("recovery", 110.0),
    ]
    assert {row["code_column"] for row in rows} == {"LOAN_PORTFOLIO"}


def test_decoding_a_name_encoded_source_alone_supplies_code_value(
    backend: str,
) -> None:
    """A dataset of only name-encoded sources has no melted code_value column.

    Only a code-bearing schedule melts with a `code_value` column, so a
    frame stacked from name-encoded schedules alone reaches the join
    without one. The coalesce that resolves the code needs the column to
    exist, so decoding supplies it.
    """
    frame = _melted_name_encoded()
    assert "code_value" not in frame.collect_schema().names()
    decoded = apply_domain_dataset_decoding(
        frame=frame, dataset=_dataset(_name_encoded_source())
    )
    assert [
        row["code_value"] for row in sorted_rows(decoded, by=["variable_name"])
    ] == [
        110.0,
        110.0,
    ]


def test_decoding_keys_the_lookup_by_schedule_as_well_as_variable(
    backend: str,
) -> None:
    """Two sources reusing one variable name each get their own output column.

    Matching on the variable name alone would let either source's
    declaration rewrite the other's rows, since one lookup now covers
    every contributing schedule at once.
    """
    first = build_frame(
        data={
            "UNINUM": [1],
            "period": ["2026-03-31"],
            "AMOUNT": [10.0],
        }
    )
    second = build_frame(
        data={
            "UNINUM": [1],
            "period": ["2026-03-31"],
            "AMOUNT": [20.0],
        }
    )
    stacked = concat(
        frames=[
            melt_schedule_frame(frame=first, schedule="RIE", code_column=None),
            melt_schedule_frame(frame=second, schedule="RIE2", code_column=None),
        ],
        how="union",
    )
    dataset = _dataset(
        DomainDatasetSource(
            schedules=("RIE",),
            code_column=None,
            columns={"AMOUNT": DomainDatasetColumn(column="charge_off", code=110)},
        ),
        DomainDatasetSource(
            schedules=("RIE2",),
            code_column=None,
            columns={"AMOUNT": DomainDatasetColumn(column="recovery", code=145)},
        ),
    )
    decoded = apply_domain_dataset_decoding(frame=stacked, dataset=dataset)
    rows = sorted_rows(decoded, by=["variable_name"])
    assert [
        (row["variable_name"], row["code_value"], row["value"]) for row in rows
    ] == [("charge_off", 110.0, 10.0), ("recovery", 145.0, 20.0)]


@pytest.mark.parametrize(
    ("melted", "source"),
    [(_melted_coded, _coded_source), (_melted_name_encoded, _name_encoded_source)],
    ids=["coded", "name-encoded"],
)
def test_decoding_drops_undeclared_variables(
    backend: str, melted: Any, source: Any
) -> None:
    """A variable the dataset does not declare contributes no row.

    A curated dataset names the columns it wants. Carrying the rest
    through would put the source's own vocabulary back into a frame whose
    whole purpose is to be free of it.
    """
    decoded = apply_domain_dataset_decoding(frame=melted(), dataset=_dataset(source()))
    assert "UNDECLARED" not in {
        row["variable_name"] for row in sorted_rows(decoded, by=["variable_name"])
    }


def _pivoted(data: dict[str, list[Any]]) -> nw.DataFrame[Any]:
    """Build a frame shaped like the pivot's output, for derived-column tests.

    Every frame here carries at least one fully populated row, so each
    column infers as Float64 the way the real pivot's output does. A
    column null in every row instead infers as null-typed on pyarrow,
    which no real pivot produces and which raises on `fill_null`. Dtypes
    are inferred rather than declared for the same reason: a declared
    schema reaches pandas' nullable dtypes, while the real path is
    numpy-backed.
    """
    frame = build_frame(data=data)
    assert isinstance(frame, nw.DataFrame)
    return frame


def test_derived_sum_treats_nulls_as_zero_when_any_component_is_present(
    backend: str,
) -> None:
    """A partially reported row sums what is there rather than going null."""
    frame = _pivoted({"a": [1.0, 4.0], "b": [None, 5.0], "c": [2.0, 6.0]})
    result = add_derived_columns(
        frame=frame,
        derived=[
            DomainDatasetDerived(
                column="total", operation="sum", components=("a", "b", "c")
            )
        ],
    )
    assert result["total"].to_list() == [3.0, 15.0]


def test_derived_sum_of_an_all_null_row_stays_null(backend: str) -> None:
    """Every component null gives null, never a manufactured zero.

    A row can exist because one schedule reported it while the schedule
    supplying these components reported nothing. Zero would read as a
    measured zero, which is a different claim from "not reported".
    """
    frame = _pivoted({"a": [1.0, None], "b": [2.0, None]})
    result = add_derived_columns(
        frame=frame,
        derived=[
            DomainDatasetDerived(column="total", operation="sum", components=("a", "b"))
        ],
    )
    totals = result["total"].to_list()
    assert totals[0] == 3.0
    assert is_missing(totals[1])


def test_derived_difference(backend: str) -> None:
    """A difference subtracts every later component from the first."""
    frame = _pivoted({"gross": [83.0, 10.0], "back": [49.0, 4.0]})
    result = add_derived_columns(
        frame=frame,
        derived=[
            DomainDatasetDerived(
                column="net", operation="difference", components=("gross", "back")
            )
        ],
    )
    assert result["net"].to_list() == [34.0, 6.0]


def test_derived_never_overwrites_a_reported_value(backend: str) -> None:
    """Where the source reports the column itself, the reported value wins.

    RI-E reports net charge-offs directly for two portfolios and leaves
    the rest to be computed, so one column carries both. Recomputing over
    the reported rows would replace a real figure with one derived from
    the nulls beside it.
    """
    frame = _pivoted({"gross": [83.0, None], "back": [49.0, None], "net": [None, 7.0]})
    result = add_derived_columns(
        frame=frame,
        derived=[
            DomainDatasetDerived(
                column="net", operation="difference", components=("gross", "back")
            )
        ],
    )
    assert result["net"].to_list() == [34.0, 7.0]


def test_derived_is_skipped_when_a_component_column_is_absent(backend: str) -> None:
    """A declaration whose components are not all present is skipped, not raised.

    A range covering only some of a dataset's schedules pivots to a frame
    missing whole groups of columns. That is a normal narrow request, not
    an error, so the derived column is simply absent too.
    """
    frame = _pivoted({"a": [1.0, 2.0]})
    result = add_derived_columns(
        frame=frame,
        derived=[
            DomainDatasetDerived(
                column="total", operation="sum", components=("a", "missing")
            )
        ],
    )
    assert "total" not in result.columns


# ---------------------------------------------------------------------------
# exclude_reported_totals and assert_pivot_has_measurements
#
# These back FCACallReport._to_domain_dataset's orchestration (issue #63,
# part 2), which builds a curated domain dataset by calling
# melt_schedule_frame, apply_domain_dataset_decoding, concat, pivot, and
# these two directly, rather than through a free function in this module.
# ---------------------------------------------------------------------------


def test_exclude_reported_totals_drops_a_matching_code(backend: str) -> None:
    """A code_value in total_codes is dropped."""
    frame = build_frame(data={"code_value": [100.0, 155.0], "value": [1.0, 2.0]})
    result = exclude_reported_totals(frame=frame, total_codes=frozenset({155}))
    rows = rows_of(result)
    assert [row["code_value"] for row in rows] == [100.0]


def test_exclude_reported_totals_keeps_a_null_code_value(backend: str) -> None:
    """A null code_value is never treated as a reported total, on every backend.

    `is_in` is itself null when tested against a null value, and that
    null is backend-inconsistent under `filter` (pandas keeps the row,
    polars and pyarrow drop it). A null code_value is definitionally not
    one of the source's own declared total codes, so it must always
    survive, the same on every backend.
    """
    frame = build_frame(data={"code_value": [110, None], "value": [100.0, 50.0]})
    result = exclude_reported_totals(frame=frame, total_codes=frozenset({155}))
    rows = sorted_rows(result, by=["value"])
    assert len(rows) == 2
    assert is_missing(rows[0]["code_value"]) or is_missing(rows[1]["code_value"])


def test_assert_pivot_has_measurements_raises_on_index_only_frame(
    backend: str,
) -> None:
    """A pivot that produced no measurement columns raises, not an empty frame.

    A pivot on an entirely empty input still produces the four
    CODE_GRAIN_INDEX columns and nothing else. Returning that silently
    would look like a real, if narrow, result rather than the absence of
    any real one, matching `convert_long_format_to_code_grain_format`'s
    equivalent guard.
    """
    pivoted = build_frame(data={column: [] for column in CODE_GRAIN_INDEX})
    with pytest.raises(ReshapeError, match="no measurement columns"):
        assert_pivot_has_measurements(
            pivoted=pivoted, message=NO_DOMAIN_DATASET_MEASUREMENTS
        )


def test_assert_pivot_has_measurements_passes_with_a_measurement_column(
    backend: str,
) -> None:
    """A pivot with at least one measurement column beyond the grain passes."""
    pivoted = build_frame(
        data={**{column: [] for column in CODE_GRAIN_INDEX}, "accruing": []}
    )
    assert_pivot_has_measurements(
        pivoted=pivoted, message=NO_DOMAIN_DATASET_MEASUREMENTS
    )


# ---------------------------------------------------------------------------
# pivot_domain_dataset_wide
# ---------------------------------------------------------------------------


def test_pivot_domain_dataset_wide_keys_one_row_per_institution_and_period(
    backend: str,
) -> None:
    """Every code's measures land in their own {code_value}__{measure} column."""
    narrow = build_frame(
        data={
            "UNINUM": [1, 1],
            "period": ["2026-03-31", "2026-03-31"],
            "code_column": ["LOAN_PORTFOLIO", "LOAN_PORTFOLIO"],
            "code_value": [100.0, 105.0],
            "accruing": [10.0, 20.0],
            "charge_off": [1.0, 2.0],
        }
    )
    wide = pivot_domain_dataset_wide(frame=narrow)
    rows = rows_of(wide)
    assert len(rows) == 1
    row = rows[0]
    assert row["100__accruing"] == 10.0
    assert row["105__accruing"] == 20.0
    assert row["100__charge_off"] == 1.0
    assert row["105__charge_off"] == 2.0
    assert "code_column" not in wide.columns
    assert "code_value" not in wide.columns


def test_pivot_domain_dataset_wide_drops_code_column(backend: str) -> None:
    """code_column is dropped, since a dataset only ever declares one.

    Two institutions rather than one, since a genuinely single-row
    pivot input hits the pyarrow backend's pre-existing single-row pivot
    bug (see issue #79), which this test is not exercising.
    """
    narrow = build_frame(
        data={
            "UNINUM": [1, 2],
            "period": ["2026-03-31", "2026-03-31"],
            "code_column": ["LOAN_PORTFOLIO", "LOAN_PORTFOLIO"],
            "code_value": [100.0, 100.0],
            "accruing": [10.0, 20.0],
        }
    )
    wide = pivot_domain_dataset_wide(frame=narrow)
    assert set(wide.columns) == {"UNINUM", "period", "100__accruing"}
