"""Tests for the shared narwhals-backed frame helpers (call_report.core._backend)."""

from __future__ import annotations

from typing import Any, get_args

import narwhals as nw
import pandas as pd
import polars as pl
import pyarrow as pa
import pytest

from call_report import core
from call_report.config import config_context
from call_report.core import _backend
from call_report.core._backend import (
    DataFrameType,
    _dataframe_type_of,
    _pyarrow_pivot,
    assert_unique_grain,
    build_frame,
    concat,
    convert_dataframe_type,
    finalize,
    finalize_as,
    is_in_null_safe,
    pivot,
)
from call_report.exceptions import LayoutParseError, ReshapeError
from tests.helpers import ALL_BACKENDS


def test_build_frame_returns_eager_narwhals_frame() -> None:
    """build_frame wraps columnar data as an eager narwhals DataFrame."""
    with config_context(dataframe_backend="pandas"):
        frame = build_frame(data={"UNINUM": [1, 2], "TOTASSETS": [100, 200]})
    assert isinstance(frame, nw.DataFrame)
    assert frame.columns == ["UNINUM", "TOTASSETS"]


def test_finalize_returns_native_eager_frame_by_default() -> None:
    """finalize() unwraps to a native, eager frame when lazy is not configured."""
    with config_context(dataframe_backend="pandas", lazy=False):
        frame = build_frame(data={"UNINUM": [1, 2]})
        result = finalize(frame=frame)
    assert isinstance(result, pd.DataFrame)


def test_finalize_returns_native_lazy_frame_when_configured() -> None:
    """finalize() returns a LazyFrame when lazy=True is configured."""
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
            concat(frames=[frame], how="bogus")  # type: ignore[call-overload]


# ---------------------------------------------------------------------------
# concat / finalize / pivot with lazy (polars.LazyFrame) input
# ---------------------------------------------------------------------------


def _lazy_frame(*, data: dict[str, list[Any]]) -> nw.LazyFrame[Any]:
    with config_context(dataframe_backend="polars"):
        frame = build_frame(data=data)
    result = frame.lazy()
    assert isinstance(result, nw.LazyFrame)
    return result


def test_finalize_accepts_an_already_lazy_frame() -> None:
    """finalize() is a no-op passthrough for a frame that's already lazy."""
    lazy = _lazy_frame(data={"UNINUM": [1, 2]})
    with config_context(dataframe_backend="polars", lazy=True):
        result = finalize(frame=lazy)
    assert isinstance(result, pl.LazyFrame)
    assert result.collect().to_dicts() == [{"UNINUM": 1}, {"UNINUM": 2}]


def test_concat_union_preserves_laziness() -> None:
    """how='union' returns a LazyFrame, uncollected, when given LazyFrame input."""
    first = _lazy_frame(data={"UNINUM": [1], "TOTASSETS": [100]})
    second = _lazy_frame(data={"UNINUM": [2], "TOTASSETS": [200], "TOTLIAB": [50]})
    stacked = concat(frames=[first, second], how="union")
    assert isinstance(stacked, nw.LazyFrame)
    assert set(stacked.collect().columns) == {"UNINUM", "TOTASSETS", "TOTLIAB"}


def test_concat_intersection_preserves_laziness() -> None:
    """how='intersection' returns an uncollected LazyFrame given LazyFrame input."""
    first = _lazy_frame(data={"UNINUM": [1], "TOTASSETS": [100]})
    second = _lazy_frame(data={"UNINUM": [2], "TOTASSETS": [200], "TOTLIAB": [50]})
    stacked = concat(frames=[first, second], how="intersection")
    assert isinstance(stacked, nw.LazyFrame)
    assert set(stacked.collect().columns) == {"UNINUM", "TOTASSETS"}


def test_concat_strict_preserves_laziness() -> None:
    """how='strict' returns a LazyFrame, uncollected, when given LazyFrame input."""
    first = _lazy_frame(data={"UNINUM": [1], "TOTASSETS": [100]})
    second = _lazy_frame(data={"UNINUM": [2], "TOTASSETS": [200]})
    stacked = concat(frames=[first, second], how="strict")
    assert isinstance(stacked, nw.LazyFrame)
    assert len(stacked.collect().rows(named=True)) == 2


@pytest.mark.parametrize("how", ["intersection", "strict"])
def test_concat_lazy_does_not_emit_performance_warning(how: str) -> None:
    """Schema comparisons use collect_schema, not .columns, to avoid the warning."""
    import warnings

    first = _lazy_frame(data={"UNINUM": [1], "TOTASSETS": [100]})
    second = _lazy_frame(data={"UNINUM": [2], "TOTASSETS": [200]})
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        concat(frames=[first, second], how=how)  # type: ignore[call-overload]


