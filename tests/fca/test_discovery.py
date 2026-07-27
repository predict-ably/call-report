"""Tests for FCA release file-pairing discovery (call_report.fca._discovery)."""

from __future__ import annotations

from pathlib import Path

from call_report.fca._discovery import scan_release
from tests.conftest import write_data, write_layout
from tests.fca.conftest import RC_LINES_7COL


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
