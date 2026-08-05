"""Tests for the shared narwhals-backed frame helpers (call_report.core._backend)."""

from __future__ import annotations

from typing import Any

import narwhals as nw
import pytest

from call_report.config import config_context
from call_report.core._backend import (
    DataFrameType,
    _dataframe_type_of,
    _join_on_index,
    _manual_pivot,
    build_frame,
    concat,
    convert_dataframe_type,
    finalize,
    finalize_as,
    pivot,
)
from call_report.exceptions import LayoutParseError, ReshapeError


def test_build_frame_returns_eager_narwhals_frame() -> None:
    """build_frame wraps columnar data as an eager narwhals DataFrame."""
    with config_context(dataframe_backend="pandas"):
        frame = build_frame(data={"UNINUM": [1, 2], "TOTASSETS": [100, 200]})
    assert isinstance(frame, nw.DataFrame)
    assert frame.columns == ["UNINUM", "TOTASSETS"]


def test_finalize_returns_native_eager_frame_by_default() -> None:
    """finalize() unwraps to a native, eager frame when lazy is not configured."""
    import pandas as pd

    with config_context(dataframe_backend="pandas", lazy=False):
        frame = build_frame(data={"UNINUM": [1, 2]})
        result = finalize(frame=frame)
    assert isinstance(result, pd.DataFrame)


def test_finalize_returns_native_lazy_frame_when_configured() -> None:
    """finalize() returns a LazyFrame when lazy=True is configured."""
    import polars as pl

    with config_context(dataframe_backend="polars", lazy=True):
        frame = build_frame(data={"UNINUM": [1, 2]})
        result = finalize(frame=frame)
    assert isinstance(result, pl.LazyFrame)


def test_concat_union_outer_joins_and_nulls_missing_columns() -> None:
    """how='union' keeps every column, nulling it out where a frame lacks it."""
    with config_context(dataframe_backend="pandas"):
        first = build_frame(data={"UNINUM": [1], "TOTASSETS": [100]})
        second = build_frame(data={"UNINUM": [2], "TOTASSETS": [200], "TOTLIAB": [50]})
        stacked = concat(frames=[first, second], how="union")
    rows = stacked.rows(named=True)
    assert len(rows) == 2
    assert set(stacked.columns) == {"UNINUM", "TOTASSETS", "TOTLIAB"}


def test_concat_intersection_drops_uncommon_columns() -> None:
    """how='intersection' keeps only columns common to every frame."""
    with config_context(dataframe_backend="pandas"):
        first = build_frame(data={"UNINUM": [1], "TOTASSETS": [100]})
        second = build_frame(data={"UNINUM": [2], "TOTASSETS": [200], "TOTLIAB": [50]})
        stacked = concat(frames=[first, second], how="intersection")
    assert set(stacked.columns) == {"UNINUM", "TOTASSETS"}
    assert len(stacked.rows(named=True)) == 2


def test_concat_strict_succeeds_when_columns_match() -> None:
    """how='strict' stacks cleanly when every frame already shares columns."""
    with config_context(dataframe_backend="pandas"):
        first = build_frame(data={"UNINUM": [1], "TOTASSETS": [100]})
        second = build_frame(data={"UNINUM": [2], "TOTASSETS": [200]})
        stacked = concat(frames=[first, second], how="strict")
    assert len(stacked.rows(named=True)) == 2


def test_concat_strict_raises_when_columns_differ() -> None:
    """how='strict' refuses to silently reconcile differing columns."""
    with config_context(dataframe_backend="pandas"):
        first = build_frame(data={"UNINUM": [1], "TOTASSETS": [100]})
        second = build_frame(data={"UNINUM": [2], "TOTASSETS": [200], "TOTLIAB": [50]})
        with pytest.raises(LayoutParseError):
            concat(frames=[first, second], how="strict")