def test_pivot_collects_a_lazy_frame() -> None:
    """pivot() accepts a LazyFrame, collecting it internally before pivoting."""
    frame = _lazy_frame(
        data={
            "UNINUM": [1, 1, 2, 2],
            "period": ["2026-03-31"] * 4,
            "key": ["A", "B", "A", "B"],
            "value": [10, 20, 30, 40],
        }
    )
    result = pivot(frame=frame, on="key", index=["UNINUM", "period"], values="value")
    assert isinstance(result, nw.DataFrame)
    rows = {row["UNINUM"]: row for row in result.sort(["UNINUM"]).rows(named=True)}
    assert rows[1]["A"] == 10
    assert rows[1]["B"] == 20
    assert rows[2]["A"] == 30
    assert rows[2]["B"] == 40


def test_finalize_as_accepts_an_already_lazy_frame() -> None:
    """finalize_as() finalizes-and-converts starting from an already-lazy frame."""
    lazy = _lazy_frame(data={"UNINUM": [1, 2]})
    with config_context(dataframe_backend="polars", lazy=True):
        result = finalize_as(frame=lazy, dataframe_type=None)
    assert isinstance(result, pl.LazyFrame)
    assert result.collect().to_dicts() == [{"UNINUM": 1}, {"UNINUM": 2}]


def test_finalize_as_converts_an_already_lazy_frame_to_a_non_lazy_type() -> None:
    """finalize_as() collects an already-lazy frame when a non-lazy type is asked."""
    lazy = _lazy_frame(data={"UNINUM": [1, 2]})
    with config_context(dataframe_backend="polars", lazy=True):
        result = finalize_as(frame=lazy, dataframe_type="pandas")
    assert isinstance(result, pd.DataFrame)
    assert result["UNINUM"].tolist() == [1, 2]


# ---------------------------------------------------------------------------
# assert_unique_grain
# ---------------------------------------------------------------------------


def test_assert_unique_grain_returns_the_eager_frame_unchanged_when_unique() -> None:
    """A genuinely unique grain passes through, still eager, no error."""
    with config_context(dataframe_backend="pandas"):
        frame = build_frame(data={"UNINUM": [1, 2], "period": ["2026-03-31"] * 2})
    result = assert_unique_grain(frame=frame, columns=["UNINUM", "period"])
    assert isinstance(result, nw.DataFrame)
    assert result.shape[0] == 2


def test_assert_unique_grain_collects_a_lazy_frame_first() -> None:
    """A LazyFrame input is collected, then checked, returning an eager frame."""
    frame = _lazy_frame(data={"UNINUM": [1, 2], "period": ["2026-03-31"] * 2})
    result = assert_unique_grain(frame=frame, columns=["UNINUM", "period"])
    assert isinstance(result, nw.DataFrame)
    assert result.shape[0] == 2


def test_assert_unique_grain_raises_on_a_genuine_duplicate() -> None:
    """A duplicated grain raises ReshapeError naming the offending columns."""
    with config_context(dataframe_backend="pandas"):
        frame = build_frame(
            data={"UNINUM": [1, 1], "period": ["2026-03-31", "2026-03-31"]}
        )
    with pytest.raises(ReshapeError, match=r"\['UNINUM', 'period'\]"):
        assert_unique_grain(frame=frame, columns=["UNINUM", "period"])


def test_assert_unique_grain_is_keyword_only() -> None:
    """assert_unique_grain takes no positional arguments."""
    with config_context(dataframe_backend="pandas"):
        frame = build_frame(data={"UNINUM": [1]})
    with pytest.raises(TypeError):
        assert_unique_grain(frame, ["UNINUM"])  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# convert_dataframe_type
# ---------------------------------------------------------------------------


def test_dataframe_type_of_identifies_each_supported_type() -> None:
    """_dataframe_type_of correctly labels a frame of each backend/laziness."""
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
    with config_context(dataframe_backend="polars", lazy=True):
        native = finalize(frame=build_frame(data={"UNINUM": [1, 2]}))
    result = convert_dataframe_type(data=native, dataframe_type="pandas")
    assert isinstance(result, pd.DataFrame)
    assert result["UNINUM"].tolist() == [1, 2]


