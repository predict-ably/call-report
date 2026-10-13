"""Tests for the FCA drift check script (scripts/check_fca_drift.py).

FCA's site and the GitHub mirror are both stood in for by the local server,
through the ``fca_site`` and ``mirror`` fixtures. The archive is a temporary
folder of small synthetic release zips, so no test reaches the internet or
touches ``data/fca-call-report/``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from call_report.core import ReportingPeriod
from call_report.fca import FCAInstitutionRegistry
from tests.fca.institution_rows import ROW
from tests.helpers import load_drift_script, write_release_zip

drift = load_drift_script()

_PAGE_PATH = Path("bank-oversight") / "call-report-data-for-download"
_MODERN = Path("template-fca") / "bank"
_LEGACY = Path("template-fca") / "download" / "CallReportData"


def _sha(path: Path) -> str:
    """Return a file's SHA-256."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _zip(path: Path, generated: str = "20260507", extra: str = "") -> Path:
    """Write a modern release zip whose content depends on `extra`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    return write_release_zip(
        path, {"D_RCB.TXT": "layout\n", f"RCB_Q1_G{generated}.TXT": f"1,2{extra}\n"}
    )


def _legacy_zip(path: Path, extra: str = "") -> Path:
    """Write a legacy release zip, which has no generation date."""
    path.parent.mkdir(parents=True, exist_ok=True)
    return write_release_zip(path, {"D_RCB.TXT": "x\n", "RCB0300.TXT": f"1{extra}\n"})


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    """Return an archive holding 2000Q1, 2000Q2, and 2026Q1."""
    folder = tmp_path / "archive"
    _legacy_zip(folder / "Mar2000.zip")
    _legacy_zip(folder / "Jun2000.zip")
    _zip(folder / "2026March.zip")
    (folder / "README.md").write_text("Not a release, so the check ignores it.")
    return folder


def _publish_page(site: Path, links: list[str]) -> None:
    """Serve an FCA download page linking each path in `links`."""
    page = site / _PAGE_PATH
    page.parent.mkdir(parents=True, exist_ok=True)
    anchors = "".join(f'<p><a href="/{link}">{link}</a></p>' for link in links)
    page.write_text(f"<html><body>{anchors}</body></html>", encoding="utf-8")


def _publish_fca_copy(site: Path, archive: Path) -> None:
    """Serve every archived zip on the FCA site, unchanged, and a page listing them."""
    links = []
    for path in sorted(archive.glob("*.zip")):
        base = _MODERN if path.name[0].isdigit() else _LEGACY
        (site / base).mkdir(parents=True, exist_ok=True)
        (site / base / path.name).write_bytes(path.read_bytes())
        links.append((base / path.name).as_posix())
    _publish_page(site, links)


def _publish_manifest(
    mirror: Path, archive: Path, *, snapshot: str = "2026-10-12", **changes: str
) -> None:
    """Serve a rolling manifest listing the archive, with some checksums replaced.

    `changes` maps a zip name to the checksum the manifest gives it.
    """
    releases = []
    for path in sorted(archive.glob("*.zip")):
        period = drift.parse_fca_release_filename(path.name)
        releases.append(
            {
                "period": period.period_end.isoformat(),
                "file": path.name,
                "sha256": changes.get(path.name, _sha(path)),
            }
        )
    folder = mirror / "fca-call-report-data"
    folder.mkdir(exist_ok=True)
    manifest = {"schema_version": 1, "snapshot": snapshot, "releases": releases}
    (folder / "manifest.json").write_text(json.dumps(manifest))


def _check(archive: Path, tmp_path: Path) -> Any:
    """Run a check against the local server, saving zips under tmp_path."""
    return drift.check(
        download_dir=tmp_path / "downloads", archive_dir=archive, max_retries=0
    )


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def test_a_matching_fca_and_mirror_are_clean(
    archive: Path, tmp_path: Path, fca_site: Path, mirror: Path
) -> None:
    """When FCA and the mirror both match the archive, nothing is reported."""
    _publish_fca_copy(fca_site, archive)
    _publish_manifest(mirror, archive)
    report = _check(archive, tmp_path)
    assert report.status == "clean"
    assert report.exit_code == drift.EXIT_CLEAN
    assert (report.new, report.changed, report.missing, report.errors) == (
        [],
        [],
        [],
        [],
    )
    assert report.mirror == {
        "status": "in_step",
        "snapshot": "2026-10-12",
        "missing": [],
        "outdated": [],
        "extra": [],
    }
    assert list((tmp_path / "downloads").iterdir()) == []


def test_drift_reports_new_changed_missing_and_unmatched_files(
    archive: Path, tmp_path: Path, fca_site: Path, mirror: Path
) -> None:
    """Every kind of difference is reported, and only differing zips are saved."""
    _publish_fca_copy(fca_site, archive)
    _publish_manifest(mirror, archive)
    _legacy_zip(fca_site / _LEGACY / "Jun2000.zip", extra="amended")
    _zip(fca_site / _MODERN / "2026June.zip", generated="20260804")
    _publish_page(
        fca_site,
        [
            f"{_LEGACY.as_posix()}/Mar2000.zip",
            f"{_LEGACY.as_posix()}/Jun2000.zip",
            f"{_MODERN.as_posix()}/2026June.zip",
            f"{_MODERN.as_posix()}/Instructions.zip",
        ],
    )
    report = _check(archive, tmp_path)
    assert report.status == "drift"
    assert report.exit_code == drift.EXIT_DRIFT
    assert report.new == ["2026-06-30"]
    assert report.changed == ["2000-06-30"]
    assert report.missing == ["2026-03-31"]
    assert [url.rsplit("/", 1)[-1] for url in report.unmatched] == ["Instructions.zip"]
    assert report.files["2026-06-30"]["file"] == "2026June.zip"
    assert report.files["2026-06-30"]["generated"] == "2026-08-04"
    assert report.files["2000-06-30"]["generated"] is None
    saved = sorted(path.name for path in (tmp_path / "downloads").iterdir())
    assert saved == ["2026June.zip", "Jun2000.zip"]
    for entry in report.files.values():
        assert _sha(tmp_path / "downloads" / entry["file"]) == entry["sha256"]


def test_an_unreadable_download_page_is_an_error(
    archive: Path, tmp_path: Path, fca_site: Path, mirror: Path
) -> None:
    """A page FCA does not serve stops the FCA comparison and exits 2."""
    _publish_manifest(mirror, archive)
    report = _check(archive, tmp_path)
    assert report.status == "error"
    assert report.exit_code == drift.EXIT_ERROR
    assert "download page could not be read" in report.errors[0]
    assert report.mirror["status"] == "in_step"


def test_a_failed_zip_download_is_an_error_and_the_others_are_compared(
    archive: Path, tmp_path: Path, fca_site: Path, mirror: Path
) -> None:
    """One quarter failing is recorded, and the rest of the check carries on."""
    _publish_fca_copy(fca_site, archive)
    _publish_manifest(mirror, archive)
    (fca_site / _LEGACY / "Mar2000.zip").unlink()
    _legacy_zip(fca_site / _LEGACY / "Jun2000.zip", extra="amended")
    report = _check(archive, tmp_path)
    assert report.status == "error"
    assert len(report.errors) == 1
    assert report.errors[0].startswith("2000Q1:")
    assert report.changed == ["2000-06-30"]


@pytest.mark.parametrize(
    ("content", "message"),
    [(b"PK not really", "not a zip file"), (None, "no D_")],
)
def test_a_zip_that_is_not_a_release_is_an_error(
    archive: Path,
    tmp_path: Path,
    fca_site: Path,
    mirror: Path,
    content: bytes | None,
    message: str,
) -> None:
    """A file FCA serves that is not a release is reported, and never saved."""
    _publish_fca_copy(fca_site, archive)
    _publish_manifest(mirror, archive)
    target = fca_site / _MODERN / "2026March.zip"
    if content is None:
        write_release_zip(target, {"README.TXT": "x"})
    else:
        target.write_bytes(content)
    report = _check(archive, tmp_path)
    assert report.status == "error"
    assert message in report.errors[0]
    assert report.files == {}
    assert list((tmp_path / "downloads").iterdir()) == []


def test_a_mirror_out_of_step_is_drift(
    archive: Path, tmp_path: Path, fca_site: Path, mirror: Path
) -> None:
    """A mirror missing, holding a different, or holding an extra zip is drift."""
    _publish_fca_copy(fca_site, archive)
    extra = tmp_path / "extra"
    _zip(extra / "2026June.zip")
    _legacy_zip(extra / "Mar2000.zip")
    _publish_manifest(mirror, extra, **{"Mar2000.zip": "0" * 64})
    report = _check(archive, tmp_path)
    assert report.status == "drift"
    assert report.mirror == {
        "status": "out_of_step",
        "snapshot": "2026-10-12",
        "missing": ["2000-06-30", "2026-03-31"],
        "outdated": ["2000-03-31"],
        "extra": ["2026-06-30"],
    }


def test_a_mirror_that_cannot_be_read_is_an_error(
    archive: Path, tmp_path: Path, fca_site: Path, mirror: Path
) -> None:
    """A mirror whose manifest cannot be read is an error, not drift.

    Nothing is known to differ, so a brief GitHub outage must not report
    that FCA's releases differ from the archive.
    """
    _publish_fca_copy(fca_site, archive)
    report = _check(archive, tmp_path)
    assert report.status == "error"
    assert report.exit_code == drift.EXIT_ERROR
    assert report.errors == []
    assert report.mirror["status"] == "unavailable"
    assert "404" in report.mirror["error"]


def test_a_report_round_trips_through_json(
    archive: Path, tmp_path: Path, fca_site: Path, mirror: Path
) -> None:
    """The report the check writes is the report apply and describe read."""
    _publish_fca_copy(fca_site, archive)
    _publish_manifest(mirror, archive)
    _zip(fca_site / _MODERN / "2026June.zip")
    _publish_page(fca_site, [f"{_MODERN.as_posix()}/2026June.zip"])
    report = _check(archive, tmp_path)
    text = report.to_json()
    assert json.loads(text)["status"] == "drift"
    assert drift.DriftReport.from_json(text) == report


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def _drifted_report(archive: Path, tmp_path: Path, fca_site: Path) -> Any:
    """Check against an FCA site with one new and one changed quarter."""
    _publish_fca_copy(fca_site, archive)
    _legacy_zip(fca_site / _LEGACY / "Jun2000.zip", extra="amended")
    _zip(fca_site / _MODERN / "2026June.zip")
    _publish_page(
        fca_site,
        [
            f"{_LEGACY.as_posix()}/Jun2000.zip",
            f"{_MODERN.as_posix()}/2026June.zip",
        ],
    )
    return _check(archive, tmp_path)


def test_apply_writes_new_and_changed_zips_and_keeps_the_rest(
    archive: Path, tmp_path: Path, fca_site: Path, mirror: Path
) -> None:
    """New and changed zips land in the archive, and nothing is deleted."""
    report = _drifted_report(archive, tmp_path, fca_site)
    written = drift.apply(
        report, download_dir=tmp_path / "downloads", archive_dir=archive
    )
    assert [path.name for path in written] == ["Jun2000.zip", "2026June.zip"]
    names = sorted(path.name for path in archive.glob("*.zip"))
    assert names == ["2026June.zip", "2026March.zip", "Jun2000.zip", "Mar2000.zip"]
    assert _sha(archive / "Jun2000.zip") == report.files["2000-06-30"]["sha256"]


def test_apply_refuses_a_saved_zip_that_differs_from_the_report(
    archive: Path, tmp_path: Path, fca_site: Path, mirror: Path
) -> None:
    """A saved zip whose checksum is not the report's writes nothing."""
    report = _drifted_report(archive, tmp_path, fca_site)
    before = {path.name: _sha(path) for path in archive.iterdir()}
    (tmp_path / "downloads" / "2026June.zip").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="2026-06-30"):
        drift.apply(report, download_dir=tmp_path / "downloads", archive_dir=archive)
    assert {path.name: _sha(path) for path in archive.iterdir()} == before