def test_build_frame_and_finalize_are_keyword_only() -> None:
    """build_frame and finalize take no positional arguments."""
    with pytest.raises(TypeError):
        build_frame({"UNINUM": [1]})  # type: ignore[call-arg]
    frame = build_frame(data={"UNINUM": [1]})
    with pytest.raises(TypeError):
        finalize(frame)  # type: ignore[call-arg]


def test_concat_rejects_unknown_schema_policy() -> None:
    """An unrecognized schema policy raises a clear ValueError."""
    with config_context(dataframe_backend="pandas"):
        frame = build_frame(data={"UNINUM": [1]})
        with pytest.raises(ValueError, match="Unknown schema policy"):
            concat(frames=[frame], how="bogus")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# convert_dataframe_type
# ---------------------------------------------------------------------------


def test_dataframe_type_of_identifies_each_supported_type() -> None:
    """_dataframe_type_of correctly labels a frame of each backend/laziness."""
    import pandas as pd
    import polars as pl
    import pyarrow as pa

    data = {"a": [1, 2]}
    assert _dataframe_type_of(pd.DataFrame(data)) == "pandas"
    assert _dataframe_type_of(pa.table(data)) == "pyarrow_table"
    assert _dataframe_type_of(pl.DataFrame(data)) == "polars_dataframe"
    assert _dataframe_type_of(pl.LazyFrame(data)) == "polars_lazyframe"


def test_convert_dataframe_type_none_returns_input_unchanged() -> None:
    """dataframe_type=None is a no-op, returning the exact same object."""
    with config_context(dataframe_backend="pandas"):
        native = finalize(frame=build_frame(data={"UNINUM": [1]}))
    result = convert_dataframe_type(data=native, dataframe_type=None)
    assert result is native


def test_convert_dataframe_type_already_matching_returns_input_unchanged() -> None:
    """Requesting the type `data` already is skips conversion entirely."""
    with config_context(dataframe_backend="pandas"):
        native = finalize(frame=build_frame(data={"UNINUM": [1]}))
    result = convert_dataframe_type(data=native, dataframe_type="pandas")
    assert result is native


def test_convert_dataframe_type_lazy_already_matching_skips_collect() -> None:
    """A lazy source already matching the target is returned without collecting."""
    with config_context(dataframe_backend="polars", lazy=True):
        native = finalize(frame=build_frame(data={"UNINUM": [1]}))
    result = convert_dataframe_type(data=native, dataframe_type="polars_lazyframe")
    assert result is native


def test_convert_dataframe_type_collects_lazy_source_when_converting() -> None:
    """A lazy source converting to a different type is collected first."""
    import pandas as pd

    with config_context(dataframe_backend="polars", lazy=True):
        native = finalize(frame=build_frame(data={"UNINUM": [1, 2]}))
    result = convert_dataframe_type(data=native, dataframe_type="pandas")
    assert isinstance(result, pd.DataFrame)
    assert result["UNINUM"].tolist() == [1, 2]


_SOURCE_BACKENDS = ["pandas", "polars", "pyarrow"]
_TARGET_TYPES: list[DataFrameType] = [
    "pandas",
    "pyarrow_table",
    "polars_dataframe",
    "polars_lazyframe",
]


@pytest.mark.parametrize("source_backend", _SOURCE_BACKENDS)
@pytest.mark.parametrize("target_type", _TARGET_TYPES)
def test_convert_dataframe_type_matrix(
    source_backend: str, target_type: DataFrameType
) -> None:
    """Every (source backend, target type) combination converts correctly."""
    import pandas as pd
    import polars as pl
    import pyarrow as pa

    with config_context(dataframe_backend=source_backend):
        native = finalize(
            frame=build_frame(data={"UNINUM": [1, 2], "TOTASSETS": [10, 20]})
        )
    result = convert_dataframe_type(data=native, dataframe_type=target_type)

    expected_types: dict[DataFrameType, type] = {
        "pandas": pd.DataFrame,
        "pyarrow_table": pa.Table,
        "polars_dataframe": pl.DataFrame,
        "polars_lazyframe": pl.LazyFrame,
    }
    assert isinstance(result, expected_types[target_type])

    if isinstance(result, pl.LazyFrame):
        result = result.collect()
    rows = nw.from_native(result).rows(named=True)
    assert rows == [
        {"UNINUM": 1, "TOTASSETS": 10},
        {"UNINUM": 2, "TOTASSETS": 20},
    ]


