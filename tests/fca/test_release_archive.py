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

The full history above only ever runs under the default (pandas) dataframe
backend. Backend choice only changes the final materialization step
(:func:`~call_report.core._backend.build_frame`/``finalize``), not the
parsing logic itself, so re-running all ~105 releases under polars and
pyarrow too would mostly re-test the same parsing path for little extra
signal. Instead, a small, deterministically sampled subset of releases is
also run under every configured backend, to catch backend-specific
materialization issues (dtype inference, null handling, schema quirks)
without tripling the full suite's runtime.
"""

from __future__ import annotations

import random

import pytest

from call_report.config import config_context
from call_report.core import PeriodRange, ReportingPeriod
from call_report.exceptions import LayoutParseError, ScheduleNotFoundError
from call_report.fca import FCACallReport
from call_report.fca.catalog import EARLIEST_PERIOD, LATEST_KNOWN_PERIOD
from call_report.fca.institutions import INSTITUTIONS_ROOT, read_institutions
from call_report.fca.layout import FCALayout
from call_report.fca.reader import read_schedule_file
from call_report.fca.transport import PackagedArchiveTransport

ALL_KNOWN_PERIODS = tuple(PeriodRange(start=EARLIEST_PERIOD, end=LATEST_KNOWN_PERIOD))

# FCA's download URL/file conventions changed in 2015 (see
# call_report.fca.catalog); pre/post-2015 is a reasonable proxy for
# "structurally different eras" of the archive when stratifying a sample.
_MODERN_ERA_FIRST_YEAR = 2015
_CROSS_BACKEND_SAMPLE_SIZE = 20
_CROSS_BACKEND_SEED = 726026  # fixed so the sample is stable across CI runs
CROSS_BACKEND_BACKENDS = ("pandas", "polars", "pyarrow")


def _stratified_sample(
    *, periods: tuple[ReportingPeriod, ...], size: int, seed: int
) -> tuple[ReportingPeriod, ...]:
    """Deterministically sample periods, split across legacy/modern eras.

    Always includes the earliest and latest period as boundary cases, then
    fills the remainder with an even, seeded random split between periods
    before and from :data:`_MODERN_ERA_FIRST_YEAR`.

    Parameters
    ----------
    periods : tuple[ReportingPeriod, ...]
        The full, chronologically ordered population to sample from.
    size : int
        Total number of periods to return, including the boundary periods.
    seed : int
        Fixed seed so the sample is reproducible across test runs.

    Returns
    -------
    tuple[ReportingPeriod, ...]
        The sampled periods, in chronological order.
    """
    rng = random.Random(seed)  # noqa: S311 -- deterministic test sampling, not security
    boundary = {periods[0], periods[-1]}
    legacy = [
        p for p in periods if p.year < _MODERN_ERA_FIRST_YEAR and p not in boundary
    ]
    modern = [
        p for p in periods if p.year >= _MODERN_ERA_FIRST_YEAR and p not in boundary
    ]
    remaining = size - len(boundary)
    n_legacy = remaining // 2
    n_modern = remaining - n_legacy
    sampled = set(boundary)
    sampled.update(rng.sample(legacy, min(n_legacy, len(legacy))))
    sampled.update(rng.sample(modern, min(n_modern, len(modern))))
    return tuple(sorted(sampled))


CROSS_BACKEND_SAMPLE_PERIODS = _stratified_sample(
    periods=ALL_KNOWN_PERIODS, size=_CROSS_BACKEND_SAMPLE_SIZE, seed=_CROSS_BACKEND_SEED
)


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


def _assert_all_schedules_load(
    *, report: FCACallReport, period: ReportingPeriod
) -> None:
    """Load every schedule's layout (metadata) and data file for `period`.

    Drives the same production code path `FCACallReport.load` uses --
    `get_layout` then `read_schedule_file` -- but without `load`'s
    resilience (which would otherwise silently record a genuine regression
    in `errors_` rather than failing the caller's test).

    Parameters
    ----------
    report : FCACallReport
        Already `fetch`-ed report to load schedules from.
    period : ReportingPeriod
        The release period to load every schedule for.
    """
    manifest = report.releases_.get(period)
    assert manifest is not None, f"{period.label} was not resolved by fetch()."

    failures: list[str] = []
    for root, files in manifest.files.items():
        if root == INSTITUTIONS_ROOT:
            continue
        try:
            layout = report.get_layout(schedule=root, period=period)
            # A single, non-None `period` always yields one FCALayout, never
            # the dict[ReportingPeriod, FCALayout] overload; assert this so
            # mypy narrows the type without masking a real behavior change.
            assert isinstance(layout, FCALayout)
            read_schedule_file(data_path=files.data_path, layout=layout)
        except (LayoutParseError, ScheduleNotFoundError) as error:
            failures.append(f"{root}: {error}")

    assert not failures, "; ".join(failures)


def _assert_institutions_load(
    *, report: FCACallReport, period: ReportingPeriod
) -> None:
    """Load the institution roster for `period`.

    Parameters
    ----------
    report : FCACallReport
        Already `fetch`-ed report to load the institution roster from.
    period : ReportingPeriod
        The release period to load the institution roster for.
    """
    manifest = report.releases_.get(period)
    assert manifest is not None, f"{period.label} was not resolved by fetch()."
    assert INSTITUTIONS_ROOT in manifest.files, (
        f"{period.label} has no {INSTITUTIONS_ROOT} layout/data file pair."
    )

    read_institutions(release_dir=manifest.release_dir)


@pytest.mark.parametrize(
    "period", ALL_KNOWN_PERIODS, ids=[period.label for period in ALL_KNOWN_PERIODS]
)
def test_release_metadata_and_data_load_for_every_schedule(
    archive_report: FCACallReport, period: ReportingPeriod
) -> None:
    """A real release's metadata and data load cleanly for every schedule it has."""
    _assert_all_schedules_load(report=archive_report, period=period)


@pytest.mark.parametrize(
    "period", ALL_KNOWN_PERIODS, ids=[period.label for period in ALL_KNOWN_PERIODS]
)
def test_release_institutions_load(
    archive_report: FCACallReport, period: ReportingPeriod
) -> None:
    """A real release's institution roster loads cleanly."""
    _assert_institutions_load(report=archive_report, period=period)


@pytest.mark.parametrize("backend", CROSS_BACKEND_BACKENDS)
@pytest.mark.parametrize(
    "period",
    CROSS_BACKEND_SAMPLE_PERIODS,
    ids=[period.label for period in CROSS_BACKEND_SAMPLE_PERIODS],
)
def test_release_metadata_and_data_load_across_backends(
    archive_report: FCACallReport, period: ReportingPeriod, backend: str
) -> None:
    """A sampled release's schedules load cleanly under every configured backend."""
    with config_context(dataframe_backend=backend):
        _assert_all_schedules_load(report=archive_report, period=period)


@pytest.mark.parametrize("backend", CROSS_BACKEND_BACKENDS)
@pytest.mark.parametrize(
    "period",
    CROSS_BACKEND_SAMPLE_PERIODS,
    ids=[period.label for period in CROSS_BACKEND_SAMPLE_PERIODS],
)
def test_release_institutions_load_across_backends(
    archive_report: FCACallReport, period: ReportingPeriod, backend: str
) -> None:
    """A sampled release's institution roster loads cleanly under every backend."""
    with config_context(dataframe_backend=backend):
        _assert_institutions_load(report=archive_report, period=period)