def test_apply_refuses_a_missing_saved_zip(
    archive: Path, tmp_path: Path, fca_site: Path, mirror: Path
) -> None:
    """A zip the check saved but that is now gone writes nothing."""
    report = _drifted_report(archive, tmp_path, fca_site)
    (tmp_path / "downloads" / "Jun2000.zip").unlink()
    with pytest.raises(ValueError, match="2000-06-30"):
        drift.apply(report, download_dir=tmp_path / "downloads", archive_dir=archive)
    assert not (archive / "2026June.zip").exists()


# ---------------------------------------------------------------------------
# The summary
# ---------------------------------------------------------------------------


def test_the_summary_lists_each_kind_of_difference() -> None:
    """The issue's summary names every section that has something in it."""
    report = drift.DriftReport(
        checked_at="2026-10-12T06:00:00+00:00",
        new=["2026-09-30"],
        changed=["2025-03-31"],
        missing=["2000-03-31"],
        unmatched=["https://www.fca.gov/x.zip"],
        files={
            "2026-09-30": {"file": "2026September.zip", "generated": "2026-11-02"},
            "2025-03-31": {"file": "2025March.zip", "generated": None},
        },
        mirror={
            "status": "out_of_step",
            "snapshot": "2026-10-12",
            "missing": ["2026-06-30"],
            "outdated": [],
            "extra": ["2000-03-31"],
        },
        errors=["2026Q1: refused"],
    )
    summary = drift.render_summary(report)
    assert "**error**" in summary
    assert "- 2026Q3 (`2026September.zip`, generated 2026-11-02)" in summary
    assert "- 2025Q1 (`2025March.zip`)" in summary
    assert "no longer lists (left in the archive)" in summary
    assert "- https://www.fca.gov/x.zip" in summary
    assert "- 2026Q1: refused" in summary
    assert "- Not on the mirror: 2026Q2" in summary
    assert "Different on the mirror" not in summary
    assert "- On the mirror but not in the archive: 2000Q1" in summary


