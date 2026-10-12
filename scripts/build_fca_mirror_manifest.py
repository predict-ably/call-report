"""Build the manifest and metadata bundle for the GitHub mirror of FCA releases.

A maintainer tool, not part of the package or its runtime. The
``fca-mirror`` workflow runs it whenever the checked-in archive or the
shipped metadata changes, and publishes what it writes as assets of the
mirror's GitHub releases.

It reads every release zip in ``data/fca-call-report/`` and the shipped
schedule metadata and institution registry in
``src/call_report/fca/data/``, and writes three files to the output folder.

- ``manifest.json`` lists every release zip with its period, SHA-256, size,
  generation date, and FCA URL. It names the snapshot being published and
  every earlier snapshot. ``call_report.fca.catalog.FCAReleaseCatalog``
  reads it, and the script reads its own output back the same way before
  it finishes.
- ``metadata.zip`` holds the shipped schedule metadata and institution
  registry, in the layout the wheel holds them, plus ``metadata.json``
  giving the newest quarter covered. Its bytes depend only on the files it
  holds, so building it twice from the same files gives the same zip.
- ``changes.json`` compares the new manifest with the previous one. It
  lists the files the rolling release needs uploaded or removed, and says
  whether anything changed at all.

The script fails if a zip's name is not the exact name FCA gives its
quarter, if a zip has no ``D_*.TXT`` layout file, if one zip's data files
carry more than one generation date, or if the shipped metadata does not
reach the newest archived quarter.

Notes
-----
Build the files for a snapshot into a folder named ``out``::

    python scripts/build_fca_mirror_manifest.py --snapshot 2026-10-12 --out-dir out

Add ``--previous-manifest <path>`` to compare with the rolling release's
current manifest. Without it, every file counts as new.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from call_report.core import FileMetadata, ReportingPeriod
from call_report.core._archive import sha256_of
from call_report.exceptions import ZipArchiveUnpackingError
from call_report.fca import FCAInstitutionRegistry
from call_report.fca._discovery import check_release_zip
from call_report.fca.catalog import (
    _MANIFEST_SCHEMA_VERSION,
    FCAReleaseCatalog,
    _is_snapshot,
    construct_fca_download_url,
    fca_release_stem,
    parse_fca_release_filename,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ARCHIVE_DIR = _REPO_ROOT / "data" / "fca-call-report"
_METADATA_DIR = _REPO_ROOT / "src" / "call_report" / "fca" / "data"

MANIFEST_NAME = "manifest.json"
METADATA_NAME = "metadata.zip"
CHANGES_NAME = "changes.json"

# The format of metadata.zip. It changes only when the bundle's layout does.
METADATA_SCHEMA_VERSION = 1

# Every member of metadata.zip gets this timestamp and these permissions, so
# the zip's bytes depend only on the files it holds.
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_ZIP_FILE_MODE = 0o644 << 16
_ZIP_UNIX = 3

_log = logging.getLogger("build_fca_mirror_manifest")


class MirrorBuildError(Exception):
    """The archive or the shipped metadata cannot be published as they are.

    main logs the message and exits 1, so the workflow publishes nothing.
    """


@dataclass(frozen=True, kw_only=True)
class ReleaseEntry:
    """One release zip, as the manifest lists it.

    `read_release` builds one for each zip in the archive.

    Attributes
    ----------
    period : ReportingPeriod
        The quarter the zip holds.
    file : str
        The zip's name, such as ``"2026March.zip"``.
    sha256 : str
        The zip's SHA-256, as lower case hex.
    size : int
        The zip's size in bytes.
    generated : datetime.date or None
        The date FCA generated the release, or ``None`` for a legacy
        release, which records none.
    """

    period: ReportingPeriod
    file: str
    sha256: str
    size: int
    generated: date | None

    def to_json(self) -> dict[str, Any]:
        """Return the entry as the manifest writes it.

        The FCA URL is built from the period, so it always names FCA's own file.

        Returns
        -------
        dict[str, Any]
            The entry's manifest fields, with its FCA URL added.
        """
        return {
            "period": self.period.period_end.isoformat(),
            "file": self.file,
            "sha256": self.sha256,
            "size": self.size,
            "generated": None if self.generated is None else self.generated.isoformat(),
            "source_url": construct_fca_download_url(period=self.period),
        }


def read_release(path: Path) -> ReleaseEntry:
    """Read one release zip's manifest entry.

    The zip is checked before it is listed, so the mirror never publishes a file the
    package cannot read.

    Parameters
    ----------
    path : pathlib.Path
        The release zip.

    Returns
    -------
    ReleaseEntry
        The zip's period, checksum, size, and generation date.

    Raises
    ------
    MirrorBuildError
        If the zip's name is not the exact name FCA gives its quarter, the
        file is not a zip, it has no ``D_*.TXT`` layout file, or its data
        files carry more than one generation date.
    """
    period = parse_fca_release_filename(path.name)
    if period is None or path.name != f"{fca_release_stem(period=period)}.zip":
        raise MirrorBuildError(
            f"{path.name} is not the name of an FCA release zip. Each zip must be "
            "named exactly as FCA names it, such as 2026March.zip or Sept2014.zip."
        )
    try:
        generated = check_release_zip(path)
    except (ZipArchiveUnpackingError, ValueError) as error:
        raise MirrorBuildError(str(error)) from error
    return ReleaseEntry(
        period=period,
        file=path.name,
        sha256=sha256_of(path),
        size=path.stat().st_size,
        generated=generated,
    )


def read_archive(archive_dir: Path) -> list[ReleaseEntry]:
    """Read every release zip in a folder, oldest quarter first.

    Other files in the folder are ignored.

    Parameters
    ----------
    archive_dir : pathlib.Path
        The folder of release zips, such as ``data/fca-call-report/``.

    Returns
    -------
    list[ReleaseEntry]
        One entry per zip.

    Raises
    ------
    MirrorBuildError
        If the folder holds no zip, or any zip fails `read_release`.
    """
    # iterdir and a lower-cased suffix, not glob("*.zip"), which matches
    # letter case differently on Windows and Linux.
    paths = [path for path in archive_dir.iterdir() if path.suffix.lower() == ".zip"]
    if not paths:
        raise MirrorBuildError(f"{archive_dir} holds no release zips.")
    return sorted((read_release(path) for path in paths), key=lambda e: e.period)


def metadata_through(metadata_dir: Path) -> ReportingPeriod:
    """Return the newest quarter the shipped metadata covers.

    That is the earlier of the newest quarter in any schedule's metadata and
    the newest quarter in the institution registry.

    Parameters
    ----------
    metadata_dir : pathlib.Path
        The folder holding ``schedules/`` and ``institutions/registry.json``.

    Returns
    -------
    ReportingPeriod
        The newest quarter both cover.

    Raises
    ------
    MirrorBuildError
        If the folder has no schedule metadata.
    """
    schedules = sorted((metadata_dir / "schedules").glob("*.json"))
    if not schedules:
        raise MirrorBuildError(f"{metadata_dir / 'schedules'} holds no metadata.")
    newest_schedule = max(
        FileMetadata.from_json(text=path.read_text(encoding="utf-8")).last_period
        for path in schedules
    )
    registry = FCAInstitutionRegistry.from_json(
        text=(metadata_dir / "institutions" / "registry.json").read_text(
            encoding="utf-8"
        )
    )
    return min(newest_schedule, registry.last_period)


def build_metadata_zip(metadata_dir: Path, *, through: ReportingPeriod) -> bytes:
    """Return the metadata bundle's bytes.

    The bundle holds ``schedules/<root>.json``, ``institutions/registry.json``,
    and ``metadata.json``. Members are stored uncompressed, in name order,
    with a fixed timestamp, so the same files always give the same bytes.

    Parameters
    ----------
    metadata_dir : pathlib.Path
        The folder holding ``schedules/`` and ``institutions/registry.json``.
    through : ReportingPeriod
        The newest quarter the metadata covers.

    Returns
    -------
    bytes
        The zip file's contents.
    """
    members: dict[str, bytes] = {
        f"schedules/{path.name}": path.read_bytes()
        for path in (metadata_dir / "schedules").glob("*.json")
    }
    members["institutions/registry.json"] = (
        metadata_dir / "institutions" / "registry.json"
    ).read_bytes()
    members["metadata.json"] = (
        json.dumps(
            {
                "schema_version": METADATA_SCHEMA_VERSION,
                "through": through.period_end.isoformat(),
            },
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as bundle:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIMESTAMP)
            info.create_system = _ZIP_UNIX
            info.external_attr = _ZIP_FILE_MODE
            bundle.writestr(info, members[name])
    return buffer.getvalue()


def build_manifest(
    *,
    releases: list[ReleaseEntry],
    metadata: bytes,
    through: ReportingPeriod,
    snapshot: str,
    previous_snapshots: list[str],
) -> dict[str, Any]:
    """Return the manifest for one snapshot.

    The same manifest is published to the snapshot release and the rolling release.

    Parameters
    ----------
    releases : list[ReleaseEntry]
        Every release zip, oldest first.
    metadata : bytes
        The metadata bundle's bytes.
    through : ReportingPeriod
        The newest quarter the metadata covers.
    snapshot : str
        The snapshot being published, such as ``"2026-10-12"``.
    previous_snapshots : list[str]
        The snapshots already published, oldest first.

    Returns
    -------
    dict[str, Any]
        The manifest, ready to write as JSON.
    """
    return {
        "schema_version": _MANIFEST_SCHEMA_VERSION,
        "snapshot": snapshot,
        "snapshots": [*previous_snapshots, snapshot],
        "releases": [release.to_json() for release in releases],
        "metadata": {
            "file": METADATA_NAME,
            "sha256": _sha256_of_bytes(metadata),
            "size": len(metadata),
            "through": through.period_end.isoformat(),
            "schema_version": METADATA_SCHEMA_VERSION,
        },
    }


def compare_manifests(
    current: Mapping[str, Any], previous: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Say which files the rolling release needs uploaded or removed.

    Files are compared by SHA-256, so an unchanged file is never uploaded again.

    Parameters
    ----------
    current : Mapping[str, Any]
        The manifest just built.
    previous : Mapping[str, Any] or None
        The rolling release's current manifest, or ``None`` if there is
        none yet.

    Returns
    -------
    dict[str, Any]
        ``changed`` is true when any file differs. ``upload`` lists the zips
        that are new or whose SHA-256 differs, and ``metadata.zip`` when the
        bundle differs. ``remove`` lists the zips the previous manifest has
        and the new one does not.
    """
    old = _checksums(previous) if previous is not None else {}
    new = _checksums(current)
    upload = sorted(name for name, checksum in new.items() if old.get(name) != checksum)
    remove = sorted(set(old) - set(new))
    return {"changed": bool(upload or remove), "upload": upload, "remove": remove}


