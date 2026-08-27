"""Shared, importable helpers for the call_report test suite.

These live in a normal module rather than in ``conftest.py`` so test modules
can import them directly. pytest treats every ``conftest.py`` as a plugin it
loads itself, and importing one as a library gives it two identities in
``sys.modules``, so fixtures and helpers defined there can be created twice.
Only fixtures and hooks belong in ``conftest.py``. Everything a test imports
by name belongs here.

The file writers below produce small windows-1252-encoded files that mimic
the *structural* quirks of real FCA Call Report releases. The suite stays
hermetic: no file is stored in ``tests/``, and no parsing test needs network
access.
"""

from __future__ import annotations

import datetime
import math
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import narwhals as nw

ENCODING = "windows-1252"

ALL_BACKENDS = ("pandas", "polars", "pyarrow")
"""The dataframe backends every cross-backend test runs against."""


def _write(path: Path, text: str) -> Path:
    """Encode *text* as windows-1252 bytes and write it to *path*."""
    path.write_bytes(text.encode(ENCODING))
    return path


def layout_text(
    *, title: str, variable_lines: Iterable[str], note: str | None = None
) -> str:
    """Assemble the raw text body of a ``D_<ROOT>.TXT`` layout file."""
    lines = [
        title,
        "                            DATA DELIMITED BY COMMAS",
        "",
        "         VARIABLE    FIELD  DEC.",
        "             NAME    TYPE   POS.  VARIABLE DESCRIPTION",
        "  ---------------  -------  ----  --------------------------------------",
        *variable_lines,
    ]
    if note is not None:
        lines.append(f"  **  NOTE:  {note}")
    return "\n".join(lines) + "\n"


def write_layout(
    directory: Path,
    *,
    root: str,
    variable_lines: Iterable[str],
    note: str | None = None,
    year_suffix: str | None = None,
) -> Path:
    """Write a ``D_<ROOT>[_<YEAR>].TXT`` layout file into *directory*."""
    suffix = f"_{year_suffix}" if year_suffix else ""
    text = layout_text(
        title=f"FILE LAYOUT FOR SCHEDULE {root}",
        variable_lines=variable_lines,
        note=note,
    )
    return _write(directory / f"D_{root}{suffix}.TXT", text)


def write_data(
    directory: Path,
    *,
    root: str,
    year: int,
    month: int,
    rows: Iterable[str],
    legacy: bool = False,
    generated: str = "20260115",
) -> Path:
    """Write a schedule's raw comma-delimited data file into *directory*.

    When *legacy* is true, uses the pre-2015 ``<ROOT><MM><YY>.TXT`` naming
    (no underscore); otherwise uses the modern
    ``<ROOT>_Q<YYYYMM>_G<YYYYMMDD>.TXT`` naming.
    """
    if legacy:
        name = f"{root}{month:02d}{year % 100:02d}.TXT"
    else:
        name = f"{root}_Q{year}{month:02d}_G{generated}.TXT"
    text = "\n".join(rows) + "\n"
    return _write(directory / name, text)


def rows_of(frame: Any) -> list[dict[str, Any]]:
    """Return a native or narwhals frame's rows as plain dicts.

    Lets a test assert on values without caring which backend built the
    frame, or whether it arrived wrapped in narwhals.
    """
    wrapped = frame if isinstance(frame, nw.DataFrame) else nw.from_native(frame)
    if isinstance(wrapped, nw.LazyFrame):
        wrapped = wrapped.collect()
    return wrapped.rows(named=True)


def sorted_rows(frame: Any, *, by: list[str] | None = None) -> list[dict[str, Any]]:
    """Return a frame's rows as dicts, sorted for order-independent comparison."""
    wrapped = frame if isinstance(frame, nw.DataFrame) else nw.from_native(frame)
    if isinstance(wrapped, nw.LazyFrame):
        wrapped = wrapped.collect()
    return wrapped.sort(by if by is not None else ["UNINUM", "period"]).rows(named=True)


def is_missing(value: object) -> bool:
    """Return True for a missing value, however the active backend spells it.

    pandas represents a missing numeric value as NaN, a float, while polars
    and pyarrow use an actual ``None``. Both count as missing here.
    """
    return value is None or (isinstance(value, float) and math.isnan(value))


def as_date(value: object) -> datetime.date:
    """Return a ``period`` cell as a plain date, however the backend spells it.

    polars and pyarrow hold `period` as a native date and hand back a
    `datetime.date`. pandas has no date dtype, so it holds the same value
    as a datetime and hands back a `datetime.datetime`. Both name the same
    quarter end, so a test comparing periods normalizes through this
    rather than expecting one backend's spelling.
    """
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    raise TypeError(f"expected a date or datetime, got {type(value).__name__}")
