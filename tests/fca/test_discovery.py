"""Tests for FCA release file-pairing discovery (call_report.fca._discovery)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from call_report.exceptions import ZipArchiveUnpackingError
from call_report.fca._discovery import (
    check_release_zip,
    release_generation_date,
    scan_release,
)
from tests.fca.layouts import RC_LINES_7COL
from tests.helpers import write_data, write_layout, write_release_zip


def test_scan_release_pairs_layout_with_data(tmp_path: Path) -> None:
    """A matched layout/data pair is discovered and keyed by its root name."""
    write_layout(tmp_path, root="RC", variable_lines=RC_LINES_7COL)
    write_data(
        tmp_path, root="RC", year=2026, month=3, rows=["6,10,0,3,2026,610000,1000000"]
    )
    manifest = scan_release(release_dir=tmp_path)
    assert set(manifest) == {"RC"}
    files = manifest["RC"]
    assert files.layout_file_name == "D_RC.TXT"
    assert files.data_file_name == "RC_Q202603_G20260115.TXT"


def test_scan_release_skips_layout_without_data(tmp_path: Path) -> None:
    """A layout file with no matching data file is silently omitted."""
    write_layout(tmp_path, root="RC", variable_lines=RC_LINES_7COL)
    # Deliberately no data file for RC.
    manifest = scan_release(release_dir=tmp_path)
    assert manifest == {}


def test_release_generation_date_reads_a_modern_release() -> None:
    """A modern release's _G<YYYYMMDD> stamp gives its generation date."""
    names = ["D_RCB.TXT", "RCB_Q202603_G20260507.TXT", "RC_Q202603_G20260507.txt"]
    assert release_generation_date(names) == date(2026, 5, 7)


def test_release_generation_date_is_none_for_a_legacy_release() -> None:
    """A legacy release's data files carry no generation date."""
    assert release_generation_date(["D_RCB.TXT", "RCB0300.TXT"]) is None


def test_release_generation_date_rejects_two_dates() -> None:
    """A release whose files carry two generation dates raises ValueError."""
    names = ["RCB_Q202603_G20260507.TXT", "RC_Q202603_G20260831.TXT"]
    with pytest.raises(ValueError, match="20260507, 20260831"):
        release_generation_date(names)


def test_check_release_zip_returns_the_generation_date(tmp_path: Path) -> None:
    """A release zip, even with its files in a folder, gives its _G date."""
    members = {"2026March/D_RCB.TXT": "x", "2026March/RCB_Q202603_G20260507.TXT": "x"}
    path = write_release_zip(tmp_path / "2026March.zip", members)
    assert check_release_zip(path) == date(2026, 5, 7)


def test_check_release_zip_is_none_for_a_legacy_release(tmp_path: Path) -> None:
    """A legacy release zip has no generation date."""
    path = write_release_zip(
        tmp_path / "Mar2000.zip", {"D_RCB.TXT": "x", "RCB0300.TXT": "x"}
    )
    assert check_release_zip(path) is None


def test_check_release_zip_rejects_a_file_that_is_not_a_zip(tmp_path: Path) -> None:
    """A file that is not a zip raises ZipArchiveUnpackingError naming it."""
    path = tmp_path / "Mar2000.zip"
    path.write_bytes(b"<html>")
    with pytest.raises(ZipArchiveUnpackingError, match=r"Mar2000\.zip"):
        check_release_zip(path)


@pytest.mark.parametrize(
    ("members", "message"),
    [
        ({"RCB0300.TXT": "x"}, "no D_"),
        (
            {"D_RCB.TXT": "x", "A_Q1_G20260101.TXT": "x", "B_Q1_G20260102.TXT": "x"},
            "more than one generation date",
        ),
    ],
)
def test_check_release_zip_rejects_a_zip_that_is_not_a_release(
    tmp_path: Path, members: dict[str, str], message: str
) -> None:
    """A zip with no layout file, or two generation dates, raises ValueError."""
    path = write_release_zip(tmp_path / "2026March.zip", members)
    with pytest.raises(ValueError, match=message):
        check_release_zip(path)
