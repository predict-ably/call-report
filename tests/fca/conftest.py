"""Hand-built FCA release-directory fixtures shared across tests/fca/*.

Every fixture here mimics a structural scenario seen in real FCA releases
(the three metadata scenarios, ragged and variable-length occurrence
groups, line-wrapped single_multiple_single records, legacy and modern
filenames, windows-1252 bytes), but is entirely hand-written. Nothing here
is read from ``references/``.

The layout variable-line blocks these fixtures build on live in
:mod:`tests.fca.layouts` and are re-exported here for convenience.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fca.layouts import (
    INST_LINES,
    RC_LINES_7COL,
    RC_LINES_8COL,
    RCB_LINES,
    RCR7_LINES,
)
from tests.helpers import write_data, write_layout

__all__ = [
    "INST_LINES",
    "RCB_LINES",
    "RCR7_LINES",
    "RC_LINES_7COL",
    "RC_LINES_8COL",
]


def _write_inst(
    directory: Path, *, year: int, month: int, rows: list[str], legacy: bool
) -> None:
    write_layout(directory, root="INST", variable_lines=INST_LINES)
    write_data(directory, root="INST", year=year, month=month, rows=rows, legacy=legacy)


def _write_rc(
    directory: Path,
    *,
    year: int,
    month: int,
    rows: list[str],
    lines: list[str],
    legacy: bool,
) -> None:
    write_layout(directory, root="RC", variable_lines=lines)
    write_data(directory, root="RC", year=year, month=month, rows=rows, legacy=legacy)


def _write_rcb(directory: Path, *, year: int, month: int, rows: list[str]) -> None:
    write_layout(
        directory,
        root="RCB",
        variable_lines=RCB_LINES,
        note="THE RECORD CONTAINS MULTIPLE OCCURRENCES OF THESE VARIABLES.",
    )
    write_data(directory, root="RCB", year=year, month=month, rows=rows)


def _write_rcr7(directory: Path, *, year: int, month: int, rows: list[str]) -> None:
    write_layout(directory, root="RCR7", variable_lines=RCR7_LINES)
    write_data(directory, root="RCR7", year=year, month=month, rows=rows)


@pytest.fixture
def release_2025q3(tmp_path: Path) -> Path:
    """Build a modern-naming release with a 7-column RC (no TOTLIAB yet) and no RCR7."""
    directory = tmp_path / "2025September"
    directory.mkdir()
    _write_inst(
        directory,
        year=2025,
        month=9,
        legacy=False,
        rows=[
            '6,10,0,9,2025,610000,"FCB of Texas","TX"',
            '6,20,0,9,2025,620000,"AgFirst FCB","SC"',
        ],
    )
    _write_rc(
        directory,
        year=2025,
        month=9,
        legacy=False,
        lines=RC_LINES_7COL,
        rows=["6,10,0,9,2025,610000,1000000", "6,20,0,9,2025,620000,2000000"],
    )
    _write_rcb(
        directory,
        year=2025,
        month=9,
        rows=[
            "6,10,0,9,2025,610000,10,100,1.50,20,200,2.50,30,300,3.50",
            "6,20,0,9,2025,620000,10,50,0.50,30,150,1.50",  # ragged: code 20 absent
        ],
    )
    return directory


@pytest.fixture
def release_2025q4(tmp_path: Path) -> Path:
    """Build a modern-naming release with an 8-column RC (gained TOTLIAB) and RCR7."""
    directory = tmp_path / "2025December"
    directory.mkdir()
    _write_inst(
        directory,
        year=2025,
        month=12,
        legacy=False,
        rows=[
            '6,10,0,12,2025,610000,"FCB of Texas","TX"',
            '6,20,0,12,2025,620000,"AgFirst FCB","SC"',
        ],
    )
    _write_rc(
        directory,
        year=2025,
        month=12,
        legacy=False,
        lines=RC_LINES_8COL,
        rows=[
            "6,10,0,12,2025,610000,1050000,900000",
            "6,20,0,12,2025,620000,2100000,1800000",
        ],
    )
    _write_rcb(
        directory,
        year=2025,
        month=12,
        rows=[
            "6,10,0,12,2025,610000,10,110,1.60,20,210,2.60,30,310,3.60",
            "6,20,0,12,2025,620000,10,60,0.60,20,160,1.60,30,160,1.60",
        ],
    )
    _write_rcr7(
        directory,
        year=2025,
        month=12,
        rows=[
            "6,10,0,12,2025,610000,",
            "10,100,200,",
            "20,300,400,",
            "900",
            "6,20,0,12,2025,620000,",
            "10,50,60,",  # ragged: only one code line for this institution
            "800",
        ],
    )
    return directory


@pytest.fixture
def release_2026q1(tmp_path: Path) -> Path:
    """Build a modern-naming release, one quarter after 2025Q4, with RCR7 present."""
    directory = tmp_path / "2026March"
    directory.mkdir()
    _write_inst(
        directory,
        year=2026,
        month=3,
        legacy=False,
        # "Café" exercises windows-1252 decoding of a non-ASCII institution name.
        rows=['6,10,0,3,2026,610000,"Café Ridge FCB","TX"'],
    )
    _write_rc(
        directory,
        year=2026,
        month=3,
        legacy=False,
        lines=RC_LINES_8COL,
        rows=["6,10,0,3,2026,610000,1100000,950000"],
    )
    _write_rcb(
        directory,
        year=2026,
        month=3,
        rows=["6,10,0,3,2026,610000,10,120,1.70,20,220,2.70,30,320,3.70"],
    )
    _write_rcr7(
        directory,
        year=2026,
        month=3,
        rows=["6,10,0,3,2026,610000,", "10,111,222,", "999"],
    )
    return directory


@pytest.fixture
def release_2003q1(tmp_path: Path) -> Path:
    """Build a legacy-naming (pre-2015, no underscore) release: RC and INST only."""
    directory = tmp_path / "Mar2003"
    directory.mkdir()
    _write_inst(
        directory,
        year=2003,
        month=3,
        legacy=True,
        rows=['6,10,0,3,2003,610000,"FCB of Wichita","KS"'],
    )
    _write_rc(
        directory,
        year=2003,
        month=3,
        legacy=True,
        lines=RC_LINES_7COL,
        rows=["6,10,0,3,2003,610000,500000"],
    )
    return directory


@pytest.fixture
def data_dir(
    tmp_path: Path,
    release_2025q3: Path,
    release_2025q4: Path,
    release_2026q1: Path,
    release_2003q1: Path,
) -> Path:
    """Parent directory containing every release fixture, keyed by release name."""
    return tmp_path
