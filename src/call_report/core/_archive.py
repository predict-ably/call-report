"""Unpack zip archives and checksum files and bytes, using the standard library only.

These helpers are shared by every call report source. They work on files
already on disk, so this module does not import the HTTP helpers in
``call_report.core._http``, and each can be used and tested alone.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import zipfile
import zlib
from pathlib import Path, PureWindowsPath

from call_report.exceptions import ZipArchiveUnpackingError


def extract_zip(*, archive: Path, target_dir: Path) -> Path:
    """Extract every member of a zip archive into a new folder.

    The archive is checked before anything is written. A file that is not a
    zip, a zip with a corrupt member, or a member whose path is absolute or
    contains a ``..`` part raises an error. Members are
    extracted into a temporary folder beside `target_dir`, which is renamed
    to `target_dir` only once every member is written. So a folder at
    `target_dir` always holds a complete extraction, and a failed
    extraction leaves nothing behind.

    Parameters
    ----------
    archive : pathlib.Path
        The zip file to extract.
    target_dir : pathlib.Path
        The folder to extract into. It must not exist yet. Its parent
        folder is created if it is missing.

    Returns
    -------
    pathlib.Path
        `target_dir`, now holding the archive's members.

    Raises
    ------
    ZipArchiveUnpackingError
        If `archive` is not a zip file, has a corrupt member, or has a
        member whose path is absolute or contains ``..``.
    FileExistsError
        If `target_dir` already exists.

    Examples
    --------
    >>> import tempfile
    >>> import zipfile
    >>> from pathlib import Path
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     archive = Path(tmp) / "2026March.zip"
    ...     with zipfile.ZipFile(archive, "w") as zipped:
    ...         zipped.writestr("D_RCB.TXT", "layout")
    ...     extracted = extract_zip(archive=archive, target_dir=Path(tmp) / "2026March")
    ...     sorted(path.name for path in extracted.iterdir())
    ['D_RCB.TXT']
    """
    target_dir = Path(target_dir)
    if target_dir.exists():
        raise FileExistsError(f"Cannot extract {archive} into {target_dir}: it exists.")
    if not zipfile.is_zipfile(archive):
        raise ZipArchiveUnpackingError(f"{archive} is not a zip file.")

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{target_dir.name}.", dir=target_dir.parent)
    )
    try:
        with zipfile.ZipFile(archive) as zipped:
            _check_members(zipped, archive=archive)
            zipped.extractall(staging)
        staging.rename(target_dir)
    except (zipfile.BadZipFile, zlib.error, EOFError) as error:
        shutil.rmtree(staging, ignore_errors=True)
        raise ZipArchiveUnpackingError(
            f"{archive} could not be unpacked: {error}"
        ) from error
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target_dir


def _check_members(zipped: zipfile.ZipFile, *, archive: Path) -> None:
    """Raise if any member of an open zip is corrupt or would escape its folder.

    Every member's path is checked before any member is read, then
    `zipfile.ZipFile.testzip` reads each member and checks its CRC.

    Parameters
    ----------
    zipped : zipfile.ZipFile
        The open archive to check.
    archive : pathlib.Path
        The archive's path, used in error messages.

    Raises
    ------
    ZipArchiveUnpackingError
        If a member's path is absolute or contains ``..``, or a member fails
        its CRC check.
    """
    for name in zipped.namelist():
        # A Windows path splits on both "/" and "\\", so a backslash cannot
        # hide a ".." or an absolute path from this check on any platform.
        member = PureWindowsPath(name)
        if member.anchor or ".." in member.parts:
            raise ZipArchiveUnpackingError(
                f"{archive} has a member outside its folder: {name!r}."
            )
    corrupt = zipped.testzip()
    if corrupt is not None:
        raise ZipArchiveUnpackingError(f"{archive} has a corrupt member: {corrupt!r}.")


def sha256_of(path: Path) -> str:
    """Return the SHA-256 digest of a file's contents.

    The file is read in pieces, so a large file is never held in memory
    whole.

    Parameters
    ----------
    path : pathlib.Path
        The file to hash.

    Returns
    -------
    str
        The digest as 64 lowercase hexadecimal characters.

    Examples
    --------
    >>> import tempfile
    >>> from pathlib import Path
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     path = Path(tmp) / "hello.txt"
    ...     _ = path.write_bytes(b"hello")
    ...     sha256_of(path)
    '2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824'
    """
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def sha256_of_bytes(data: bytes) -> str:
    """Return the SHA-256 digest of some bytes.

    This is the digest `sha256_of` gives for a file holding the same bytes.

    Parameters
    ----------
    data : bytes
        The bytes to hash.

    Returns
    -------
    str
        The digest as 64 lowercase hexadecimal characters.

    Examples
    --------
    >>> sha256_of_bytes(b"hello")
    '2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824'
    """
    return hashlib.sha256(data).hexdigest()