def test_convert_dataframe_type_rejects_unknown_value() -> None:
    """An unsupported dataframe_type raises a clear ValueError."""
    with config_context(dataframe_backend="pandas"):
        native = finalize(frame=build_frame(data={"UNINUM": [1]}))
    with pytest.raises(ValueError, match="dataframe_type must be one of"):
        convert_dataframe_type(data=native, dataframe_type="bogus")  # type: ignore[call-overload]


def test_convert_dataframe_type_is_keyword_only() -> None:
    """convert_dataframe_type takes no positional arguments."""
    with config_context(dataframe_backend="pandas"):
        native = finalize(frame=build_frame(data={"UNINUM": [1]}))
    with pytest.raises(TypeError):
        convert_dataframe_type(native, None)  # type: ignore[call-overload]


# ---------------------------------------------------------------------------
# finalize_as
# ---------------------------------------------------------------------------


def test_finalize_as_finalizes_and_converts_in_one_step() -> None:
    """finalize_as combines finalize() and convert_dataframe_type()."""
    import pyarrow as pa

    with config_context(dataframe_backend="pandas", lazy=False):
        frame = build_frame(data={"UNINUM": [1, 2]})
        result = finalize_as(frame=frame, dataframe_type="pyarrow_table")
    assert isinstance(result, pa.Table)


def test_finalize_as_none_dataframe_type_matches_finalize_alone() -> None:
    """finalize_as with dataframe_type=None behaves exactly like finalize()."""
    import pandas as pd

    with config_context(dataframe_backend="pandas", lazy=False):
        frame = build_frame(data={"UNINUM": [1, 2]})
        result = finalize_as(frame=frame, dataframe_type=None)
    assert isinstance(result, pd.DataFrame)


def test_finalize_as_is_keyword_only() -> None:
    """finalize_as takes no positional arguments."""
    with config_context(dataframe_backend="pandas"):
        frame = build_frame(data={"UNINUM": [1]})
    with pytest.raises(TypeError):
        finalize_as(frame, None)  # type: ignore[call-overload]


# ---------------------------------------------------------------------------
# pivot / _manual_pivot / _join_on_index
# ---------------------------------------------------------------------------


def _build_long_frame(*, backend: str) -> nw.DataFrame[Any]:
    with config_context(dataframe_backend=backend):
        return build_frame(
            data={
                "UNINUM": [1, 1, 2, 2],
                "period": ["2026-03-31"] * 4,
                "key": ["A", "B", "A", "B"],
                "value": [10, 20, 30, 40],
            }
        )


def _build_duplicate_grain_frame(*, backend: str) -> nw.DataFrame[Any]:
    with config_context(dataframe_backend=backend):
        return build_frame(
            data={
                "UNINUM": [1, 1, 1],
                "period": ["2026-03-31"] * 3,
                "key": ["A", "A", "B"],
                "value": [10, 99, 20],
            }
        )


@pytest.mark.parametrize("backend", ["pandas", "polars"])
def test_pivot_native_path_pivots_correctly(backend: str) -> None:
    """The native pivot path produces the expected wide shape and values."""
    frame = _build_long_frame(backend=backend)
    result = pivot(frame=frame, on="key", index=["UNINUM", "period"], values="value")
    rows = {row["UNINUM"]: row for row in result.sort(["UNINUM"]).rows(named=True)}
    assert result.columns == ["UNINUM", "period", "A", "B"]
    assert rows[1]["A"] == 10
    assert rows[1]["B"] == 20
    assert rows[2]["A"] == 30
    assert rows[2]["B"] == 40