def _checksums(manifest: Mapping[str, Any]) -> dict[str, str]:
    """Return every file a manifest lists, with its SHA-256.

    The metadata bundle is included when the manifest has one.

    Parameters
    ----------
    manifest : Mapping[str, Any]
        A manifest.

    Returns
    -------
    dict[str, str]
        Each release zip's and the metadata bundle's checksum, keyed by
        file name.
    """
    checksums = {entry["file"]: entry["sha256"] for entry in manifest["releases"]}
    if "metadata" in manifest:
        checksums[manifest["metadata"]["file"]] = manifest["metadata"]["sha256"]
    return checksums


def _sha256_of_bytes(data: bytes) -> str:
    """Return the SHA-256 of some bytes, as lower case hex.

    This is the form the manifest records.

    Parameters
    ----------
    data : bytes
        The bytes to hash.

    Returns
    -------
    str
        The checksum.
    """
    return hashlib.sha256(data).hexdigest()


def build(
    *,
    snapshot: str,
    out_dir: Path,
    archive_dir: Path = _ARCHIVE_DIR,
    metadata_dir: Path = _METADATA_DIR,
    previous_manifest: Path | None = None,
) -> dict[str, Any]:
    """Write the manifest, the metadata bundle, and the comparison.

    Nothing is written unless every check passes.

    Parameters
    ----------
    snapshot : str
        The snapshot being published, such as ``"2026-10-12"``.
    out_dir : pathlib.Path
        The folder to write ``manifest.json``, ``metadata.zip``, and
        ``changes.json`` to. It is created if missing.
    archive_dir : pathlib.Path, optional
        The folder of release zips. Defaults to ``data/fca-call-report/``.
    metadata_dir : pathlib.Path, optional
        The shipped metadata folder. Defaults to
        ``src/call_report/fca/data/``.
    previous_manifest : pathlib.Path, optional
        The rolling release's current manifest. Without it, every file
        counts as new.

    Returns
    -------
    dict[str, Any]
        The comparison written to ``changes.json``.

    Raises
    ------
    MirrorBuildError
        If `snapshot` is not a snapshot date or is already published, a zip
        cannot be published, or the metadata does not reach the newest
        archived quarter.
    """
    if not _is_snapshot(snapshot):
        raise MirrorBuildError(
            f"--snapshot must be a date such as 2026-10-12, not {snapshot!r}."
        )
    previous = (
        json.loads(previous_manifest.read_text(encoding="utf-8"))
        if previous_manifest is not None
        else None
    )
    previous_snapshots: list[str] = list(previous["snapshots"]) if previous else []
    if snapshot in previous_snapshots:
        raise MirrorBuildError(f"Snapshot {snapshot} is already published.")

    releases = read_archive(archive_dir)
    through = metadata_through(metadata_dir)
    newest = releases[-1].period
    if through < newest:
        raise MirrorBuildError(
            f"The shipped metadata covers quarters through {through.label}, but the "
            f"archive holds {newest.label}. Run scripts/"
            "generate_fca_schedule_metadata.py and scripts/"
            "generate_fca_institution_registry.py first."
        )
    metadata = build_metadata_zip(metadata_dir, through=through)
    manifest = build_manifest(
        releases=releases,
        metadata=metadata,
        through=through,
        snapshot=snapshot,
        previous_snapshots=previous_snapshots,
    )
    manifest_text = json.dumps(manifest, indent=2) + "\n"
    # Read the manifest back the way the package does, so a manifest the
    # package cannot read is never published.
    FCAReleaseCatalog.from_manifest_json(manifest_text)
    changes = compare_manifests(manifest, previous)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / MANIFEST_NAME).write_text(manifest_text, encoding="utf-8", newline="\n")
    (out_dir / METADATA_NAME).write_bytes(metadata)
    (out_dir / CHANGES_NAME).write_text(
        json.dumps(changes, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    _log.info(
        "Wrote the %s manifest: %d releases, %s to %s, metadata through %s.",
        snapshot,
        len(releases),
        releases[0].period.label,
        newest.label,
        through.label,
    )
    _log.info(
        "%d file(s) to upload, %d to remove.",
        len(changes["upload"]),
        len(changes["remove"]),
    )
    return changes


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse this script's command-line arguments.

    A thin wrapper around `argparse`, so `main` stays testable.

    Parameters
    ----------
    argv : list[str], optional
        Arguments to parse. Defaults to ``sys.argv[1:]``.

    Returns
    -------
    argparse.Namespace
        The parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--snapshot",
        required=True,
        help="The snapshot being published, such as 2026-10-12.",
    )
    parser.add_argument(
        "--out-dir", required=True, type=Path, help="The folder to write to."
    )
    parser.add_argument(
        "--previous-manifest",
        type=Path,
        help="The rolling release's current manifest.json, if there is one.",
    )
    parser.add_argument(
        "--archive-dir",
        type=Path,
        default=_ARCHIVE_DIR,
        help="The folder of release zips. Defaults to data/fca-call-report.",
    )
    parser.add_argument(
        "--metadata-dir",
        type=Path,
        default=_METADATA_DIR,
        help="The shipped metadata folder. Defaults to src/call_report/fca/data.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Build the mirror's files.

    See the module docstring for what is written and when the build fails.

    Parameters
    ----------
    argv : list[str], optional
        Arguments to parse. Defaults to ``sys.argv[1:]``.

    Returns
    -------
    int
        ``0`` on success, ``1`` if the files cannot be built.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parse_args(argv)
    try:
        build(
            snapshot=args.snapshot,
            out_dir=args.out_dir,
            archive_dir=args.archive_dir,
            metadata_dir=args.metadata_dir,
            previous_manifest=args.previous_manifest,
        )
    except MirrorBuildError as error:
        _log.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
