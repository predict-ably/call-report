"""Check whether FCA's published releases have drifted from the checked-in archive.

A maintainer tool, not part of the package or its runtime. The nightly
``fca-drift`` workflow runs it. It has three commands.

``check``
    Reads FCA's download page, downloads every release zip it lists, and
    compares each SHA-256 with the zip in ``data/fca-call-report/``. It
    reports the quarters FCA has published that the archive lacks, the
    quarters whose bytes differ, the quarters the archive holds that FCA no
    longer lists, and any zip link whose name is not a release. It also
    compares the GitHub mirror's manifest with the archive, so a mirror that
    failed to publish is noticed. It writes a JSON report and a Markdown
    summary, and saves each new or changed zip to a download folder.
    The exit status is 0 when nothing differs, 1 when something does, and
    2 when the check itself could not finish, for example because FCA
    refused a request.
``apply``
    Copies the new and changed zips a ``check`` saved into the archive,
    replacing the old files after checking each one's SHA-256 against the
    report. It never deletes a zip, so a quarter FCA stops listing is only
    reported.
``describe``
    Writes the description of the data pull request, and adds or replaces
    its changelog entry. The description lists the new and changed
    quarters, each schedule whose shipped metadata changed, and each UNINUM
    whose name or address history changed in quarters the registry already
    covered. Those UNINUMs are what a reviewer checks for values a
    regeneration backfilled into earlier quarters.

Every request sends the package's own ``User-Agent``, as the downloader
does.

Notes
-----
Check FCA and the mirror, keeping the zips that differ::

    python scripts/check_fca_drift.py check --report drift.json --download-dir zips

Copy them into the archive::

    python scripts/check_fca_drift.py apply --report drift.json --download-dir zips
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import tempfile
import textwrap
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from call_report.core import ReportingPeriod
from call_report.core._archive import sha256_of
from call_report.core._http import HttpClient, fetch_bytes
from call_report.exceptions import (
    DownloadError,
    PeriodNotAvailableError,
    ZipArchiveUnpackingError,
)
from call_report.fca import FCAInstitution, FCAInstitutionRegistry
from call_report.fca._discovery import check_release_zip
from call_report.fca.catalog import (
    FCAReleaseCatalog,
    fca_release_stem,
    parse_fca_release_filename,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ARCHIVE_DIR = _REPO_ROOT / "data" / "fca-call-report"
_DATA_DIR = _REPO_ROOT / "src" / "call_report" / "fca" / "data"
_CHANGELOG = _REPO_ROOT / "docs" / "source" / "changelog.rst"

EXIT_CLEAN = 0
EXIT_DRIFT = 1
EXIT_ERROR = 2

_DOWNLOAD_WORKERS = 4
_CHANGELOG_WIDTH = 78

_log = logging.getLogger("check_fca_drift")


@dataclass
class DriftReport:
    """What a check found.

    `check` fills it in, and `to_json` writes it for the `apply` and `describe`
    commands.

    Attributes
    ----------
    checked_at : str
        When the check ran, as an ISO 8601 UTC timestamp.
    new : list[str]
        The period ends of quarters FCA lists that the archive lacks.
    changed : list[str]
        The period ends of quarters whose FCA zip differs from the
        archive's.
    missing : list[str]
        The period ends of quarters the archive holds that FCA does not
        list.
    unmatched : list[str]
        The URLs of zip links on FCA's page whose names name no release.
    files : dict[str, dict[str, Any]]
        For each new or changed quarter, keyed by period end, the zip's
        ``file`` name, ``sha256``, FCA ``url``, and ``generated`` date.
    mirror : dict[str, Any]
        The mirror comparison. ``status`` is ``"in_step"``,
        ``"out_of_step"``, or ``"unavailable"``. ``missing``, ``outdated``,
        and ``extra`` list period ends, and ``snapshot`` names the snapshot
        compared.
    errors : list[str]
        Every failure that kept the check from finishing.
    """  # numpydoc ignore=PR01

    checked_at: str = ""
    new: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    mirror: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        """Return ``"error"``, ``"drift"``, or ``"clean"``.

        An error outranks a difference, since a check that could not finish may have
        missed one.

        Returns
        -------
        str
            ``"error"`` if the check could not finish, ``"drift"`` if
            anything differs, and ``"clean"`` otherwise.
        """
        if self.errors:
            return "error"
        drifted = self.new or self.changed or self.missing or self.unmatched
        if drifted or self.mirror.get("status") != "in_step":
            return "drift"
        return "clean"

    @property
    def exit_code(self) -> int:
        """Return the exit status a check with this report ends with.

        The workflow reads it to decide whether to open an issue.

        Returns
        -------
        int
            `EXIT_ERROR`, `EXIT_DRIFT`, or `EXIT_CLEAN`.
        """
        return {"error": EXIT_ERROR, "drift": EXIT_DRIFT}.get(self.status, EXIT_CLEAN)

    def to_json(self) -> str:
        """Return the report as JSON text.

        The status is written too, so a reader of the file need not work it out.

        Returns
        -------
        str
            The report, with its status.
        """
        data = {"status": self.status, **self.__dict__}
        return json.dumps(data, indent=2) + "\n"

    @classmethod
    def from_json(cls, text: str) -> DriftReport:
        """Read a report written by `to_json`.

        The status is dropped, since the report works it out again.

        Parameters
        ----------
        text : str
            The report's JSON text.

        Returns
        -------
        DriftReport
            The report.
        """
        data = json.loads(text)
        data.pop("status")
        return cls(**data)


def archived_checksums(archive_dir: Path) -> dict[ReportingPeriod, str]:
    """Return the SHA-256 of every release zip in the archive, by period.

    Files whose names are not release zips are ignored.

    Parameters
    ----------
    archive_dir : pathlib.Path
        The folder of release zips.

    Returns
    -------
    dict[ReportingPeriod, str]
        Each archived quarter's checksum.
    """
    checksums: dict[ReportingPeriod, str] = {}
    for path in archive_dir.iterdir():
        period = parse_fca_release_filename(path.name)
        if period is not None:
            checksums[period] = sha256_of(path)
    return dict(sorted(checksums.items()))


def check(
    *,
    download_dir: Path,
    archive_dir: Path = _ARCHIVE_DIR,
    timeout: float = 60.0,
    max_retries: int = 3,
    http_client: HttpClient = "urllib",
) -> DriftReport:
    """Compare FCA's releases and the mirror with the archive.

    Nothing in the archive is changed. `apply` copies the saved zips in.

    Parameters
    ----------
    download_dir : pathlib.Path
        The folder each new or changed zip is saved to. It is created if
        missing.
    archive_dir : pathlib.Path, optional
        The folder of release zips. Defaults to ``data/fca-call-report/``.
    timeout : float, default 60.0
        Seconds to wait for the server before a request fails.
    max_retries : int, default 3
        How many times to send a request again after a transient failure.
    http_client : {"urllib", "curl_cffi"}, default "urllib"
        The library used for HTTP requests. ``"urllib"`` uses the standard
        library. ``"curl_cffi"`` needs
        ``pip install "call-report[curl_cffi]"``.

    Returns
    -------
    DriftReport
        What the check found.
    """
    report = DriftReport(checked_at=datetime.now(UTC).isoformat(timespec="seconds"))
    archived = archived_checksums(archive_dir)
    options: dict[str, Any] = {
        "timeout": timeout,
        "max_retries": max_retries,
        "http_client": http_client,
    }
    _check_fca(report, archived, download_dir=download_dir, options=options)
    _check_mirror(report, archived, options=options)
    return report


def _check_fca(
    report: DriftReport,
    archived: Mapping[ReportingPeriod, str],
    *,
    download_dir: Path,
    options: Mapping[str, Any],
) -> None:
    """Compare FCA's download page and zips with the archive.

    Zips are downloaded four at a time. A failed download is recorded, and the others
    are still compared.

    Parameters
    ----------
    report : DriftReport
        The report to fill in.
    archived : Mapping[ReportingPeriod, str]
        Each archived quarter's checksum.
    download_dir : pathlib.Path
        The folder each new or changed zip is saved to.
    options : Mapping[str, Any]
        The request options passed to every request.
    """
    try:
        catalog = FCAReleaseCatalog.from_fca(**options)
    except DownloadError as error:
        report.errors.append(f"FCA's download page could not be read: {error}")
        return
    report.unmatched = list(catalog.unmatched)
    report.missing = [_key(period) for period in archived if period not in catalog.urls]
    download_dir.mkdir(parents=True, exist_ok=True)

    def compare(period: ReportingPeriod) -> None:
        """Download one quarter's zip from FCA and compare it with the archive.

        A zip that differs is saved and checked before it is reported.

        Parameters
        ----------
        period : ReportingPeriod
            The quarter to compare.
        """
        url = catalog.url_for(period=period)
        try:
            body = fetch_bytes(url, **options)
        except DownloadError as error:
            report.errors.append(f"{period.label}: {error}")
            return
        checksum = _sha256_of_bytes(body)
        if archived.get(period) == checksum:
            return
        name = f"{fca_release_stem(period=period)}.zip"
        path = download_dir / name
        path.write_bytes(body)
        try:
            generated = check_release_zip(path)
        except (ZipArchiveUnpackingError, ValueError) as error:
            path.unlink()
            report.errors.append(f"{period.label}: FCA's file {url} {error}")
            return
        (report.new if period not in archived else report.changed).append(_key(period))
        report.files[_key(period)] = {
            "file": name,
            "sha256": checksum,
            "url": url,
            "generated": None if generated is None else generated.isoformat(),
        }

    # Each worker appends to the report's lists. list.append is atomic, and
    # the lists are sorted once every worker has finished.
    with ThreadPoolExecutor(max_workers=_DOWNLOAD_WORKERS) as pool:
        list(pool.map(compare, catalog.periods))
    report.new.sort()
    report.changed.sort()
    report.errors.sort()
    report.files = dict(sorted(report.files.items()))


def _check_mirror(
    report: DriftReport,
    archived: Mapping[ReportingPeriod, str],
    *,
    options: Mapping[str, Any],
) -> None:
    """Compare the GitHub mirror's manifest with the archive.

    A manifest that cannot be read is recorded as unavailable, not as an error.

    Parameters
    ----------
    report : DriftReport
        The report to fill in.
    archived : Mapping[ReportingPeriod, str]
        Each archived quarter's checksum.
    options : Mapping[str, Any]
        The request options passed to every request.
    """
    try:
        mirror = FCAReleaseCatalog.from_github(**options)
    except DownloadError as error:
        report.mirror = {"status": "unavailable", "error": str(error)}
        return
    missing = [_key(period) for period in archived if period not in mirror.urls]
    outdated = [
        _key(period)
        for period, checksum in archived.items()
        if period in mirror.sha256 and mirror.sha256[period] != checksum
    ]
    extra = [_key(period) for period in mirror.periods if period not in archived]
    in_step = not (missing or outdated or extra)
    report.mirror = {
        "status": "in_step" if in_step else "out_of_step",
        "snapshot": mirror.snapshot,
        "missing": missing,
        "outdated": outdated,
        "extra": extra,
    }


def apply(
    report: DriftReport, *, download_dir: Path, archive_dir: Path = _ARCHIVE_DIR
) -> list[Path]:
    """Copy the new and changed zips a check saved into the archive.

    Every saved zip is checked first, so either all are written or none are.

    Parameters
    ----------
    report : DriftReport
        The report the check wrote.
    download_dir : pathlib.Path
        The folder the check saved the zips to.
    archive_dir : pathlib.Path, optional
        The folder of release zips. Defaults to ``data/fca-call-report/``.

    Returns
    -------
    list[pathlib.Path]
        The archive files written, oldest quarter first.

    Raises
    ------
    ValueError
        If a saved zip is missing, or its SHA-256 differs from the report's.
        Nothing is written then.
    """
    sources = []
    for key, entry in report.files.items():
        source = download_dir / entry["file"]
        if not source.is_file() or sha256_of(source) != entry["sha256"]:
            raise ValueError(
                f"The zip saved for {key} is missing or differs from the report. "
                "Run the check again."
            )
        sources.append(source)
    written = []
    for source in sources:
        target = archive_dir / source.name
        # Copy to a temporary file beside the target, then rename it into
        # place, so the archive never holds a partly written zip.
        handle, temporary = tempfile.mkstemp(dir=archive_dir, suffix=".part")
        os.close(handle)
        shutil.copyfile(source, temporary)
        Path(temporary).replace(target)
        written.append(target)
        _log.info("Wrote %s.", target)
    return written


def render_summary(report: DriftReport) -> str:
    """Return a Markdown summary of a report, for the drift issue.

    Only sections with something in them are shown, and the mirror is always described.

    Parameters
    ----------
    report : DriftReport
        The report to summarize.

    Returns
    -------
    str
        The summary.
    """
    lines = [f"FCA drift check at {report.checked_at}: **{report.status}**.", ""]
    sections = [
        ("New quarters at FCA", [_describe_file(report, key) for key in report.new]),
        (
            "Quarters FCA has changed",
            [_describe_file(report, key) for key in report.changed],
        ),
        (
            "Quarters FCA no longer lists (left in the archive)",
            [_label(key) for key in report.missing],
        ),
        (
            "Zip links on FCA's page that name no release",
            list(report.unmatched),
        ),
        ("Errors", list(report.errors)),
    ]
    for title, items in sections:
        if items:
            lines += [f"#### {title}", "", *(f"- {item}" for item in items), ""]
    lines += ["#### The GitHub mirror", "", *_describe_mirror(report.mirror), ""]
    return "\n".join(lines)


def _describe_file(report: DriftReport, key: str) -> str:
    """Return one line naming a new or changed quarter's zip.

    The line is used in both the summary and the pull request description.

    Parameters
    ----------
    report : DriftReport
        The report holding the quarter.
    key : str
        The quarter's period end.

    Returns
    -------
    str
        The quarter, its file, and its generation date when there is one.
    """
    entry = report.files[key]
    generated = f", generated {entry['generated']}" if entry["generated"] else ""
    return f"{_label(key)} (`{entry['file']}`{generated})"


def _describe_mirror(mirror: Mapping[str, Any]) -> list[str]:
    """Return the lines describing the mirror comparison.

    Only the kinds of difference found are listed.

    Parameters
    ----------
    mirror : Mapping[str, Any]
        The report's mirror comparison.

    Returns
    -------
    list[str]
        One or more Markdown lines.
    """
    if mirror.get("status") == "unavailable":
        return [f"The mirror's manifest could not be read: {mirror['error']}"]
    if mirror.get("status") == "in_step":
        return [f"Snapshot {mirror['snapshot']} matches the archive."]
    lines = [f"Snapshot {mirror.get('snapshot')} differs from the archive."]
    for name, title in (
        ("missing", "Not on the mirror"),
        ("outdated", "Different on the mirror"),
        ("extra", "On the mirror but not in the archive"),
    ):
        if mirror.get(name):
            labels = ", ".join(_label(key) for key in mirror[name])
            lines.append(f"- {title}: {labels}")
    return lines


def changed_schedules(previous_dir: Path, current_dir: Path) -> list[str]:
    """Return the schedule metadata files that differ between two folders.

    Files are compared byte for byte.

    Parameters
    ----------
    previous_dir : pathlib.Path
        The shipped data folder before regeneration.
    current_dir : pathlib.Path
        The shipped data folder after regeneration.

    Returns
    -------
    list[str]
        The schedule roots whose file was added, removed, or changed.
    """
    previous = _files(previous_dir / "schedules")
    current = _files(current_dir / "schedules")
    names = set(previous) | set(current)
    return sorted(
        Path(name).stem for name in names if previous.get(name) != current.get(name)
    )


def _files(folder: Path) -> dict[str, bytes]:
    """Return every JSON file in a folder, by name.

    A missing folder counts as empty.

    Parameters
    ----------
    folder : pathlib.Path
        The folder to read.

    Returns
    -------
    dict[str, bytes]
        Each file's bytes, or nothing if the folder does not exist.
    """
    if not folder.is_dir():
        return {}
    return {path.name: path.read_bytes() for path in folder.glob("*.json")}


def changed_institutions(
    previous: FCAInstitutionRegistry, current: FCAInstitutionRegistry
) -> list[tuple[int, str | None]]:
    """Return the UNINUMs whose values changed in quarters already covered.

    A UNINUM is listed when, for any quarter the previous registry reports
    it, the current registry reports different values or does not report it
    at all. Quarters added by the regeneration are not compared.

    Parameters
    ----------
    previous : FCAInstitutionRegistry
        The registry before regeneration.
    current : FCAInstitutionRegistry
        The registry after regeneration.

    Returns
    -------
    list[tuple[int, str or None]]
        Each changed UNINUM with its short name in the newest quarter the
        previous registry reports it.
    """
    changed = []
    for uninum, institution in previous.items():
        quarters = [quarter for span in institution.periods for quarter in span]
        if uninum not in current or any(
            not _same_snapshot(institution, current[uninum], quarter)
            for quarter in quarters
        ):
            name = institution.as_of(period=quarters[-1]).short_name
            changed.append((uninum, name))
    return changed


def _same_snapshot(
    old: FCAInstitution, new: FCAInstitution, quarter: ReportingPeriod
) -> bool:
    """Return whether two histories of one UNINUM agree in one quarter.

    A quarter `new` does not report counts as a disagreement.

    Parameters
    ----------
    old : FCAInstitution
        The history before regeneration.
    new : FCAInstitution
        The history after regeneration.
    quarter : ReportingPeriod
        The quarter to compare. `old` reports the UNINUM in it.

    Returns
    -------
    bool
        ``True`` if `new` reports the same values in `quarter`.
    """
    try:
        return old.as_of(period=quarter) == new.as_of(period=quarter)
    except PeriodNotAvailableError:
        return False


def render_pull_request(
    report: DriftReport,
    *,
    issue: int,
    schedules: Sequence[str],
    institutions: Sequence[tuple[int, str | None]],
) -> str:
    """Return the description of the data pull request.

    The description follows the pull request template, and an empty list reads "None."

    Parameters
    ----------
    report : DriftReport
        The report the check wrote.
    issue : int
        The drift issue the pull request closes.
    schedules : Sequence[str]
        The schedule roots whose shipped metadata changed.
    institutions : Sequence[tuple[int, str or None]]
        The UNINUMs whose history changed in quarters already covered, with
        their short names.

    Returns
    -------
    str
        The description, in the layout of the pull request template.
    """

    def bullets(items: Iterable[str]) -> list[str]:
        """Return items as Markdown bullets, or "None." when there are none.

        Every list in the description reads the same way when it is empty.

        Parameters
        ----------
        items : Iterable[str]
            The items to list.

        Returns
        -------
        list[str]
            One line per item.
        """
        listed = [f"- {item}" for item in items]
        return listed or ["None."]

    # Each paragraph is built before the list, so the list holds one string
    # per line and a missing comma cannot join two lines without notice.
    why = (
        "The nightly FCA drift check found releases on FCA's download page that "
        "differ from `data/fca-call-report/`. This pull request copies FCA's "
        "files into the archive and regenerates the shipped schedule metadata "
        "and institution registry with the scripts in `scripts/`."
    )
    uninum_note = (
        "UNINUMs whose name or address history changed in quarters the registry "
        "already covered. A regeneration can backfill an institution's current "
        "values into earlier quarters. Check each one, and add an entry to "
        "`data/fca-institutions/overrides/registry.json` where the earlier "
        "value should be kept, then run "
        "`python scripts/generate_fca_institution_registry.py` again."
    )
    ai_usage = (
        "- [x] I did not use AI tools for this pull request. It was opened by "
        "the `fca-drift` workflow."
    )
    lines = [
        "#### Reference issues and plan",
        "",
        f"Closes #{issue}.",
        "",
        "#### What does this change, and why?",
        "",
        why,
        "",
        "New quarters:",
        "",
        *bullets(_describe_file(report, key) for key in report.new),
        "",
        "Quarters FCA has changed:",
        "",
        *bullets(_describe_file(report, key) for key in report.changed),
        "",
        "Schedules whose shipped metadata changed:",
        "",
        *bullets(f"`{root}`" for root in schedules),
        "",
        uninum_note,
        "",
        *bullets(
            f"{uninum} ({name})" if name else str(uninum)
            for uninum, name in institutions
        ),
        "",
        "#### Departures from the plan",
        "",
        "None.",
        "",
        "#### New dependencies",
        "",
        "None.",
        "",
        "#### AI usage",
        "",
        ai_usage,
        "",
        "#### Checklist",
        "",
        f"- [x] The title follows `[TAG] Imperative summary (#{issue})`.",
        "- [ ] The UNINUMs above are checked, and overrides are added where needed.",
        "- [ ] CI passes, including the exhaustive regression.",
        "- [x] The `run-exhaustive` label is added.",
        "",
    ]
    return "\n".join(lines)


def changelog_entry(report: DriftReport, *, issue: int) -> str:
    """Return the changelog entry for the data pull request.

    It names the new quarters and the quarters FCA replaced, in the changelog's style.

    Parameters
    ----------
    report : DriftReport
        The report the check wrote.
    issue : int
        The drift issue the pull request closes.

    Returns
    -------
    str
        One reStructuredText bullet, wrapped, ending in a newline.
    """
    sentences = []
    if report.new:
        sentences.append(f"The FCA archive now covers {_series(report.new)}.")
    if report.changed:
        holds = "It also holds" if report.new else "The FCA archive now holds"
        sentences.append(f"{holds} FCA's new versions of {_series(report.changed)}.")
    sentences.append(
        "The shipped schedule metadata and institution registry are regenerated "
        f"to match (:issue:`{issue}`)."
    )
    return (
        textwrap.fill(
            " ".join(sentences),
            width=_CHANGELOG_WIDTH,
            initial_indent="- ",
            subsequent_indent="  ",
            break_on_hyphens=False,
        )
        + "\n"
    )


def update_changelog(path: Path, entry: str, *, issue: int) -> None:
    """Add a data entry under the changelog's Unreleased heading.

    An entry this script wrote earlier for the same issue is replaced, so
    running it again on the same pull request leaves one entry.

    Parameters
    ----------
    path : pathlib.Path
        The changelog, ``docs/source/changelog.rst``.
    entry : str
        The entry `changelog_entry` returned.
    issue : int
        The drift issue the entry names.

    Raises
    ------
    ValueError
        If the changelog has no Unreleased heading.
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    try:
        heading = lines.index("Unreleased\n")
    except ValueError:
        raise ValueError(f"{path} has no 'Unreleased' heading.") from None
    marker = f"(:issue:`{issue}`)."
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if line.startswith("- The FCA archive now ")
            and _entry_end(lines, index, marker) is not None
        ),
        None,
    )
    if start is not None:
        end = _entry_end(lines, start, marker)
        assert end is not None  # noqa: S101
        lines[start : end + 1] = [entry]
    else:
        # The heading, its underline, and a blank line come before the first
        # entry.
        lines.insert(heading + 3, entry)
    path.write_text("".join(lines), encoding="utf-8", newline="\n")