def test_pivot_pyarrow_path_dispatches_to_manual_pivot() -> None:
    """Pyarrow input is routed through _manual_pivot and produces the same result."""
    frame = _build_long_frame(backend="pyarrow")
    assert frame.implementation is nw.Implementation.PYARROW
    result = pivot(frame=frame, on="key", index=["UNINUM", "period"], values="value")
    rows = {row["UNINUM"]: row for row in result.sort(["UNINUM"]).rows(named=True)}
    assert set(result.columns) == {"UNINUM", "period", "A", "B"}
    assert rows[1]["A"] == 10
    assert rows[1]["B"] == 20
    assert rows[2]["A"] == 30
    assert rows[2]["B"] == 40


@pytest.mark.parametrize("backend", ["pandas", "polars", "pyarrow"])
def test_pivot_matches_across_backends(backend: str) -> None:
    """Every backend's pivot path (native or manual) agrees on the same input."""
    reference = pivot(
        frame=_build_long_frame(backend="pandas"),
        on="key",
        index=["UNINUM", "period"],
        values="value",
    )
    result = pivot(
        frame=_build_long_frame(backend=backend),
        on="key",
        index=["UNINUM", "period"],
        values="value",
    )
    reference_rows = reference.sort(["UNINUM"]).rows(named=True)
    result_rows = result.sort(["UNINUM"]).select(reference.columns).rows(named=True)
    assert result_rows == reference_rows


@pytest.mark.parametrize("backend", ["pandas", "polars"])
def test_pivot_duplicate_grain_raises_reshape_error_native(backend: str) -> None:
    """A genuine duplicate (index, on) grain raises ReshapeError on the native path."""
    frame = _build_duplicate_grain_frame(backend=backend)
    with pytest.raises(ReshapeError, match="Could not pivot"):
        pivot(frame=frame, on="key", index=["UNINUM", "period"], values="value")


def test_pivot_duplicate_grain_raises_reshape_error_manual() -> None:
    """A genuine duplicate (index, on) grain raises ReshapeError on the manual path."""
    frame = _build_duplicate_grain_frame(backend="pyarrow")
    with pytest.raises(ReshapeError, match="not a unique grain"):
        pivot(frame=frame, on="key", index=["UNINUM", "period"], values="value")


def test_manual_pivot_matches_native_pivot_output() -> None:
    """_manual_pivot produces output identical to the native pandas pivot path."""
    native_result = _build_long_frame(backend="pandas").pivot(
        on="key", index=["UNINUM", "period"], values="value", sort_columns=True
    )
    manual_result = _manual_pivot(
        frame=_build_long_frame(backend="pandas"),
        on="key",
        index=["UNINUM", "period"],
        values="value",
    )
    assert manual_result.sort(["UNINUM"]).rows(named=True) == native_result.sort(
        ["UNINUM"]
    ).rows(named=True)


def test_pivot_is_keyword_only() -> None:
    """Pivot takes no positional arguments."""
    frame = _build_long_frame(backend="pandas")
    with pytest.raises(TypeError):
        pivot(frame, "key", ["UNINUM", "period"], "value")  # type: ignore[call-arg]


def test_join_on_index_coalesces_shared_join_keys() -> None:
    """_join_on_index drops every backend's `{col}_right` join-key duplicate."""
    with config_context(dataframe_backend="pandas"):
        left = build_frame(data={"UNINUM": [1, 2], "period": ["a", "a"], "A": [10, 20]})
        right = build_frame(
            data={"UNINUM": [2, 3], "period": ["a", "a"], "B": [30, 40]}
        )
    joined = _join_on_index(left=left, right=right, index=["UNINUM", "period"])
    assert set(joined.columns) == {"UNINUM", "period", "A", "B"}
    rows = {row["UNINUM"]: row for row in joined.rows(named=True)}

    def _is_missing(value: object) -> bool:
        return value is None or value != value  # NaN is the only value != itself

    assert rows[1]["A"] == 10
    assert _is_missing(rows[1]["B"])
    assert _is_missing(rows[3]["A"])
    assert rows[3]["B"] == 40
