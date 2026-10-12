"""Tests for the shared zip helpers in ``call_report.core._archive``."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from call_report.core._archive import extract_zip, sha256_of
from call_report.exceptions import CallReportError, ZipArchiveUnpackingError


def _zip(path: Path, members: dict[str, bytes]) -> Path:
    """Write a zip at `path` holding `members`, deflated."""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        for name, data in members.items():
            zipped.writestr(name, data)
    return path


def _corrupt_first_member(path: Path, data: bytes) -> None:
    """Flip bytes inside the first member's compressed data, leaving the rest."""
    raw = bytearray(path.read_bytes())
    # The local file header is 30 bytes plus the name. Corrupt the data after it.
    with zipfile.ZipFile(path) as zipped:
        info = zipped.infolist()[0]
    start = info.header_offset + 30 + len(info.filename.encode())
    for offset in range(start, start + min(8, info.compress_size)):
        raw[offset] ^= 0xFF
    path.write_bytes(bytes(raw))


def test_a_valid_zip_extracts_to_the_target(tmp_path: Path) -> None:
    """Every member lands under target_dir, nested folders included."""
    archive = _zip(
        tmp_path / "2026March.zip",
        {"D_RCB.TXT": b"layout", "sub/RCB_Q202603.TXT": b"data"},
    )
    target = tmp_path / "out" / "2026March"
    assert extract_zip(archive=archive, target_dir=target) == target
    assert (target / "D_RCB.TXT").read_bytes() == b"layout"
    assert (target / "sub" / "RCB_Q202603.TXT").read_bytes() == b"data"
    assert sorted(path.name for path in target.parent.iterdir()) == ["2026March"]


def test_a_file_that_is_not_a_zip_raises(tmp_path: Path) -> None:
    """A file that is not a zip raises ZipArchiveUnpackingError, and writes nothing."""
    archive = tmp_path / "page.zip"
    archive.write_bytes(b"<html>Just a moment...</html>")
    target = tmp_path / "out"
    with pytest.raises(ZipArchiveUnpackingError, match="not a zip file"):
        extract_zip(archive=archive, target_dir=target)
    assert not target.exists()


def test_a_zip_with_a_corrupt_member_raises(tmp_path: Path) -> None:
    """A member whose data is damaged raises, and leaves no target folder."""
    data = b"value," * 2000
    archive = _zip(tmp_path / "a.zip", {"D_RCB.TXT": data})
    _corrupt_first_member(archive, data)
    target = tmp_path / "out"
    with pytest.raises(
        ZipArchiveUnpackingError, match=r"could not be unpacked|corrupt"
    ):
        extract_zip(archive=archive, target_dir=target)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["a.zip"]


def test_a_member_that_fails_its_crc_check_raises(tmp_path: Path) -> None:
    """A stored member whose bytes changed fails testzip, and nothing is written."""
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as zipped:
        zipped.writestr("D_RCB.TXT", b"original layout")
    raw = archive.read_bytes().replace(b"original layout", b"tampered layout")
    archive.write_bytes(raw)
    target = tmp_path / "out"
    with pytest.raises(ZipArchiveUnpackingError, match=r"corrupt member: 'D_RCB\.TXT'"):
        extract_zip(archive=archive, target_dir=target)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["a.zip"]


def test_a_zip_with_a_broken_directory_raises(tmp_path: Path) -> None:
    """A zip whose central directory is damaged raises ZipArchiveUnpackingError."""
    archive = _zip(tmp_path / "a.zip", {"D_RCB.TXT": b"layout"})
    raw = archive.read_bytes().replace(b"PK\x01\x02", b"XX\x01\x02")
    archive.write_bytes(raw)
    target = tmp_path / "out"
    with pytest.raises(ZipArchiveUnpackingError, match="could not be unpacked"):
        extract_zip(archive=archive, target_dir=target)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["a.zip"]


@pytest.mark.parametrize(
    "name",
    [
        "../evil.txt",
        "/etc/evil.txt",
        "nested/../../evil.txt",
        "..\\evil.txt",
        "C:\\evil.txt",
        "\\\\server\\share\\evil.txt",
    ],
)
def test_a_member_outside_the_target_raises(tmp_path: Path, name: str) -> None:
    """A member with an absolute path or a ``..`` is refused, and nothing is written."""
    archive = _zip(tmp_path / "a.zip", {"D_RCB.TXT": b"layout", name: b"evil"})
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(ZipArchiveUnpackingError, match="outside its folder"):
        extract_zip(archive=archive, target_dir=work / "out")
    assert list(work.iterdir()) == []
    assert not (tmp_path / "evil.txt").exists()


def test_an_existing_target_is_not_overwritten(tmp_path: Path) -> None:
    """Extraction never writes into a folder that already exists."""
    archive = _zip(tmp_path / "a.zip", {"D_RCB.TXT": b"layout"})
    target = tmp_path / "out"
    target.mkdir()
    (target / "keep.txt").write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        extract_zip(archive=archive, target_dir=target)
    assert sorted(path.name for path in target.iterdir()) == ["keep.txt"]


def test_a_failure_while_writing_removes_the_partial_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error that is not about the zip itself still leaves nothing behind."""
    archive = _zip(tmp_path / "a.zip", {"D_RCB.TXT": b"layout"})

    def fail(self: zipfile.ZipFile, path: object = None, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(zipfile.ZipFile, "extractall", fail)
    with pytest.raises(OSError, match="disk full"):
        extract_zip(archive=archive, target_dir=tmp_path / "out")
    assert sorted(path.name for path in tmp_path.iterdir()) == ["a.zip"]


def test_zip_archive_unpacking_error_is_a_call_report_error() -> None:
    """ZipArchiveUnpackingError is caught by ``except CallReportError``."""
    assert issubclass(ZipArchiveUnpackingError, CallReportError)


def test_sha256_of_matches_hashlib(tmp_path: Path) -> None:
    """sha256_of gives the same digest as hashlib over the whole file."""
    path = tmp_path / "data.bin"
    data = bytes(range(256)) * 1000
    path.write_bytes(data)
    assert sha256_of(path) == hashlib.sha256(data).hexdigest()


def test_the_archive_module_does_not_import_the_http_module() -> None:
    """Unpacking a zip does not load the HTTP helpers, so each stands alone."""
    code = (
        "import sys, call_report.core._archive; "
        "print('call_report.core._http' in sys.modules)"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False"
