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
import functools
import importlib.util
import math
import sys
import zipfile
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any

import narwhals as nw

from call_report.core import ReportingPeriod
from call_report.fca.catalog import fca_release_stem, parse_fca_release_filename

ENCODING = "windows-1252"

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
_ARCHIVE_ROOT = Path(__file__).resolve().parents[1] / "data" / "fca-call-report"


def _load_script(name: str) -> ModuleType:
    """Load one maintainer script from ``scripts/`` as a module.

    ``scripts/`` is not a Python package, so a script is loaded from its
    file path rather than imported. It is registered in `sys.modules`
    before execution, because `dataclasses.dataclass` resolves postponed
    annotations by looking the defining module up there, which a bare
    ``module_from_spec``/``exec_module`` never populates on its own.

    Parameters
    ----------
    name : str
        The script's file name without ``.py``.

    Returns
    -------
    types.ModuleType
        The executed script module.
    """
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS_DIR / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@functools.cache
def load_generation_script() -> ModuleType:
    """Load the FCA schedule metadata generation script as a module.

    Cached, so every caller shares one module object rather than
    re-executing the script and replacing the `sys.modules` entry.

    Returns
    -------
    types.ModuleType
        The executed ``generate_fca_schedule_metadata`` script.
    """
    return _load_script("generate_fca_schedule_metadata")


@functools.cache
def load_institution_registry_script() -> ModuleType:
    """Load the FCA institution registry generation script as a module.

    Cached for the same reason as `load_generation_script`.

    Returns
    -------
    types.ModuleType
        The executed ``generate_fca_institution_registry`` script.
    """
    return _load_script("generate_fca_institution_registry")


@functools.cache
def load_mirror_manifest_script() -> ModuleType:
    """Load the GitHub mirror manifest script as a module.

    Cached for the same reason as `load_generation_script`.

    Returns
    -------
    types.ModuleType
        The executed ``build_fca_mirror_manifest`` script.
    """
    return _load_script("build_fca_mirror_manifest")


@functools.cache
def load_drift_script() -> ModuleType:
    """Load the FCA drift check script as a module.

    Cached for the same reason as `load_generation_script`.

    Returns
    -------
    types.ModuleType
        The executed ``check_fca_drift`` script.
    """
    return _load_script("check_fca_drift")


def write_release_zip(path: Path, members: dict[str, str] | None = None) -> Path:
    """Write a small release zip, by default holding one layout and one data file.

    Parameters
    ----------
    path : pathlib.Path
        The zip to write. Its name decides the quarter it stands for.
    members : dict[str, str], optional
        The files to put in it, by name. Defaults to a ``D_RCB.TXT`` layout
        and a matching modern data file.

    Returns
    -------
    pathlib.Path
        `path`.
    """
    if members is None:
        members = {"D_RCB.TXT": "layout\n", "RCB_Q202603_G20260507.TXT": "1,2\n"}
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in members.items():
            archive.writestr(name, text)
    return path


ALL_BACKENDS = ("pandas", "polars", "pyarrow")
"""The dataframe backends every cross-backend test runs against."""


@functools.cache
def archived_periods() -> tuple[ReportingPeriod, ...]:
    """Return every quarter with a release zip in ``data/fca-call-report/``.

    The newest zip decides the newest quarter, so adding a quarter's zip
    needs no constant changed anywhere. The zip names are read directly,
    so the answer does not depend on today's date, which some tests
    replace. Cached, since every caller asks about the same checked-in
    folder.

    Returns
    -------
    tuple[ReportingPeriod, ...]
        The archived quarters, oldest first.
    """
    periods = (
        parse_fca_release_filename(path.name) for path in _ARCHIVE_ROOT.glob("*.zip")
    )
    return tuple(sorted(period for period in periods if period is not None))


def misnamed_release_zips(folder: Path) -> list[str]:
    """Return the zips in a folder that name a release but not exactly.

    `parse_fca_release_filename` ignores letter case and reads ``Sep`` as
    ``Sept``, but the transports look for the exact name
    `fca_release_stem` gives. So ``mar2000.zip`` names 2000Q1, yet on a
    case-sensitive file system the transport cannot find it.

    Parameters
    ----------
    folder : pathlib.Path
        The folder of release zips to check.

    Returns
    -------
    list[str]
        The names that parse to a period but differ from
        ``fca_release_stem(period=...) + ".zip"``, sorted.
    """
    # iterdir and a lower-cased suffix, not glob("*.zip"), which matches
    # letter case differently on Windows and Linux.
    return sorted(
        path.name
        for path in folder.iterdir()
        if path.suffix.lower() == ".zip"
        and (period := parse_fca_release_filename(path.name)) is not None
        and path.name != f"{fca_release_stem(period=period)}.zip"
    )


def newest_archived_period() -> ReportingPeriod:
    """Return the newest quarter with a release zip in ``data/fca-call-report/``.

    Returns
    -------
    ReportingPeriod
        The newest archived quarter.
    """
    return archived_periods()[-1]


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