@pytest.mark.parametrize(
    ("mirror_state", "expected"),
    [
        ({"status": "in_step", "snapshot": "2026-10-12"}, "matches the archive"),
        ({"status": "unavailable", "error": "HTTP 404"}, "could not be read: HTTP 404"),
    ],
)
def test_the_summary_describes_the_mirror(
    mirror_state: dict[str, Any], expected: str
) -> None:
    """A clean summary still says how the mirror compares."""
    report = drift.DriftReport(checked_at="now", mirror=mirror_state)
    summary = drift.render_summary(report)
    assert expected in summary
    assert "New quarters" not in summary


# ---------------------------------------------------------------------------
# describe
# ---------------------------------------------------------------------------


def _registry(rows: dict[str, list[dict[str, Any]]]) -> FCAInstitutionRegistry:
    """Build a registry from roster rows keyed by period end."""
    return FCAInstitutionRegistry.from_rosters(
        rosters=[
            (ReportingPeriod.from_period_end(value=key), value)
            for key, value in rows.items()
        ]
    )


def test_changed_institutions_finds_backfilled_and_dropped_uninums() -> None:
    """A changed value or a dropped quarter or UNINUM is listed.

    A quarter the regeneration adds is not compared, so a UNINUM whose only
    change is a new quarter is not listed.
    """
    other = {**ROW, "UNINUM": 100, "SHORTNAME": "Other ACA"}
    gone = {**ROW, "UNINUM": 200, "SHORTNAME": None}
    late = {**ROW, "UNINUM": 300, "SHORTNAME": "Late ACA"}
    moved = {**ROW, "ZIP": "40299"}
    previous = _registry({"2025-12-31": [ROW, other, gone, late]})
    current = _registry(
        {
            "2025-12-31": [moved, other],
            "2026-03-31": [moved, {**other, "ZIP": "1"}, late],
        }
    )
    assert drift.changed_institutions(previous, current) == [
        (200, None),
        (300, "Late ACA"),
        (722825, "Mid-America ACA"),
    ]


