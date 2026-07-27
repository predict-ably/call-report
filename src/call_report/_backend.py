"""Private narwhals-backed helpers for building and stacking dataframes.

Every reader in this package is written against these three primitives so
none of them need to know anything about the specific dataframe library
configured via :mod:`call_report.config`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

import narwhals as nw

from call_report.config import get_config
from call_report.exceptions import LayoutParseError

SchemaPolicy = Literal["union", "intersection", "strict"]


def build_frame(*, data: dict[str, list[Any]]) -> nw.DataFrame[Any]:
    """Build an eager narwhals DataFrame from columnar data.

    Uses the dataframe library named by the current
    :func:`~call_report.config.get_config` (never lazy, regardless of the
    ``"lazy"`` setting) -- laziness is applied once, at the public return
    boundary, by :func:`finalize`.

    Parameters
    ----------
    data : dict[str, list[Any]]
        Column name to column values, as produced by a parser.

    Returns
    -------
    narwhals.DataFrame
        An eager narwhals-wrapped frame of the configured backend.
    """
    backend = get_config()["dataframe_backend"]
    return nw.from_dict(data, backend=backend)


def finalize(*, frame: nw.DataFrame[Any]) -> Any:
    """Apply the configured laziness and unwrap to a native frame.

    This is the single point where every public, frame-returning function
    in this package converts its internal, always-eager narwhals frame
    into the native object callers actually receive.

    Parameters
    ----------
    frame : narwhals.DataFrame
        The eager narwhals frame to finalize.

    Returns
    -------
    Any
        A native frame of the configured backend -- eager, or lazy if
        ``lazy=True`` is configured (e.g. a ``polars.LazyFrame``).
    """
    config = get_config()
    result: nw.DataFrame[Any] | nw.LazyFrame[Any] = (
        frame.lazy() if config["lazy"] else frame
    )
    return result.to_native()


def concat(
    *, frames: Sequence[nw.DataFrame[Any]], how: SchemaPolicy
) -> nw.DataFrame[Any]:
    """Stack multiple eager narwhals frames according to a schema policy.

    Used to combine one dataframe per requested period into a single
    result, reconciling any schema differences between periods (e.g. a
    column added in a later quarter) according to `how`.

    Parameters
    ----------
    frames : Sequence[narwhals.DataFrame]
        The per-period frames to stack, in the order they should appear.
    how : {"union", "intersection", "strict"}
        ``"union"`` outer-joins columns, nulling out any column a given
        frame lacks. ``"intersection"`` keeps only columns common to every
        frame. ``"strict"`` requires every frame to already share the exact
        same columns, raising `LayoutParseError` otherwise.

    Returns
    -------
    narwhals.DataFrame
        The stacked frame.

    Raises
    ------
    LayoutParseError
        If `how` is ``"strict"`` and the frames' columns are not identical.
    """
    if how == "union":
        return nw.concat(list(frames), how="diagonal")

    if how == "intersection":
        common = set(frames[0].columns)
        for frame in frames[1:]:
            common &= set(frame.columns)
        ordered = [name for name in frames[0].columns if name in common]
        selected = [frame.select(ordered) for frame in frames]
        return nw.concat(selected, how="vertical")

    if how == "strict":
        first_columns = set(frames[0].columns)
        for frame in frames[1:]:
            if set(frame.columns) != first_columns:
                raise LayoutParseError(
                    "schema_policy='strict' requires every stacked period to share "
                    "the exact same columns, but they differ; use 'union' or "
                    "'intersection' to reconcile schema differences across periods."
                )
        return nw.concat(list(frames), how="vertical")

    raise ValueError(
        f"Unknown schema policy {how!r}; expected 'union', 'intersection', or 'strict'."
    )
