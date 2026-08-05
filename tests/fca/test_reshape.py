"""Tests for the private wide-format reshaping helpers (call_report.fca._reshape)."""

from __future__ import annotations

from typing import Any

import narwhals as nw
import pytest

from call_report.config import config_context
from call_report.core._backend import build_frame
from call_report.exceptions import ReshapeError
from call_report.fca._reshape import (
    _normalize_value_dtype,
    _with_column_key,
    melt_schedule_frame,
    to_wide_format,
)


def _rows(frame: nw.DataFrame[Any]) -> list[dict[str, Any]]:
    return frame.sort(["UNINUM", "period"]).rows(named=True)


# ---------------------------------------------------------------------------
# melt_schedule_frame
# ---------------------------------------------------------------------------


def test_melt_schedule_frame_no_code_column() -> None:
    """A plain (no-code) schedule melts every non-identifier column."""
    with config_context(dataframe_backend="pandas"):
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
    with config_context(dataframe_backend="pandas"):
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
    with config_context(dataframe_backend="pandas"):
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
    with config_context(dataframe_backend="pandas"):
        frame = build_frame(data={"UNINUM": [1], "period": ["2026-03-31"], "A": [1]})
    with pytest.raises(TypeError):
        melt_schedule_frame(frame, "RC", None)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# _normalize_value_dtype
# ---------------------------------------------------------------------------


def test_normalize_value_dtype_casts_numeric_to_float64() -> None:
    """An Int64 `value` column is cast to Float64."""
    with config_context(dataframe_backend="pandas"):
        frame = build_frame(data={"value": [1, 2, 3]})
    assert frame.schema["value"] == nw.Int64()
    result = _normalize_value_dtype(frame)
    assert result.schema["value"] == nw.Float64()


def test_normalize_value_dtype_leaves_non_numeric_untouched() -> None:
    """A String `value` column is left as-is."""
    with config_context(dataframe_backend="pandas"):
        frame = build_frame(data={"value": ["a", "b"]})
    result = _normalize_value_dtype(frame)
    assert result.schema["value"] == nw.String()


# ---------------------------------------------------------------------------
# _with_column_key
# ---------------------------------------------------------------------------


def test_with_column_key_plain_naming_when_no_code_column_present() -> None:
    """A frame with no `code_column` at all gets `{schedule}__{variable}` keys."""
    with config_context(dataframe_backend="pandas"):
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
    with config_context(dataframe_backend="pandas"):
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


def test_with_column_key_mixed_null_and_coded_rows() -> None:
    """A union of coded and non-coded schedules keys each row correctly."""
    with config_context(dataframe_backend="pandas"):
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
        from call_report.core._backend import concat

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
    with config_context(dataframe_backend="pandas"):
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
    rows = {row["UNINUM"]: row for row in _rows(result)}
    assert rows[1]["RC__TOTASSETS"] == 1000.0
    assert rows[1]["RCB__INV_CODE_10__BKVAL"] == 100.0
    assert rows[1]["RCB__INV_CODE_20__BKVAL"] == 150.0
    assert rows[2]["RC__TOTASSETS"] == 2000.0


def test_to_wide_format_duplicate_grain_raises_reshape_error() -> None:
    """A genuinely duplicated (UNINUM, period, variable) row raises ReshapeError."""
    with config_context(dataframe_backend="pandas"):
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


def test_to_wide_format_is_keyword_only() -> None:
    """to_wide_format takes no positional arguments."""
    with config_context(dataframe_backend="pandas"):
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

    from call_report.core._backend import concat

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
    rows = {row["UNINUM"]: row for row in _rows(result)}
    assert rows[1]["RC__TOTASSETS"] == 1000.0
    assert rows[1]["RCB__INV_CODE_10__BKVAL"] == 100.0
    assert rows[2]["RC__TOTASSETS"] == 2000.0
