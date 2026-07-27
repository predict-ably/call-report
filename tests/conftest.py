"""Shared, hand-built fixture-file writers for the call_report test suite.

Every helper here writes small windows-1252-encoded files that mimic the
*structural* quirks of real FCA Call Report releases. The suite stays hermetic with
no file stored in tests and no tests of file reading/parsing functionality
requiring the data to be read from the network.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

ENCODING = "windows-1252"


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
