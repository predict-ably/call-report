"""Tests for the GitHub mirror manifest script (scripts/build_fca_mirror_manifest.py).

The script is loaded from its file path, as the other script tests do. The
real archive and the shipped metadata are read directly. They are small
enough that building the whole manifest takes well under a second.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import zipfile
from pathlib import Path
from typing import Any

import pytest

from call_report.core import ReportingPeriod
from call_report.fca import FCAInstitutionRegistry
from call_report.fca.catalog import FCAReleaseCatalog, construct_fca_download_url
from tests.fca.institution_rows import ROW
from tests.helpers import (
    archived_periods,
    load_mirror_manifest_script,
    newest_archived_period,
    write_release_zip,
)

build_script = load_mirror_manifest_script()

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ARCHIVE = _REPO_ROOT / "data" / "fca-call-report"
_METADATA = _REPO_ROOT / "src" / "call_report" / "fca" / "data"


def _build(out_dir: Path, **kwargs: Any) -> dict[str, Any]:
    """Build the mirror's files for snapshot 2026-10-12 and return the changes."""
    changes: dict[str, Any] = build_script.build(
        snapshot=kwargs.pop("snapshot", "2026-10-12"), out_dir=out_dir, **kwargs
    )
    return changes


def _manifest(out_dir: Path) -> dict[str, Any]:
    """Read the manifest a build wrote."""
    manifest: dict[str, Any] = json.loads((out_dir / "manifest.json").read_text())
    return manifest


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the mirror's files once from the real archive and shipped metadata."""
    out_dir = tmp_path_factory.mktemp("mirror")
    _build(out_dir)
    return out_dir


# ---------------------------------------------------------------------------
# The real archive
# ---------------------------------------------------------------------------


def test_manifest_lists_every_archived_quarter(built: Path) -> None:
    """Each archived zip is listed once, oldest first, by its own name."""
    releases = _manifest(built)["releases"]
    periods = tuple(
        ReportingPeriod.from_period_end(value=entry["period"]) for entry in releases
    )
    assert periods == archived_periods()
    assert all((_ARCHIVE / entry["file"]).is_file() for entry in releases)


def test_manifest_checksums_sizes_and_urls_match_the_zips(built: Path) -> None:
    """Every checksum matches hashlib, and every URL is FCA's own for that quarter."""
    for entry in _manifest(built)["releases"]:
        data = (_ARCHIVE / entry["file"]).read_bytes()
        period = ReportingPeriod.from_period_end(value=entry["period"])
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()
        assert entry["size"] == len(data)
        assert entry["source_url"] == construct_fca_download_url(period=period)


def test_manifest_records_generation_dates_for_modern_releases_only(
    built: Path,
) -> None:
    """Modern zips carry their _G date, and legacy zips carry null."""
    by_period = {entry["period"]: entry for entry in _manifest(built)["releases"]}
    assert by_period["2026-06-30"]["generated"] == "2026-08-04"
    assert by_period["2015-03-31"]["generated"] == "2024-08-07"
    assert by_period["2014-12-31"]["generated"] is None
    assert by_period["2000-03-31"]["generated"] is None


def test_the_package_reads_the_manifest(built: Path) -> None:
    """The catalog built from the manifest points at this snapshot's release."""
    text = (built / "manifest.json").read_text()
    catalog = FCAReleaseCatalog.from_manifest_json(text)
    assert catalog.periods == archived_periods()
    assert catalog.snapshot == "2026-10-12"
    assert catalog.url_for(period=newest_archived_period()).endswith(
        "/fca-data-2026-10-12/2026June.zip"
    )


def test_metadata_zip_holds_the_shipped_files(built: Path) -> None:
    """The bundle holds every shipped schedule file and the registry, unchanged."""
    with zipfile.ZipFile(built / "metadata.zip") as bundle:
        names = bundle.namelist()
        for path in (_METADATA / "schedules").glob("*.json"):
            assert bundle.read(f"schedules/{path.name}") == path.read_bytes()
        registry = (_METADATA / "institutions" / "registry.json").read_bytes()
        assert bundle.read("institutions/registry.json") == registry
        info = json.loads(bundle.read("metadata.json"))
    assert names == sorted(names)
    assert info == {
        "schema_version": 1,
        "through": newest_archived_period().period_end.isoformat(),
    }


def test_manifest_describes_the_metadata_zip(built: Path) -> None:
    """The manifest's metadata entry matches the bundle's bytes."""
    data = (built / "metadata.zip").read_bytes()
    assert _manifest(built)["metadata"] == {
        "file": "metadata.zip",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "through": newest_archived_period().period_end.isoformat(),
        "schema_version": 1,
    }