_TARGET_TYPES: list[DataFrameType] = [
    "pandas",
    "pyarrow_table",
    "polars_dataframe",
    "polars_lazyframe",
]


@pytest.mark.parametrize("source_backend", ALL_BACKENDS)
@pytest.mark.parametrize("target_type", _TARGET_TYPES)
def test_convert_dataframe_type_matrix(
    source_backend: str, target_type: DataFrameType
) -> None:
    """Every (source backend, target type) combination converts correctly."""
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
    with config_context(dataframe_backend="pandas", lazy=False):
        frame = build_frame(data={"UNINUM": [1, 2]})
        result = finalize_as(frame=frame, dataframe_type="pyarrow_table")
    assert isinstance(result, pa.Table)


def test_finalize_as_none_dataframe_type_matches_finalize_alone() -> None:
    """finalize_as with dataframe_type=None behaves exactly like finalize()."""
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
# is_in_null_safe
# ---------------------------------------------------------------------------


def test_is_in_null_safe_matches_a_listed_value(backend: str) -> None:
    """A value in the set tests True on every backend."""
    frame = build_frame(data={"code": [110, 155]})
    kept = frame.filter(is_in_null_safe(column="code", values=[155]))
    assert kept["code"].to_list() == [155]


def test_is_in_null_safe_answers_false_for_a_null(backend: str) -> None:
    """A null tests False rather than null, so filtering it is not backend-dependent.

    `is_in` against a null is itself null, and a null predicate under
    `filter` disagrees across backends: pandas keeps the row, polars and
    pyarrow drop it. A null is not a member of any set of known values,
    so every backend must keep it when the test is negated.
    """
    frame = build_frame(data={"code": [110, None]})
    kept = frame.filter(~is_in_null_safe(column="code", values=[155]))
    assert kept.shape[0] == 2


# ---------------------------------------------------------------------------
# pivot / _pyarrow_pivot
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


def test_pivot_pyarrow_path_dispatches_to_pyarrow_pivot() -> None:
    """Pyarrow input is routed through _pyarrow_pivot and produces the same result."""
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


def test_pivot_duplicate_grain_raises_reshape_error_pyarrow() -> None:
    """A genuine duplicate (index, on) grain raises ReshapeError on the pyarrow path."""
    frame = _build_duplicate_grain_frame(backend="pyarrow")
    with pytest.raises(ReshapeError, match="not a unique grain"):
        pivot(frame=frame, on="key", index=["UNINUM", "period"], values="value")


def test_pyarrow_pivot_matches_native_pivot_output() -> None:
    """_pyarrow_pivot produces output identical to the native pandas pivot path."""
    native_result = _build_long_frame(backend="pandas").pivot(
        on="key", index=["UNINUM", "period"], values="value", sort_columns=True
    )
    pyarrow_result = _pyarrow_pivot(
        frame=_build_long_frame(backend="pyarrow"),
        on="key",
        index=["UNINUM", "period"],
        values="value",
    )
    assert pyarrow_result.sort(["UNINUM"]).rows(named=True) == native_result.sort(
        ["UNINUM"]
    ).rows(named=True)


def _pyarrow_frame(table: pa.Table) -> nw.DataFrame[Any]:
    """Wrap a pyarrow table for the _pyarrow_pivot tests below."""
    return nw.from_native(table, eager_only=True)


def test_pyarrow_pivot_returns_index_only_frame_for_empty_input() -> None:
    """An empty long frame pivots to an empty frame carrying just the index.

    There is no `on` value to name a column after, so the result has no
    value columns. Without the zero-row branch the adjacent-row
    comparison would be asked for a slice of negative length.
    """
    result = _pyarrow_pivot(
        frame=_pyarrow_frame(
            pa.table(
                {
                    "UNINUM": pa.array([], type=pa.int64()),
                    "key": pa.array([], type=pa.string()),
                    "value": pa.array([], type=pa.float64()),
                }
            )
        ),
        on="key",
        index=["UNINUM"],
        values="value",
    )
    assert result.columns == ["UNINUM"]
    assert result.shape == (0, 1)


