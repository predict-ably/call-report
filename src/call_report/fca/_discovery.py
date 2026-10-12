"""Match layout/data file pairs within an extracted FCA release directory.

FCA releases pair a ``D_<ROOT>[_<YEAR>].TXT`` layout file with a data file
sharing the same root name, but the data filename convention itself differs
by era. Modern releases (2015 onward) use
``<ROOT>_Q<YYYYMM>_G<YYYYMMDD>.TXT``, while legacy releases (2000 to 2014)
use ``<ROOT><MM><YY>.TXT`` with no separator at all.
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from call_report.exceptions import ZipArchiveUnpackingError

# The generation date a modern release stamps on each data file name, as in
# RCB_Q202603_G20260507.TXT.
_GENERATION_DATE = re.compile(r"_G(\d{8})\.TXT$", re.IGNORECASE)


@dataclass(frozen=True, kw_only=True)
class ReleaseFiles:
    """A matched layout/data file pair for one root within a release.

    Returned by `scan_release`, keyed by root name, for a caller (e.g.
    `call_report.fca.institutions` or `call_report.fca.report`) to parse.

    Attributes
    ----------
    root : str
        The shared root name, e.g. ``"RCB"``.
    layout_path : pathlib.Path
        Path to the ``D_<ROOT>[_<YEAR>].TXT`` layout file.
    data_path : pathlib.Path
        Path to the matching data file.
    """

    root: str
    layout_path: Path
    data_path: Path

    @property
    def layout_file_name(self) -> str:
        """Return `layout_path`'s bare filename.

        A convenience so callers (e.g. logging or error messages) don't
        need to reach into `layout_path` themselves.

        Returns
        -------
        str
            The layout file's name, e.g. ``"D_RCB.TXT"``.
        """
        return self.layout_path.name

    @property
    def data_file_name(self) -> str:
        """Return `data_path`'s bare filename.

        A convenience so callers (e.g. logging or error messages) don't
        need to reach into `data_path` themselves.

        Returns
        -------
        str
            The data file's name, e.g. ``"RCB_Q202603_G20260507.TXT"``.
        """
        return self.data_path.name


def scan_release(*, release_dir: Path) -> dict[str, ReleaseFiles]:
    """Scan a release directory and pair each layout file with its data file.

    A root whose layout file has no matching data file is omitted rather
    than raising. A missing pairing is treated the same as the root simply
    not being part of that release.

    Parameters
    ----------
    release_dir : pathlib.Path
        Path to one quarter's extracted release files (a flat directory of
        ``D_*.TXT`` layout files and their data files).

    Returns
    -------
    dict[str, ReleaseFiles]
        A mapping from root name to its matched file pair.
    """
    all_files = [path for path in release_dir.iterdir() if path.is_file()]
    layout_paths = [path for path in all_files if path.name.startswith("D_")]
    data_candidates = [path for path in all_files if not path.name.startswith("D_")]

    manifest: dict[str, ReleaseFiles] = {}
    for layout_path in layout_paths:
        root = _extract_root(layout_filename=layout_path.name)
        data_path = _match_data_file(root=root, candidates=data_candidates)
        if data_path is None:
            continue
        manifest[root] = ReleaseFiles(
            root=root, layout_path=layout_path, data_path=data_path
        )
    return manifest


def _extract_root(*, layout_filename: str) -> str:
    """Extract a layout file's root name.

    Some layout filenames carry an extra ``_<YEAR>`` suffix reflecting the
    layout's last revision year (e.g. ``D_RCI2B_2018.TXT``) that the data
    filename omits, so the root is everything before the first underscore.

    Parameters
    ----------
    layout_filename : str
        A ``D_<ROOT>[_<YEAR>].TXT`` filename.

    Returns
    -------
    str
        The root name, e.g. ``"RCI2B"``.
    """
    stem = layout_filename.removeprefix("D_").removesuffix(".TXT")
    return stem.split("_", 1)[0]


def _match_data_file(*, root: str, candidates: list[Path]) -> Path | None:
    """Find a root's data file among candidates, trying both eras' naming.

    The modern convention (``<ROOT>_...``) is tried first since it is
    unambiguous. The legacy convention (``<ROOT><MMYY>.TXT``, with no
    separator) is matched by exact fixed-width suffix rather than by
    prefix, because a prefix match alone cannot distinguish a root such as
    ``"RC"`` from ``"RC1"``.

    Parameters
    ----------
    root : str
        The root name to find a data file for.
    candidates : list[pathlib.Path]
        Candidate data file paths (every non-layout file in the release).

    Returns
    -------
    pathlib.Path or None
        The matching data file, or ``None`` if no candidate matches.
    """
    modern_pattern = re.compile(rf"^{re.escape(root)}_")
    for candidate in candidates:
        if modern_pattern.match(candidate.name):
            return candidate

    legacy_pattern = re.compile(rf"^{re.escape(root)}\d{{4}}$")
    for candidate in candidates:
        stem = candidate.name.removesuffix(".TXT")
        if legacy_pattern.match(stem):
            return candidate

    return None


def release_generation_date(names: Iterable[str]) -> date | None:
    """Return the date FCA generated a release, read from its file names.

    Modern releases (2015 onward) end each data file name with
    ``_G<YYYYMMDD>``, the date FCA generated the file. Legacy releases have
    no such date.

    Parameters
    ----------
    names : Iterable[str]
        The names of the files in one release, with no folder.

    Returns
    -------
    datetime.date or None
        The generation date, or ``None`` if no name carries one.

    Raises
    ------
    ValueError
        If the names carry more than one generation date.

    Examples
    --------
    >>> release_generation_date(["D_RCB.TXT", "RCB_Q202603_G20260507.TXT"])
    datetime.date(2026, 5, 7)
    >>> release_generation_date(["D_RCB.TXT", "RCB0300.TXT"]) is None
    True
    """
    stamps = {match[1] for name in names if (match := _GENERATION_DATE.search(name))}
    if len(stamps) > 1:
        raise ValueError(
            f"The release carries more than one generation date: "
            f"{', '.join(sorted(stamps))}."
        )
    if not stamps:
        return None
    return date.fromisoformat(stamps.pop())


def check_release_zip(path: Path) -> date | None:
    """Check a file is an FCA release zip, and return its generation date.

    A release zip holds at least one ``D_*.TXT`` layout file, and its data
    files carry at most one generation date. Folders inside the zip are
    ignored.

    Parameters
    ----------
    path : pathlib.Path
        The zip file to check.

    Returns
    -------
    datetime.date or None
        The date FCA generated the release, or ``None`` for a legacy
        release, which records none.

    Raises
    ------
    ZipArchiveUnpackingError
        If the file is not a zip.
    ValueError
        If the zip holds no ``D_*.TXT`` layout file, or its data files carry
        more than one generation date.

    Examples
    --------
    >>> import tempfile, zipfile
    >>> from pathlib import Path
    >>> with tempfile.TemporaryDirectory() as folder:
    ...     path = Path(folder) / "2026March.zip"
    ...     with zipfile.ZipFile(path, "w") as archive:
    ...         archive.writestr("D_RCB.TXT", "layout")
    ...         archive.writestr("RCB_Q202603_G20260507.TXT", "data")
    ...     check_release_zip(path)
    datetime.date(2026, 5, 7)
    """
    try:
        with zipfile.ZipFile(path) as archive:
            names = [Path(name).name for name in archive.namelist()]
    except zipfile.BadZipFile as error:
        raise ZipArchiveUnpackingError(f"{path.name} is not a zip file.") from error
    if not any(
        name.upper().startswith("D_") and name.upper().endswith(".TXT")
        for name in names
    ):
        raise ValueError(f"{path.name} holds no D_*.TXT layout file.")
    try:
        return release_generation_date(names)
    except ValueError as error:
        raise ValueError(f"{path.name}: {error}") from error