def test_metadata_zip_is_the_same_every_build(built: Path, tmp_path: Path) -> None:
    """Two builds from the same files give the same bytes.

    Without this, every run of the mirror workflow would see a changed
    bundle and publish a new snapshot.
    """
    _build(tmp_path, snapshot="2026-10-13")
    assert (tmp_path / "metadata.zip").read_bytes() == (
        built / "metadata.zip"
    ).read_bytes()


# ---------------------------------------------------------------------------
# Snapshots and changes
# ---------------------------------------------------------------------------


def test_a_first_build_uploads_every_file(built: Path) -> None:
    """With no previous manifest, every zip and the bundle are new."""
    changes = json.loads((built / "changes.json").read_text())
    assert changes["changed"] is True
    assert len(changes["upload"]) == len(archived_periods()) + 1
    assert "metadata.zip" in changes["upload"]
    assert changes["remove"] == []
    assert _manifest(built)["snapshots"] == ["2026-10-12"]


def test_an_unchanged_build_changes_nothing(built: Path, tmp_path: Path) -> None:
    """The same files as the previous manifest give nothing to upload."""
    changes = _build(
        tmp_path, snapshot="2026-10-13", previous_manifest=built / "manifest.json"
    )
    assert changes == {"changed": False, "upload": [], "remove": []}
    assert _manifest(tmp_path)["snapshots"] == ["2026-10-12", "2026-10-13"]


def test_changes_list_changed_and_removed_files(built: Path, tmp_path: Path) -> None:
    """A changed zip and the bundle are uploaded, and a dropped zip is removed."""
    previous = _manifest(built)
    previous["releases"][0]["sha256"] = "0" * 64
    previous["releases"].append({"file": "Dec1999.zip", "sha256": "1" * 64})
    previous["metadata"]["sha256"] = "2" * 64
    path = tmp_path / "previous.json"
    path.write_text(json.dumps(previous))
    changes = _build(tmp_path / "out", snapshot="2026-10-13", previous_manifest=path)
    assert changes == {
        "changed": True,
        "upload": sorted([previous["releases"][0]["file"], "metadata.zip"]),
        "remove": ["Dec1999.zip"],
    }


def test_compare_manifests_handles_a_previous_manifest_with_no_metadata() -> None:
    """A previous manifest without a bundle entry uploads the bundle."""
    current = {
        "releases": [{"file": "Mar2000.zip", "sha256": "a"}],
        "metadata": {"file": "metadata.zip", "sha256": "b"},
    }
    previous = {"releases": [{"file": "Mar2000.zip", "sha256": "a"}]}
    assert build_script.compare_manifests(current, previous) == {
        "changed": True,
        "upload": ["metadata.zip"],
        "remove": [],
    }


def test_a_published_snapshot_cannot_be_built_again(
    built: Path, tmp_path: Path
) -> None:
    """A snapshot is immutable, so building one already listed fails."""
    with pytest.raises(build_script.MirrorBuildError, match="already published"):
        _build(tmp_path, previous_manifest=built / "manifest.json")


@pytest.mark.parametrize("snapshot", ["latest", "2026-13-01", "today"])
def test_an_invalid_snapshot_fails(snapshot: str, tmp_path: Path) -> None:
    """The snapshot must be a date, since it becomes a release tag."""
    with pytest.raises(build_script.MirrorBuildError, match="--snapshot"):
        _build(tmp_path, snapshot=snapshot)


# ---------------------------------------------------------------------------
# Archives that cannot be published
# ---------------------------------------------------------------------------


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    """Return an empty folder to put synthetic release zips in."""
    folder = tmp_path / "archive"
    folder.mkdir()
    return folder


def test_an_empty_archive_fails(archive: Path, tmp_path: Path) -> None:
    """A folder with no zips is never published as an empty mirror."""
    with pytest.raises(build_script.MirrorBuildError, match="holds no release zips"):
        _build(tmp_path / "out", archive_dir=archive)


@pytest.mark.parametrize("name", ["mar2000.zip", "Sep2014.zip", "notes.zip"])
def test_a_misnamed_zip_fails(name: str, archive: Path, tmp_path: Path) -> None:
    """A zip must have the exact name FCA gives its quarter, letter case included."""
    write_release_zip(archive / name)
    with pytest.raises(build_script.MirrorBuildError, match=name):
        _build(tmp_path / "out", archive_dir=archive)