def test_pyarrow_pivot_fills_gaps_for_a_key_missing_from_some_rows() -> None:
    """A key absent from an index row gets null there, not a shifted value.

    A key present for every row is taken straight from the sorted values,
    while a sparse one has to be realigned against the full row list. The
    two paths are separate, so a frame needs both to exercise them.
    """
    result = _pyarrow_pivot(
        frame=_pyarrow_frame(
            pa.table(
                {
                    "UNINUM": [1, 1, 2],
                    "key": ["dense", "sparse", "dense"],
                    "value": [10.0, 20.0, 30.0],
                }
            )
        ),
        on="key",
        index=["UNINUM"],
        values="value",
    )
    assert result.columns == ["UNINUM", "dense", "sparse"]
    assert result.rows(named=True) == [
        {"UNINUM": 1, "dense": 10.0, "sparse": 20.0},
        {"UNINUM": 2, "dense": 30.0, "sparse": None},
    ]


def test_pyarrow_pivot_treats_two_null_index_values_as_one_row() -> None:
    """Rows sharing a null in an index column belong to the same output row.

    A comparison against null is null rather than true, so without
    handling nulls explicitly every null-keyed row would look like the
    start of a new index group and the result would carry a row per long
    row.
    """
    result = _pyarrow_pivot(
        frame=_pyarrow_frame(
            pa.table(
                {
                    "UNINUM": [1, None, None],
                    "period": ["2026-03-31"] * 3,
                    "key": ["A", "A", "B"],
                    "value": [10.0, 20.0, 30.0],
                }
            )
        ),
        on="key",
        index=["UNINUM", "period"],
        values="value",
    )
    assert result.shape == (2, 4)
    assert result.rows(named=True)[1] == {
        "UNINUM": None,
        "period": "2026-03-31",
        "A": 20.0,
        "B": 30.0,
    }


def test_pyarrow_pivot_keeps_a_non_numeric_value_column_intact() -> None:
    """The value column's dtype survives the reshape.

    Each output column is cut from the value column itself, so nothing
    re-infers a dtype from the values.
    """
    result = _pyarrow_pivot(
        frame=_pyarrow_frame(
            pa.table({"UNINUM": [1, 2], "key": ["A", "B"], "value": ["x", "y"]})
        ),
        on="key",
        index=["UNINUM"],
        values="value",
    )
    assert result.schema["A"] == nw.String()
    assert result.rows(named=True) == [
        {"UNINUM": 1, "A": "x", "B": None},
        {"UNINUM": 2, "A": None, "B": "y"},
    ]


def test_pyarrow_pivot_handles_a_multi_chunk_table() -> None:
    """A table whose columns arrive in several chunks pivots correctly.

    Concatenating tables (as stacking a schedule across periods does)
    leaves each column chunked, and the compute kernels the pivot uses
    need one contiguous array per column.
    """
    table = pa.concat_tables(
        [
            pa.table({"UNINUM": [1], "key": ["A"], "value": [10.0]}),
            pa.table({"UNINUM": [2], "key": ["B"], "value": [20.0]}),
        ]
    )
    assert table.column("UNINUM").num_chunks == 2
    result = _pyarrow_pivot(
        frame=_pyarrow_frame(table), on="key", index=["UNINUM"], values="value"
    )
    assert result.rows(named=True) == [
        {"UNINUM": 1, "A": 10.0, "B": None},
        {"UNINUM": 2, "A": None, "B": 20.0},
    ]


def test_pivot_is_keyword_only() -> None:
    """Pivot takes no positional arguments."""
    frame = _build_long_frame(backend="pandas")
    with pytest.raises(TypeError):
        pivot(frame, "key", ["UNINUM", "period"], "value")  # type: ignore[call-arg]


def test_build_frame_declares_dtypes_the_values_cannot_supply(backend: str) -> None:
    """A declared schema types a column the values give no basis to infer.

    Every backend infers a dtype from the values, so a column that is
    entirely null leaves each of them to guess, and they guess
    differently: polars and pyarrow report Unknown where pandas reports
    String. Declaring the dtype is what makes the three agree. See issue
    #43.
    """
    schema: dict[str, nw.dtypes.DType] = {
        "ALLNULL": nw.Int64(),
        "TEXT": nw.String(),
        "AMOUNT": nw.Float64(),
    }
    frame = build_frame(
        data={"ALLNULL": [None, None], "TEXT": [None, "x"], "AMOUNT": [1.5, None]},
        schema=schema,
    )
    assert dict(frame.collect_schema()) == schema


def test_build_frame_declares_dtypes_on_a_frame_with_no_rows(backend: str) -> None:
    """A zero-row frame carries its declared dtypes rather than Unknown.

    FCA publishes a zero-byte RCO data file for 2000Q1 through 2003Q4, so
    an empty frame with usable dtypes is a real requirement, not a
    hypothetical one.
    """
    schema: dict[str, nw.dtypes.DType] = {"UNINUM": nw.Int64(), "NAME": nw.String()}
    frame = build_frame(data={"UNINUM": [], "NAME": []}, schema=schema)
    assert dict(frame.collect_schema()) == schema
    assert len(frame) == 0