def test_changed_schedules_finds_added_removed_and_changed_files(
    tmp_path: Path,
) -> None:
    """A schedule file that differs, or exists on one side only, is listed."""
    before = tmp_path / "before" / "schedules"
    after = tmp_path / "after" / "schedules"
    before.mkdir(parents=True)
    after.mkdir(parents=True)
    (before / "RC.json").write_text("same")
    (after / "RC.json").write_text("same")
    (before / "RCB.json").write_text("old")
    (after / "RCB.json").write_text("new")
    (before / "RCO.json").write_text("gone")
    (after / "RCX.json").write_text("added")
    assert drift.changed_schedules(before.parent, after.parent) == ["RCB", "RCO", "RCX"]
    assert drift.changed_schedules(tmp_path / "none", after.parent) == [
        "RC",
        "RCB",
        "RCX",
    ]


@pytest.mark.parametrize(
    ("new", "changed", "expected"),
    [
        (
            ["2026-09-30"],
            [],
            "The FCA archive now covers 2026Q3.",
        ),
        (
            [],
            ["2025-03-31", "2025-06-30"],
            "The FCA archive now holds FCA's new versions of 2025Q1 and 2025Q2.",
        ),
        (
            ["2026-09-30"],
            ["2025-03-31", "2025-06-30", "2025-09-30"],
            (
                "The FCA archive now covers 2026Q3. It also holds FCA's new "
                "versions of 2025Q1, 2025Q2, and 2025Q3."
            ),
        ),
    ],
)
def test_changelog_entry_names_new_and_changed_quarters(
    new: list[str], changed: list[str], expected: str
) -> None:
    """The entry says what the archive now holds, as one wrapped bullet."""
    report = drift.DriftReport(new=new, changed=changed)
    entry = drift.changelog_entry(report, issue=140)
    lines = entry.splitlines()
    assert entry.endswith("\n")
    assert lines[0].startswith("- ")
    assert all(line.startswith("  ") for line in lines[1:])
    assert max(len(line) for line in lines) <= 78
    assert " ".join(line[2:] for line in lines) == (
        f"{expected} The shipped schedule metadata and institution registry are "
        "regenerated to match (:issue:`140`)."
    )


