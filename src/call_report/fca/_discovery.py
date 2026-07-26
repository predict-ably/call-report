"""Match layout/data file pairs within an extracted FCA release directory.

FCA releases pair a ``D_<ROOT>[_<YEAR>].TXT`` layout file with a data file
sharing the same root name, but the data filename convention itself
differs by era: modern releases (2015+) use
``<ROOT>_Q<YYYYMM>_G<YYYYMMDD>.TXT``; legacy releases (2000-2014) use
``<ROOT><MM><YY>.TXT`` with no separator at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


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

    Roots whose layout file has no matching data file are silently omitted
    -- this has not been observed in any real FCA release, but a missing
    pairing is treated the same as the root simply not being part of that
    release rather than as an error.

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
    unambiguous; the legacy convention (``<ROOT><MMYY>.TXT``, no separator)
    is matched by exact fixed-width suffix rather than by prefix, since a
    prefix match alone cannot distinguish e.g. root ``"RC"`` from ``"RC1"``.

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