def _entry_end(lines: Sequence[str], start: int, marker: str) -> int | None:
    """Return the last line of the changelog entry starting at a line.

    An entry runs on over every following line indented by two spaces.

    Parameters
    ----------
    lines : Sequence[str]
        The changelog's lines.
    start : int
        The index of the entry's first line.
    marker : str
        Text the entry's last line ends with.

    Returns
    -------
    int or None
        The index of the entry's last line, or ``None`` if the entry does not
        end with `marker`.
    """
    end = start
    while end + 1 < len(lines) and lines[end + 1].startswith("  "):
        end += 1
    return end if lines[end].rstrip().endswith(marker) else None


def describe(
    report: DriftReport,
    *,
    issue: int,
    previous_data: Path,
    data_dir: Path = _DATA_DIR,
    changelog: Path = _CHANGELOG,
) -> str:
    """Write the changelog entry and return the pull request description.

    The description compares the shipped data before and after regeneration.

    Parameters
    ----------
    report : DriftReport
        The report the check wrote.
    issue : int
        The drift issue the pull request closes.
    previous_data : pathlib.Path
        A copy of ``src/call_report/fca/data/`` from before regeneration.
    data_dir : pathlib.Path, optional
        The shipped data folder after regeneration. Defaults to
        ``src/call_report/fca/data/``.
    changelog : pathlib.Path, optional
        The changelog to update. Defaults to ``docs/source/changelog.rst``.

    Returns
    -------
    str
        The pull request description.
    """
    registry = Path("institutions") / "registry.json"
    institutions = changed_institutions(
        FCAInstitutionRegistry.from_json(
            text=(previous_data / registry).read_text(encoding="utf-8")
        ),
        FCAInstitutionRegistry.from_json(
            text=(data_dir / registry).read_text(encoding="utf-8")
        ),
    )
    schedules = changed_schedules(previous_data, data_dir)
    update_changelog(changelog, changelog_entry(report, issue=issue), issue=issue)
    return render_pull_request(
        report, issue=issue, schedules=schedules, institutions=institutions
    )