_CHANGELOG = """Changelog
=========

.. _changelog_unreleased:

Unreleased
==========

- An earlier entry (:issue:`1`).

0.1.0
=====
"""


def test_update_changelog_adds_then_replaces_its_entry(tmp_path: Path) -> None:
    """The entry goes first under Unreleased, and a second run replaces it."""
    path = tmp_path / "changelog.rst"
    path.write_text(_CHANGELOG)
    first = drift.changelog_entry(drift.DriftReport(new=["2026-09-30"]), issue=140)
    drift.update_changelog(path, first, issue=140)
    second = drift.changelog_entry(
        drift.DriftReport(new=["2026-09-30"], changed=["2025-03-31"]), issue=140
    )
    drift.update_changelog(path, second, issue=140)
    text = path.read_text()
    assert text.count(":issue:`140`") == 1
    assert "Unreleased\n==========\n\n" + second + "- An earlier entry" in text


def test_update_changelog_leaves_another_issues_entry(tmp_path: Path) -> None:
    """An archive entry for a different issue is kept, and a new one is added."""
    path = tmp_path / "changelog.rst"
    earlier = drift.changelog_entry(drift.DriftReport(new=["2026-06-30"]), issue=130)
    path.write_text(_CHANGELOG.replace("- An earlier entry (:issue:`1`).\n", earlier))
    entry = drift.changelog_entry(drift.DriftReport(new=["2026-09-30"]), issue=140)
    drift.update_changelog(path, entry, issue=140)
    text = path.read_text()
    assert ":issue:`130`" in text
    assert text.index(":issue:`140`") < text.index(":issue:`130`")


def test_update_changelog_adds_an_unreleased_section_before_a_release(
    tmp_path: Path,
) -> None:
    """Right after a release there is no Unreleased section, so one is added.

    Without this, the first drift pull request after a release would fail.
    """
    path = tmp_path / "changelog.rst"
    path.write_text(
        "Changelog\n=========\n\n.. _changelog_0.2.0:\n\n0.2.0\n=====\n\n- Old.\n"
    )
    drift.update_changelog(path, "- New (:issue:`140`).\n", issue=140)
    assert path.read_text() == (
        "Changelog\n=========\n\n"
        ".. _changelog_unreleased:\n\nUnreleased\n==========\n\n"
        "- New (:issue:`140`).\n\n"
        ".. _changelog_0.2.0:\n\n0.2.0\n=====\n\n- Old.\n"
    )


