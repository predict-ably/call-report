"""Tests for the shared narwhals-backed frame helpers (call_report._backend)."""

from __future__ import annotations

import narwhals as nw
import pytest

from call_report._backend import build_frame, concat, finalize
from call_report.config import config_context
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
