"""Full-history regression test against every real, archived FCA release.

Unlike the rest of the test suite (hand-built, hermetic fixtures confirmed
against specific structural quirks -- see ``tests/conftest.py``), this test
deliberately exercises real FCA Call Report data: every quarterly release
shipped in ``data/fca-call-report/``, spanning FCA's entire known
publication history (``EARLIEST_PERIOD`` through ``LATEST_KNOWN_PERIOD``).
For each release, it drives the public :class:`~call_report.fca.FCACallReport`
interface to load metadata (the layout) and data for every schedule the
release contains, plus the institution roster -- catching real-world parsing
edge cases that synthetic fixtures can't reproduce.
"""

from __future__ import annotations

import pytest

from call_report.core import PeriodRange, ReportingPeriod
from call_report.exceptions import LayoutParseError, ScheduleNotFoundError
from call_report.fca import FCACallReport
from call_report.fca.catalog import EARLIEST_PERIOD, LATEST_KNOWN_PERIOD
from call_report.fca.institutions import INSTITUTIONS_ROOT, read_institutions
from call_report.fca.layout import FCALayout
from call_report.fca.reader import read_schedule_file
from call_report.fca.transport import PackagedArchiveTransport

ALL_KNOWN_PERIODS = tuple(PeriodRange(start=EARLIEST_PERIOD, end=LATEST_KNOWN_PERIOD))


@pytest.fixture(scope="session")
def archive_report() -> FCACallReport:
    """Build a fetched FCACallReport spanning FCA's entire known release history.

    Uses `PackagedArchiveTransport` against this repository's own checked-in
    ``data/fca-call-report/`` archive, so this test also serves as an
    end-to-end regression test of that transport against every real release
    it ships.

    Returns
    -------
    FCACallReport
        Already `fetch`-ed, so `releases_`/`schedules_` are populated.
    """
    transport = PackagedArchiveTransport()
    if not transport.archive_root.is_dir():
        pytest.skip(
            f"No archived FCA release zips found under {transport.archive_root}."
        )

    report = FCACallReport(
        start=EARLIEST_PERIOD.period_end,
        end=LATEST_KNOWN_PERIOD.period_end,
        transport=transport,
    )
    report.fetch()
    return report


@pytest.mark.parametrize(
    "period", ALL_KNOWN_PERIODS, ids=[period.label for period in ALL_KNOWN_PERIODS]
)
def test_release_metadata_and_data_load_for_every_schedule(
    archive_report: FCACallReport, period: ReportingPeriod
) -> None:
    """A real release's metadata and data load cleanly for every schedule it has.

    Loads each schedule's layout (metadata) via the public `get_layout`, then
    parses its data file with that layout via the public `read_schedule_file`
    -- the same production code path `FCACallReport.load` uses, but without
    `load`'s resilience (which would otherwise silently record a genuine
    regression in `errors_` rather than failing this test).
    """
    manifest = archive_report.releases_.get(period)
    assert manifest is not None, f"{period.label} was not resolved by fetch()."

    failures: list[str] = []
    for root, files in manifest.files.items():
        if root == INSTITUTIONS_ROOT:
            continue
        try:
            layout = archive_report.get_layout(schedule=root, period=period)
            # A single, non-None `period` always yields one FCALayout, never
            # the dict[ReportingPeriod, FCALayout] overload; assert this so
            # mypy narrows the type without masking a real behavior change.
            assert isinstance(layout, FCALayout)
            read_schedule_file(data_path=files.data_path, layout=layout)
        except (LayoutParseError, ScheduleNotFoundError) as error:
            failures.append(f"{root}: {error}")

    assert not failures, "; ".join(failures)


@pytest.mark.parametrize(
    "period", ALL_KNOWN_PERIODS, ids=[period.label for period in ALL_KNOWN_PERIODS]
)
def test_release_institutions_load(
    archive_report: FCACallReport, period: ReportingPeriod
) -> None:
    """A real release's institution roster loads cleanly."""
    manifest = archive_report.releases_.get(period)
    assert manifest is not None, f"{period.label} was not resolved by fetch()."
    assert INSTITUTIONS_ROOT in manifest.files, (
        f"{period.label} has no {INSTITUTIONS_ROOT} layout/data file pair."
    )

    read_institutions(release_dir=manifest.release_dir)