def _key(period: ReportingPeriod) -> str:
    """Return the key a report uses for a period, its period end.

    Period ends keep the report readable as plain JSON.

    Parameters
    ----------
    period : ReportingPeriod
        The period.

    Returns
    -------
    str
        The period end, such as ``"2026-06-30"``.
    """
    return period.period_end.isoformat()


def _label(key: str) -> str:
    """Return a report key's quarter label, such as ``"2026Q2"``.

    Labels are what the summary and the pull request show.

    Parameters
    ----------
    key : str
        A period end, such as ``"2026-06-30"``.

    Returns
    -------
    str
        The quarter's label.
    """
    return ReportingPeriod.from_period_end(value=key).label


def _series(keys: Sequence[str]) -> str:
    """Return quarters as an English list, such as ``"2026Q1 and 2026Q2"``.

    Three or more quarters get commas, with "and" before the last.

    Parameters
    ----------
    keys : Sequence[str]
        Period ends, oldest first.

    Returns
    -------
    str
        Their labels joined with commas and "and".
    """
    labels = [_label(key) for key in keys]
    if len(labels) <= 2:
        return " and ".join(labels)
    return f"{', '.join(labels[:-1])}, and {labels[-1]}"


def _sha256_of_bytes(data: bytes) -> str:
    """Return the SHA-256 of some bytes, as lower case hex.

    This is the form the mirror's manifest records.

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


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse this script's command-line arguments.

    Each command has its own options. See the module docstring.

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
    commands = parser.add_subparsers(dest="command", required=True)

    check_parser = commands.add_parser("check", help="Compare FCA and the mirror.")
    check_parser.add_argument("--report", type=Path, required=True)
    check_parser.add_argument("--summary", type=Path)
    check_parser.add_argument("--download-dir", type=Path, required=True)
    check_parser.add_argument("--archive-dir", type=Path, default=_ARCHIVE_DIR)
    check_parser.add_argument("--timeout", type=float, default=60.0)
    check_parser.add_argument("--max-retries", type=int, default=3)
    check_parser.add_argument(
        "--http-client", choices=("urllib", "curl_cffi"), default="urllib"
    )

    apply_parser = commands.add_parser("apply", help="Copy saved zips to the archive.")
    apply_parser.add_argument("--report", type=Path, required=True)
    apply_parser.add_argument("--download-dir", type=Path, required=True)
    apply_parser.add_argument("--archive-dir", type=Path, default=_ARCHIVE_DIR)

    describe_parser = commands.add_parser(
        "describe", help="Write the pull request description and changelog entry."
    )
    describe_parser.add_argument("--report", type=Path, required=True)
    describe_parser.add_argument("--issue", type=int, required=True)
    describe_parser.add_argument("--previous-data", type=Path, required=True)
    describe_parser.add_argument("--body", type=Path, required=True)
    describe_parser.add_argument("--data-dir", type=Path, default=_DATA_DIR)
    describe_parser.add_argument("--changelog", type=Path, default=_CHANGELOG)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run one of the script's commands.

    See the module docstring for what each command does.

    Parameters
    ----------
    argv : list[str], optional
        Arguments to parse. Defaults to ``sys.argv[1:]``.

    Returns
    -------
    int
        For ``check``, 0 when nothing differs, 1 when something does, and 2
        when the check could not finish. For ``apply`` and ``describe``, 0
        on success and 2 on failure.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parse_args(argv)
    if args.command == "check":
        report = check(
            download_dir=args.download_dir,
            archive_dir=args.archive_dir,
            timeout=args.timeout,
            max_retries=args.max_retries,
            http_client=args.http_client,
        )
        args.report.write_text(report.to_json(), encoding="utf-8", newline="\n")
        summary = render_summary(report)
        if args.summary is not None:
            args.summary.write_text(summary, encoding="utf-8", newline="\n")
        _log.info("%s", summary)
        return report.exit_code

    report = DriftReport.from_json(args.report.read_text(encoding="utf-8"))
    if args.command == "apply":
        try:
            apply(report, download_dir=args.download_dir, archive_dir=args.archive_dir)
        except ValueError as error:
            _log.error("%s", error)
            return EXIT_ERROR
        return EXIT_CLEAN

    body = describe(
        report,
        issue=args.issue,
        previous_data=args.previous_data,
        data_dir=args.data_dir,
        changelog=args.changelog,
    )
    args.body.write_text(body, encoding="utf-8", newline="\n")
    return EXIT_CLEAN


if __name__ == "__main__":
    raise SystemExit(main())