@pytest.mark.parametrize("text", ["Changelog\n=========\n", "Changelog\n=========\n\n"])
def test_update_changelog_adds_an_unreleased_section_at_the_end(
    tmp_path: Path, text: str
) -> None:
    """A changelog with no release yet gets the section at its end."""
    path = tmp_path / "changelog.rst"
    path.write_text(text)
    drift.update_changelog(path, "- New (:issue:`140`).\n", issue=140)
    assert path.read_text() == (
        "Changelog\n=========\n\n"
        ".. _changelog_unreleased:\n\nUnreleased\n==========\n\n"
        "- New (:issue:`140`).\n\n"
    )


def _data_dir(folder: Path, registry: FCAInstitutionRegistry, schedule: str) -> Path:
    """Write a shipped data folder holding a registry and one schedule file."""
    (folder / "institutions").mkdir(parents=True)
    (folder / "schedules").mkdir()
    (folder / "institutions" / "registry.json").write_text(registry.to_json())
    (folder / "schedules" / "RCB.json").write_text(schedule)
    return folder


def test_describe_writes_the_changelog_and_returns_the_description(
    tmp_path: Path,
) -> None:
    """The description closes the issue and lists every change for review."""
    previous = _data_dir(tmp_path / "before", _registry({"2025-12-31": [ROW]}), "a")
    current = _data_dir(
        tmp_path / "after", _registry({"2025-12-31": [{**ROW, "ZIP": "1"}]}), "b"
    )
    changelog = tmp_path / "changelog.rst"
    changelog.write_text(_CHANGELOG)
    report = drift.DriftReport(
        new=["2026-09-30"],
        files={"2026-09-30": {"file": "2026September.zip", "generated": "2026-11-02"}},
    )
    body = drift.describe(
        report,
        issue=140,
        previous_data=previous,
        data_dir=current,
        changelog=changelog,
    )
    assert "Closes #140." in body
    assert "- 2026Q3 (`2026September.zip`, generated 2026-11-02)" in body
    assert "- `RCB`" in body
    assert "- 722825 (Mid-America ACA)" in body
    assert "Quarters FCA has changed:\n\nNone." in body
    assert ":issue:`140`" in changelog.read_text()


def test_the_description_names_a_uninum_with_no_short_name() -> None:
    """A UNINUM with no short name is listed by number alone."""
    body = drift.render_pull_request(
        drift.DriftReport(), issue=140, schedules=[], institutions=[(200, None)]
    )
    assert "- 200\n" in body


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


def test_main_check_writes_the_report_and_summary(
    archive: Path, tmp_path: Path, fca_site: Path, mirror: Path
) -> None:
    """Check writes both files and exits with the report's status."""
    _publish_fca_copy(fca_site, archive)
    _publish_manifest(mirror, archive)
    report, summary = tmp_path / "drift.json", tmp_path / "drift.md"
    status = drift.main(
        [
            "check",
            "--report",
            str(report),
            "--summary",
            str(summary),
            "--download-dir",
            str(tmp_path / "downloads"),
            "--archive-dir",
            str(archive),
            "--max-retries",
            "0",
        ]
    )
    assert status == drift.EXIT_CLEAN
    assert json.loads(report.read_text())["status"] == "clean"
    assert "**clean**" in summary.read_text()


def test_main_check_without_a_summary_file(
    archive: Path, tmp_path: Path, fca_site: Path, mirror: Path
) -> None:
    """The summary file is optional, and a drift exits 1."""
    _publish_fca_copy(fca_site, archive)
    _publish_manifest(mirror, archive)
    _zip(fca_site / _MODERN / "2026June.zip")
    _publish_page(fca_site, [f"{_MODERN.as_posix()}/2026June.zip"])
    status = drift.main(
        [
            "check",
            "--report",
            str(tmp_path / "drift.json"),
            "--download-dir",
            str(tmp_path / "downloads"),
            "--archive-dir",
            str(archive),
            "--max-retries",
            "0",
        ]
    )
    assert status == drift.EXIT_DRIFT


