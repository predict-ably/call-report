"""Tests for the FCA institution-roster reader (call_report.fca.institutions)."""

from __future__ import annotations

from pathlib import Path

import narwhals as nw
import pytest

from call_report.exceptions import DownloadError
from call_report.fca.institutions import read_institutions
from tests.conftest import write_data, write_layout
from tests.fca.conftest import INST_LINES, RC_LINES_7COL


def test_read_institutions_returns_roster(tmp_path: Path) -> None:
    """The institution roster includes every institution with its roster fields."""
    write_layout(tmp_path, root="INST", variable_lines=INST_LINES)
    write_data(
        tmp_path,
        root="INST",
        year=2026,
        month=3,
        rows=[
            '6,10,0,3,2026,610000,"Café Ridge FCB","TX"',
            '6,20,0,3,2026,620000,"AgFirst FCB","SC"',
        ],
    )
    result = read_institutions(release_dir=tmp_path)
    rows = nw.from_native(result).rows(named=True)
    assert len(rows) == 2
    by_uninum = {r["UNINUM"]: r for r in rows}
    # windows-1252 decoding must round-trip non-ASCII institution names correctly.
    assert by_uninum[610000]["SHORTNAME"] == "Café Ridge FCB"
    assert by_uninum[620000]["STATE"] == "SC"


def test_read_institutions_legacy_naming(tmp_path: Path) -> None:
    """The legacy (pre-2015, no underscore) INST filename convention is supported."""
    write_layout(tmp_path, root="INST", variable_lines=INST_LINES)
    write_data(
        tmp_path,
        root="INST",
        year=2003,
        month=3,
        legacy=True,
        rows=['6,10,0,3,2003,610000,"FCB of Wichita","KS"'],
    )
    result = read_institutions(release_dir=tmp_path)
    rows = nw.from_native(result).rows(named=True)
    assert rows[0]["UNINUM"] == 610000
    assert rows[0]["SHORTNAME"] == "FCB of Wichita"


def test_read_institutions_is_keyword_only(tmp_path: Path) -> None:
    """read_institutions takes no positional arguments."""
    write_layout(tmp_path, root="INST", variable_lines=INST_LINES)
    write_data(
        tmp_path,
        root="INST",
        year=2026,
        month=3,
        rows=['6,10,0,3,2026,610000,"X","TX"'],
    )
    with pytest.raises(TypeError):
        read_institutions(tmp_path)  # type: ignore[call-arg]


def test_read_institutions_missing_pair_raises(tmp_path: Path) -> None:
    """A release with no INST layout/data pair raises DownloadError."""
    # Write an unrelated RC pair but no INST files.
    write_layout(tmp_path, root="RC", variable_lines=RC_LINES_7COL)
    write_data(
        tmp_path, root="RC", year=2026, month=3, rows=["6,10,0,3,2026,610000,1000000"]
    )
    with pytest.raises(DownloadError, match="INST"):
        read_institutions(release_dir=tmp_path)