def test_build_frame_declared_integers_hold_nulls(backend: str) -> None:
    """A declared Int64 column keeps that dtype while holding a null.

    pandas' default integer dtype is numpy-backed and cannot hold a null,
    so honoring the declaration there requires its nullable extension
    dtype. Without that, `load` hands back Float64 for a field the
    packaged metadata declares Int64.
    """
    frame = build_frame(data={"COUNT": [1, None, 3]}, schema={"COUNT": nw.Int64()})
    assert frame.collect_schema()["COUNT"] == nw.Int64()
    # Each backend spells its own missing value, so ask narwhals instead of
    # comparing against None.
    assert frame["COUNT"].is_null().to_list() == [False, True, False]


def test_build_frame_ignores_a_schema_entry_for_an_absent_column(backend: str) -> None:
    """A schema naming a column the data lacks does not add that column."""
    frame = build_frame(
        data={"UNINUM": [1]}, schema={"UNINUM": nw.Int64(), "ABSENT": nw.String()}
    )
    assert frame.columns == ["UNINUM"]


def test_build_frame_infers_a_column_the_schema_omits(backend: str) -> None:
    """A column absent from the schema keeps its inferred dtype."""
    frame = build_frame(
        data={"UNINUM": [1], "NOTE": ["a"]}, schema={"UNINUM": nw.Int64()}
    )
    assert frame.collect_schema()["UNINUM"] == nw.Int64()
    assert frame.collect_schema()["NOTE"] == nw.String()


def test_build_frame_falls_back_for_a_column_the_values_contradict(
    backend: str,
) -> None:
    """An unrepresentable value costs one column's declared dtype, not the frame's.

    `fca.reader._cast` keeps a non-numeric value in a Numeric-typed field
    as a string rather than discarding it. That value cannot be held as
    the declared Int64, so that one column infers instead, while every
    other column still gets what the layout declares.
    """
    frame = build_frame(
        data={"UNINUM": [1, 2], "TOTASSETS": ["NA", "n/a"], "ALLNULL": [None, None]},
        schema={
            "UNINUM": nw.Int64(),
            "TOTASSETS": nw.Int64(),
            "ALLNULL": nw.Int64(),
        },
    )
    schema = frame.collect_schema()
    assert schema["TOTASSETS"] == nw.String()
    assert schema["UNINUM"] == nw.Int64()
    assert schema["ALLNULL"] == nw.Int64()
    assert frame["TOTASSETS"].to_list() == ["NA", "n/a"]


def test_build_frame_without_a_schema_still_infers(backend: str) -> None:
    """Omitting the schema leaves every column to the backend's inference."""
    frame = build_frame(data={"UNINUM": [1, 2], "NAME": ["a", "b"]})
    assert frame.collect_schema()["UNINUM"] == nw.Int64()
    assert frame.collect_schema()["NAME"] == nw.String()


def test_dataframe_aliases_are_public_and_importable() -> None:
    """NativeDataFrame and DataFrameType are part of the call_report.core API.

    Both appear in the signature of nearly every public dataframe-returning
    method, and docs/source/api_reference.rst documents them as importable
    from ``call_report.core``. Without the re-export a reader following the
    documentation would have to import from a private module instead.
    """
    assert core.NativeDataFrame is _backend.NativeDataFrame
    assert core.DataFrameType is _backend.DataFrameType
    assert "NativeDataFrame" in core.__all__
    assert "DataFrameType" in core.__all__


def test_native_dataframe_alias_names_every_supported_backend_type() -> None:
    """The NativeDataFrame alias covers exactly the types conversion produces.

    The alias is a string rather than a real union, so no type checker
    verifies it against convert_dataframe_type's overloads. This does.
    """
    assert _backend.NativeDataFrame == (
        "pandas.DataFrame | pyarrow.Table | polars.DataFrame | polars.LazyFrame"
    )


def test_dataframe_type_alias_matches_the_supported_names() -> None:
    """DataFrameType's members are exactly the names convert_dataframe_type takes.

    The Literal and the frozenset it is validated against are declared
    separately, so a value added to one and not the other would otherwise
    only surface as a type-checking or runtime mismatch at a call site.
    """
    assert set(get_args(DataFrameType)) == _backend._SUPPORTED_DATAFRAME_TYPES