def test_main_apply_copies_the_zips_or_reports_why_not(
    archive: Path,
    tmp_path: Path,
    fca_site: Path,
    mirror: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Apply exits 0 after copying, and 2 when a saved zip does not match."""
    report = _drifted_report(archive, tmp_path, fca_site)
    path = tmp_path / "drift.json"
    path.write_text(report.to_json())
    argv = [
        "apply",
        "--report",
        str(path),
        "--download-dir",
        str(tmp_path / "downloads"),
    ]
    assert drift.main([*argv, "--archive-dir", str(archive)]) == drift.EXIT_CLEAN
    assert (archive / "2026June.zip").is_file()
    (tmp_path / "downloads" / "2026June.zip").write_bytes(b"x")
    with caplog.at_level(logging.ERROR):
        status = drift.main([*argv, "--archive-dir", str(archive)])
    assert status == drift.EXIT_ERROR
    assert "Run the check again" in caplog.text


def test_main_describe_writes_the_body(tmp_path: Path) -> None:
    """Describe writes the pull request description to the file named."""
    registry = _registry({"2025-12-31": [ROW]})
    previous = _data_dir(tmp_path / "before", registry, "a")
    current = _data_dir(tmp_path / "after", registry, "a")
    changelog = tmp_path / "changelog.rst"
    changelog.write_text(_CHANGELOG)
    report = tmp_path / "drift.json"
    report.write_text(
        drift.DriftReport(
            changed=["2025-12-31"],
            files={"2025-12-31": {"file": "2025December.zip", "generated": None}},
        ).to_json()
    )
    body = tmp_path / "body.md"
    status = drift.main(
        [
            "describe",
            "--report",
            str(report),
            "--issue",
            "140",
            "--previous-data",
            str(previous),
            "--body",
            str(body),
            "--data-dir",
            str(current),
            "--changelog",
            str(changelog),
        ]
    )
    assert status == drift.EXIT_CLEAN
    assert "- 2025Q4 (`2025December.zip`)" in body.read_text()


def test_main_check_that_fails_still_writes_a_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unexpected failure writes a report holding it, and exits 2.

    An uncaught exception would exit 1, which the workflow reads as drift,
    and would leave no report to open the issue from.
    """

    def fail(**_: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(drift, "check", fail)
    report, summary = tmp_path / "drift.json", tmp_path / "drift.md"
    argv = ["check", "--report", str(report), "--download-dir", str(tmp_path)]
    with caplog.at_level(logging.ERROR):
        status = drift.main([*argv, "--summary", str(summary)])
    assert status == drift.EXIT_ERROR
    written = json.loads(report.read_text())
    assert written["status"] == "error"
    assert "disk full" in written["errors"][0]
    assert "The check failed." in caplog.text
    # No mirror comparison ran, so the summary must not claim one differs.
    assert "The mirror was not checked." in summary.read_text()
    assert "differs from the archive" not in summary.read_text()


def test_main_describe_failure_exits_2(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A describe that raises is logged and exits 2, not 1."""
    report = tmp_path / "drift.json"
    report.write_text(drift.DriftReport(new=["2026-09-30"]).to_json())
    argv = [
        "describe",
        "--report",
        str(report),
        "--issue",
        "140",
        "--previous-data",
        str(tmp_path / "missing"),
        "--body",
        str(tmp_path / "body.md"),
    ]
    with caplog.at_level(logging.ERROR):
        assert drift.main(argv) == drift.EXIT_ERROR
    assert "The describe command failed." in caplog.text
    assert not (tmp_path / "body.md").exists()


def test_apply_leaves_no_partial_file_when_a_copy_fails(
    archive: Path,
    tmp_path: Path,
    fca_site: Path,
    mirror: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed copy removes its temporary file and raises the error."""
    report = _drifted_report(archive, tmp_path, fca_site)

    def fail(source: Path, target: Path) -> None:
        Path(target).write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(drift.shutil, "copyfile", fail)
    with pytest.raises(OSError, match="disk full"):
        drift.apply(report, download_dir=tmp_path / "downloads", archive_dir=archive)
    assert list(archive.glob("*.part")) == []


@pytest.mark.parametrize(
    ("command", "text"),
    [
        ("check", "The JSON report to write."),
        ("apply", "The folder the check saved the zips to."),
        ("describe", "The drift issue the PR closes."),
    ],
)
def test_every_command_documents_its_options(
    command: str, text: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each command's --help describes its options."""
    with pytest.raises(SystemExit) as caught:
        drift._parse_args([command, "--help"])
    assert caught.value.code == 0
    assert text in " ".join(capsys.readouterr().out.split())