def test_a_file_that_is_not_a_zip_fails(archive: Path, tmp_path: Path) -> None:
    """A corrupt zip fails the build rather than being published."""
    (archive / "Mar2000.zip").write_bytes(b"not a zip")
    with pytest.raises(build_script.MirrorBuildError, match="not a zip file"):
        _build(tmp_path / "out", archive_dir=archive)


def test_a_zip_with_no_layout_file_fails(archive: Path, tmp_path: Path) -> None:
    """A zip with no D_*.TXT file is not a release."""
    write_release_zip(archive / "Mar2000.zip", {"RCB0300.TXT": "1,2\n"})
    with pytest.raises(build_script.MirrorBuildError, match=r"no D_\*\.TXT"):
        _build(tmp_path / "out", archive_dir=archive)


def test_a_zip_with_two_generation_dates_fails(archive: Path, tmp_path: Path) -> None:
    """One release's data files must carry one generation date."""
    members = {
        "D_RCB.TXT": "x",
        "RCB_Q202603_G20260507.TXT": "x",
        "RC_Q202603_G20260831.TXT": "x",
    }
    write_release_zip(archive / "2026March.zip", members)
    with pytest.raises(build_script.MirrorBuildError, match="more than one"):
        _build(tmp_path / "out", archive_dir=archive)


def test_a_zip_in_a_folder_inside_the_zip_is_read(
    archive: Path, tmp_path: Path
) -> None:
    """Member folders are ignored when looking for layout files and dates."""
    members = {"2026March/D_RCB.TXT": "x", "2026March/RCB_Q202603_G20260507.TXT": "x"}
    write_release_zip(archive / "2026March.zip", members)
    _build(tmp_path / "out", archive_dir=archive)
    assert _manifest(tmp_path / "out")["releases"][0]["generated"] == "2026-05-07"


def test_metadata_behind_the_archive_fails(archive: Path, tmp_path: Path) -> None:
    """A quarter newer than the shipped metadata asks for the generation scripts."""
    write_release_zip(archive / "2026September.zip")
    with pytest.raises(build_script.MirrorBuildError, match="generate_fca"):
        _build(tmp_path / "out", archive_dir=archive)


def test_a_metadata_folder_with_no_schedules_fails(
    archive: Path, tmp_path: Path
) -> None:
    """Without schedule metadata there is no bundle to publish."""
    write_release_zip(archive / "Mar2000.zip")
    (tmp_path / "metadata" / "schedules").mkdir(parents=True)
    with pytest.raises(build_script.MirrorBuildError, match="holds no metadata"):
        _build(
            tmp_path / "out", archive_dir=archive, metadata_dir=tmp_path / "metadata"
        )


def test_the_metadata_covers_the_earlier_of_schedules_and_registry(
    tmp_path: Path,
) -> None:
    """A registry behind the schedules decides how far the bundle reaches."""
    metadata = tmp_path / "metadata"
    (metadata / "schedules").mkdir(parents=True)
    (metadata / "institutions").mkdir()
    for path in (_METADATA / "schedules").glob("*.json"):
        (metadata / "schedules" / path.name).write_bytes(path.read_bytes())
    december = ReportingPeriod.from_period_end(value="2025-12-31")
    registry = FCAInstitutionRegistry.from_rosters(rosters=[(december, [ROW])])
    (metadata / "institutions" / "registry.json").write_text(registry.to_json())
    assert build_script.metadata_through(metadata) == december


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


def test_main_writes_the_files(tmp_path: Path) -> None:
    """The command line builds the three files and returns 0."""
    status = build_script.main(["--snapshot", "2026-10-12", "--out-dir", str(tmp_path)])
    assert status == 0
    assert {path.name for path in tmp_path.iterdir()} == {
        "manifest.json",
        "metadata.zip",
        "changes.json",
    }


def test_main_reports_a_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A build that cannot go ahead logs why and returns 1."""
    archive = tmp_path / "archive"
    archive.mkdir()
    argv = ["--snapshot", "2026-10-12", "--out-dir", str(tmp_path / "out")]
    with caplog.at_level(logging.ERROR):
        status = build_script.main([*argv, "--archive-dir", str(archive)])
    assert status == 1
    assert "holds no release zips" in caplog.text
    assert not (tmp_path / "out").exists()


def test_metadata_zip_members_have_fixed_attributes(built: Path) -> None:
    """Every member has the same timestamp, permissions, and no compression."""
    data = (built / "metadata.zip").read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as bundle:
        infos = bundle.infolist()
    assert {info.date_time for info in infos} == {(1980, 1, 1, 0, 0, 0)}
    assert {info.compress_type for info in infos} == {zipfile.ZIP_STORED}
    assert {info.external_attr for info in infos} == {0o644 << 16}
