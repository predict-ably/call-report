"""Tests for the shared narwhals-backed frame helpers (call_report.core._backend)."""

from __future__ import annotations

import narwhals as nw
import pytest

from call_report.config import config_context
from call_report.core._backend import (
    DataFrameType,
    _dataframe_type_of,
    build_frame,
    concat,
    convert_dataframe_type,
    finalize,
)
from call_report.exceptions import LayoutParseError


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
