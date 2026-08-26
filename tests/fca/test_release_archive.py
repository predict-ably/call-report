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
without tripling the full suite's runtime. A separate, smaller sample of
releases spanning the full history goes one step further and checks that
all three backends parse the exact same values for every schedule, not
just that they load without error.
"""

from __future__ import annotations

import math
import random
from typing import Any

import narwhals as nw
import pytest

from call_report.config import config_context, get_config
from call_report.core import FieldSchema, PeriodRange, ReportingPeriod
from call_report.exceptions import LayoutParseError, ScheduleNotFoundError
from call_report.fca import (
    FCACallReport,
    FCASchedule,
    convert_long_format_to_wide_format,
    convert_wide_format_to_long_format,
    get_fca_file_metadata,
    get_institutions_file_metadata,
)
from call_report.fca.catalog import EARLIEST_PERIOD, LATEST_KNOWN_PERIOD
from call_report.fca.institutions import INSTITUTIONS_ROOT, read_institutions
from call_report.fca.layout import FCALayout
from call_report.fca.reader import read_schedule_file
from call_report.fca.transport import PackagedArchiveTransport
from tests.helpers import ALL_BACKENDS

# This whole module drives real archived releases end to end and dominates
# the suite's runtime. Marked so a contributor iterating on unit tests can
# run `pytest -m "not slow"`; CI still runs everything.
pytestmark = pytest.mark.slow

ALL_KNOWN_PERIODS = tuple(PeriodRange(start=EARLIEST_PERIOD, end=LATEST_KNOWN_PERIOD))

# FCA's download URL/file conventions changed in 2015 (see
# call_report.fca.catalog); pre/post-2015 is a reasonable proxy for
# "structurally different eras" of the archive when stratifying a sample.
_MODERN_ERA_FIRST_YEAR = 2015
_CROSS_BACKEND_SAMPLE_SIZE = 20
_CROSS_BACKEND_SEED = 726026  # fixed so the sample is stable across CI runs


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

_EQUALITY_CHECK_SAMPLE_SIZE = 4


def _evenly_spaced(
    *, periods: tuple[ReportingPeriod, ...], size: int
) -> tuple[ReportingPeriod, ...]:
    """Return `size` periods evenly spaced across `periods`, including both ends.

    Deterministic, unlike `_stratified_sample`: used where a handful of
    periods just needs to span the full known release history, not
    represent it statistically.

    Parameters
    ----------
    periods : tuple[ReportingPeriod, ...]
        The full, chronologically ordered population to sample from.
    size : int
        Number of periods to return.

    Returns
    -------
    tuple[ReportingPeriod, ...]
        The sampled periods, in chronological order.
    """
    last_index = len(periods) - 1
    indices = {round(i * last_index / (size - 1)) for i in range(size)}
    return tuple(periods[i] for i in sorted(indices))


EQUALITY_CHECK_PERIODS = _evenly_spaced(
    periods=ALL_KNOWN_PERIODS, size=_EQUALITY_CHECK_SAMPLE_SIZE
)

# FCA ships a zero-byte RCO data file for its first sixteen quarters, so an
# empty frame is the correct parse of those releases rather than a silent
# failure. RCO carries rows from 2004Q1 onward, and every other schedule in
# every other release has rows. Pinning the exception means a schedule that
# newly parses to nothing fails, instead of passing as a vacuous comparison
# of two empty frames.
KNOWN_EMPTY_SCHEDULES = frozenset(
    (period, "RCO") for period in PeriodRange(start="2000-03-31", end="2003-12-31")
)

# `period` holds a datetime.date under every backend, but pandas' default
# dtypes have no date type, so it lands in an Object column while polars
# and pyarrow report Date. The values are identical, the declared type is
# not. Pinned by column name rather than allowed for any column, so an
# Object dtype appearing anywhere else still fails.
OBJECT_DATE_COLUMNS = frozenset({"period"})

# `to_long_format` builds its `value` column by concatenating schedules
# whose source columns have different dtypes. polars and pyarrow coerce
# the result to Float64; pandas falls back to Object. `to_wide_format`
# pivots that column, so every value column it produces inherits the
# same Object dtype under pandas. The values are numbers under all three
# backends, so the row comparisons still hold, but pandas callers get an
# object-typed frame where the other backends get a numeric one.
#
# The reshape comparisons therefore pass allow_pandas_object=True. The
# raw schedule comparisons do not, so this stays confined to the
# reshaped output rather than becoming a blanket allowance.
RESHAPE_PANDAS_OBJECT_DTYPES = True

# `load` raises under polars for these schedules. Each has at least one
# column that is entirely null in one release and populated in another,
# which polars types Null against Int64 and refuses to vstack, so the
# cross-period concat fails. See issue #43.
#
# Pinned as a set rather than as xfail markers so that the count cannot
# drift unnoticed in either direction: a sixteenth schedule breaking
# fails the test, and so does fixing #43, which forces this constant and
# the branch reading it to be deleted as part of that fix.
POLARS_LOAD_FAILURES = frozenset(
    {
        "RC",
        "RC1",
        "RCB",
        "RCB4",
        "RCB5",
        "RCF1",
        "RCG",
        "RCH",
        "RCI",
        "RCO",
        "RCR4",
        "RI",
        "RIB",
        "RID",
        "RIE",
    }
)

# The same columns that make polars raise also make the stacked schema
# disagree between pandas and pyarrow. A column that is all null in the
# early releases and populated later resolves to String under pandas'
# concat and Int64 under pyarrow's, so pandas hands back a string column
# for numeric data. Also issue #43.
#
# RCO is the one member of the set above that does not reach this: its
# empty releases are empty frames rather than all-null columns, so
# nothing survives to disagree about. Expressed as a difference rather
# than a second hand-maintained list, so fixing #43 empties both at once.
LOAD_SCHEMA_DIVERGENCE = POLARS_LOAD_FAILURES - {"RCO"}


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


def _fetched_full_history_report() -> FCACallReport:
    """Build and fetch a fresh full-history report.

    The `archive_report` fixture is session-scoped, and `load` records
    parsing problems by appending to `errors_`. A test that asserts on
    `errors_` therefore needs an instance of its own, or it would see
    whatever an earlier test left behind.

    Returns
    -------
    FCACallReport
        Already `fetch`-ed, spanning the whole known release history.
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


@pytest.fixture(scope="session")
def loadable_archive_report() -> FCACallReport:
    """Build a full-history report dedicated to the per-schedule `load` tests.

    Separate from `archive_report` because `load` appends to `errors_`.
    Keeping the two apart means a per-schedule test can assert on
    `errors_` without seeing what an unrelated test left there.

    Returns
    -------
    FCACallReport
        Already `fetch`-ed, spanning the whole known release history.
    """
    return _fetched_full_history_report()


@pytest.fixture(scope="session")
def backend_archive_reports() -> dict[str, FCACallReport]:
    """One fetched full-history report per dataframe backend.

    `fetch` only resolves file paths, so it costs the same under every
    backend, but building these once rather than per test keeps the
    cross-backend `load` comparison from re-fetching the whole archive
    for each of the 37 schedules.

    Returns
    -------
    dict[str, FCACallReport]
        Backend name to an already `fetch`-ed report.
    """
    reports: dict[str, FCACallReport] = {}
    for backend in ALL_BACKENDS:
        with config_context(dataframe_backend=backend):
            reports[backend] = _fetched_full_history_report()
    return reports


def _assert_schedule_loads(
    *, report: FCACallReport, schedule: FCASchedule, backend: str
) -> None:
    """Load one schedule across every period it appears in, via the public API.

    `load` stacks the schedule across periods and records a period it
    could not parse in `errors_` rather than raising, so this checks both
    that rows came back and that nothing was quietly skipped for this
    schedule.

    The loaded columns are also checked against the packaged schedule
    metadata that `get_fca_file_metadata` serves. That metadata is
    generated by `scripts/generate_fca_schedule_metadata.py` and shipped
    to users as the authoritative description of each schedule, so it
    silently going stale against what the loader actually produces would
    mislead every caller who reads it.

    Parameters
    ----------
    report : FCACallReport
        Already `fetch`-ed report to load from.
    schedule : FCASchedule
        The schedule to load.
    backend : str
        The dataframe backend in force, for failure messages.
    """
    frame = report.load(schedule=schedule)
    assert len(_eager(frame)) > 0, (
        f"{backend}: load(schedule={schedule.value!r}) returned 0 rows."
    )

    recorded = [issue for issue in report.errors_ if issue.schedule is schedule]
    assert not recorded, (
        f"{backend}: load(schedule={schedule.value!r}) recorded "
        f"{len(recorded)} issue(s): {recorded[:3]}"
    )

    _assert_matches_packaged_metadata(
        frame=_eager(frame), schedule=schedule, backend=backend
    )


def _assert_matches_packaged_metadata(
    *, frame: nw.DataFrame[Any], schedule: FCASchedule, backend: str
) -> None:
    """Check a loaded schedule against the metadata the package ships for it.

    `load` adds a ``period`` column to identify which release each row
    came from, so that column is excluded before comparing against the
    declared fields.

    Parameters
    ----------
    frame : narwhals.DataFrame
        The frame `load` returned for `schedule`.
    schedule : FCASchedule
        The schedule the frame was loaded for.
    backend : str
        The dataframe backend in force, for failure messages.
    """
    _assert_matches_declared_schema(
        frame=frame,
        declared_schema=get_fca_file_metadata(schedule=schedule).file_schema,
        label=f"{backend}:load({schedule.value})",
        backend=backend,
    )


def _assert_matches_declared_schema(
    *,
    frame: nw.DataFrame[Any],
    declared_schema: FieldSchema,
    label: str,
    backend: str,
) -> None:
    """Check a loaded frame against the field schema the package declares for it.

    Column names must match the declared fields exactly, ignoring the
    ``period`` column the public loaders add to identify which release a
    row came from. Each column's dtype must match one of the dtypes the
    field is declared to have taken, under the same equivalence
    `_assert_schemas_equivalent` uses, plus one pandas-only allowance
    described below.

    Parameters
    ----------
    frame : narwhals.DataFrame
        The loaded frame to check.
    declared_schema : FieldSchema
        The packaged schema describing what the frame should contain.
    label : str
        Identifies the call in failure messages.
    backend : str
        The dataframe backend in force.
    """
    declared = set(declared_schema.names)
    loaded = set(frame.columns) - {"period"}
    assert loaded == declared, (
        f"{label}: columns disagree with the packaged metadata. Loaded but "
        f"not declared: {sorted(loaded - declared)}. Declared but not "
        f"loaded: {sorted(declared - loaded)}."
    )

    actual_schema = frame.collect_schema()
    uninformative = _uninformative_columns(frame)
    for name in declared_schema.names:
        actual = actual_schema[name]
        if name in uninformative:
            continue
        # A field's declared dtype can change over the archive's history,
        # and a stacked frame spans those versions, so any declared
        # version is acceptable.
        declared_dtypes = {
            str(version.dtype) for version in declared_schema[name].versions
        }
        if str(actual) in declared_dtypes:
            continue
        if any({str(actual), d} == {"Int64", "Float64"} for d in declared_dtypes):
            continue
        if backend == "pandas" and str(actual) in {"String", "Object"}:
            # Issue #43. A column that is all null in the early releases
            # and populated later resolves to String, or to Object, under
            # pandas' concat, so pandas hands back a non-numeric column
            # where the metadata declares an integer one.
            continue
        raise AssertionError(
            f"{label} column {name!r}: loaded dtype {actual} is not any of the "
            f"declared dtypes {sorted(declared_dtypes)}."
        )


def _assert_public_load_api(*, report: FCACallReport, backend: str) -> None:
    """Drive the public load API over the whole archive and check what it returned.

    `load_all` and `load_institutions` skip a period or schedule that
    fails to parse and record the reason in `errors_` rather than
    raising, so a caller who never inspects `errors_` sees a partial
    result as a successful one. Asserting the tuple is empty is what
    turns that resilience into a regression check.

    Parameters
    ----------
    report : FCACallReport
        A freshly fetched report spanning the whole release history.
    backend : str
        The dataframe backend in force, for failure messages.
    """
    loaded = report.load_all()
    assert loaded, f"{backend}: load_all() returned no schedules."
    # `load_all` omits a schedule that failed in every period rather than
    # raising, so checking the result is non-empty is not enough: it has
    # to carry every schedule `fetch` discovered.
    assert set(loaded) == set(report.schedules_), (
        f"{backend}: load_all() dropped "
        f"{sorted(s.value for s in set(report.schedules_) - set(loaded))}."
    )
    for schedule, frame in loaded.items():
        assert len(_eager(frame)) > 0, (
            f"{backend}: load_all() returned 0 rows for {schedule}."
        )

    institutions = report.load_institutions()
    assert len(_eager(institutions)) > 0, (
        f"{backend}: load_institutions() returned 0 rows."
    )

    assert report.errors_ == (), (
        f"{backend}: the public load API recorded "
        f"{len(report.errors_)} issue(s): {report.errors_[:5]}"
    )


def _assert_all_schedules_load(
    *, report: FCACallReport, period: ReportingPeriod
) -> None:
    """Load every schedule's layout (metadata) and data file for `period`.

    Drives the same production code path `FCACallReport.load` uses --
    `get_layout` then `read_schedule_file` -- but without `load`'s
    resilience (which would otherwise silently record a genuine regression
    in `errors_` rather than failing the caller's test).

    Each schedule's row count is checked against
    `KNOWN_EMPTY_SCHEDULES`. Parsing a file to zero rows raises nothing,
    so without this a reader that stopped returning data would still look
    like a clean load.

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
            frame = read_schedule_file(data_path=files.data_path, layout=layout)
        except (LayoutParseError, ScheduleNotFoundError) as error:
            failures.append(f"{root}: {error}")
            continue

        rows = len(_eager(frame))
        expected_empty = (period, root) in KNOWN_EMPTY_SCHEDULES
        if rows == 0 and not expected_empty:
            failures.append(f"{root}: parsed 0 rows")
        elif rows > 0 and expected_empty:
            failures.append(
                f"{root}: parsed {rows} rows, but FCA ships an empty file here"
            )

    assert not failures, "; ".join(failures)


def _assert_institutions_load(
    *, report: FCACallReport, period: ReportingPeriod
) -> None:
    """Load the institution roster for `period`, and check it has institutions.

    Every release FCA has published names at least 64 institutions, so an
    empty roster is a parsing failure rather than a quarter with no
    filers.

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

    institutions = _eager(read_institutions(release_dir=manifest.release_dir))
    assert len(institutions) > 0, f"{period.label}: institution roster parsed 0 rows."

    _assert_matches_declared_schema(
        frame=institutions,
        declared_schema=get_institutions_file_metadata().file_schema,
        label=f"institutions:{period.label}",
        backend=get_config()["dataframe_backend"],
    )


def _eager(native_frame: Any) -> nw.DataFrame[Any]:
    """Return any backend's native frame as an eager narwhals DataFrame."""
    frame = nw.from_native(native_frame)
    if isinstance(frame, nw.LazyFrame):
        return frame.collect()
    return frame


def _native_rows(native_frame: Any) -> list[dict[str, Any]]:
    """Convert any backend's native frame into a backend-agnostic list of row dicts."""
    return _eager(native_frame).rows(named=True)


def _uninformative_columns(frame: nw.DataFrame[Any]) -> frozenset[str]:
    """Return the columns this frame gives a backend nothing to infer a dtype from.

    A column is uninformative when the frame has no rows, or when every
    entry in it is null. Backends disagree about what to call such a
    column, because nothing in the data decides it.

    Parameters
    ----------
    frame : narwhals.DataFrame
        The frame to inspect.

    Returns
    -------
    frozenset[str]
        Names of the columns carrying no values.
    """
    if len(frame) == 0:
        return frozenset(frame.columns)
    return frozenset(name for name in frame.columns if frame[name].is_null().all())


def _assert_schemas_equivalent(
    *,
    reference: nw.DataFrame[Any],
    other: nw.DataFrame[Any],
    label: str,
    allow_pandas_object: bool = False,
    ignore_column_order: bool = False,
) -> None:
    """Assert two backends built the same schema for one frame.

    Column names and their order must match exactly. Dtypes must match
    too, with three documented exceptions:

    - A column with no values to infer from (every entry null, or the
      frame has no rows) may carry any dtype. polars and pyarrow report
      ``Unknown``, pandas falls back to ``String`` or ``Float64``.
    - An integer column holding at least one null is ``Int64`` under
      polars and pyarrow, which have a nullable integer, and ``Float64``
      under pandas, whose default dtypes do not.
    - A column named in `OBJECT_DATE_COLUMNS` may be ``Object`` under
      pandas against ``Date`` elsewhere.

    Any other disagreement fails.

    Parameters
    ----------
    reference : narwhals.DataFrame
        The pandas-backend frame to compare against.
    other : narwhals.DataFrame
        Another backend's frame for the same schedule and period.
    label : str
        Identifies the backend/schedule combination in failure messages.
    allow_pandas_object : bool, default False
        Also accept an ``Object`` dtype under pandas against any dtype
        elsewhere. Set only by the reshape comparisons, whose pandas
        output is object-typed throughout. See
        `RESHAPE_PANDAS_OBJECT_DTYPES`.
    """
    reference_schema = reference.collect_schema()
    other_schema = other.collect_schema()
    if ignore_column_order:
        assert set(other_schema) == set(reference_schema), (
            f"{label}: columns {sorted(other_schema)}, expected "
            f"{sorted(reference_schema)}."
        )
    else:
        assert list(other_schema) == list(reference_schema), (
            f"{label}: columns {list(other_schema)}, expected {list(reference_schema)}."
        )

    uninformative = _uninformative_columns(reference) | _uninformative_columns(other)
    for name in reference_schema:
        reference_dtype = reference_schema[name]
        other_dtype = other_schema[name]
        if reference_dtype == other_dtype or name in uninformative:
            continue
        pair = {str(reference_dtype), str(other_dtype)}
        if pair == {"Object", "Date"} and name in OBJECT_DATE_COLUMNS:
            continue
        if allow_pandas_object and "Object" in pair:
            continue
        assert pair == {"Int64", "Float64"}, (
            f"{label} column {name!r}: dtype {other_dtype} is not equivalent to "
            f"{reference_dtype}."
        )


def _is_missing(value: object) -> bool:
    """Return True for a missing value, however the active backend represents it.

    pandas represents a missing numeric value as NaN (a float); polars and
    pyarrow use an actual None -- both count as "missing" here.
    """
    return value is None or (isinstance(value, float) and math.isnan(value))


def _load_schedule_frames(
    *,
    report: FCACallReport,
    period: ReportingPeriod,
    schedule_roots: tuple[str, ...],
    backend: str,
) -> dict[str, nw.DataFrame[Any]]:
    """Load every schedule in `schedule_roots` for `period`, under `backend`.

    Returns frames rather than rows so a caller can compare schemas as
    well as values. Row dicts alone lose every dtype.

    Parameters
    ----------
    report : FCACallReport
        Already `fetch`-ed report to load schedules from.
    period : ReportingPeriod
        The release period to load schedules for.
    schedule_roots : tuple[str, ...]
        The schedule roots (excluding the institution roster) to load.
    backend : str
        The dataframe backend to configure while loading.

    Returns
    -------
    dict[str, narwhals.DataFrame]
        Each schedule root's data, as a backend-agnostic eager frame.
    """
    manifest = report.releases_.get(period)
    assert manifest is not None, f"{period.label} was not resolved by fetch()."

    frames_by_root: dict[str, nw.DataFrame[Any]] = {}
    with config_context(dataframe_backend=backend):
        for root in schedule_roots:
            files = manifest.files[root]
            layout = report.get_layout(schedule=root, period=period)
            assert isinstance(layout, FCALayout)
            frame = read_schedule_file(data_path=files.data_path, layout=layout)
            frames_by_root[root] = _eager(frame)
    return frames_by_root


def _assert_frames_equal(
    *,
    reference: nw.DataFrame[Any],
    other: nw.DataFrame[Any],
    label: str,
    allow_pandas_object: bool = False,
) -> None:
    """Assert two backends produced the same schema and the same values.

    Parameters
    ----------
    reference : narwhals.DataFrame
        The pandas-backend frame to compare against.
    other : narwhals.DataFrame
        Another backend's frame for the same schedule and period.
    label : str
        Identifies the backend/schedule combination in failure messages.
    allow_pandas_object : bool, default False
        Also accept an ``Object`` dtype under pandas against any dtype
        elsewhere. Set only by the reshape comparisons, whose pandas
        output is object-typed throughout. See
        `RESHAPE_PANDAS_OBJECT_DTYPES`.
    """
    _assert_schemas_equivalent(
        reference=reference,
        other=other,
        label=label,
        allow_pandas_object=allow_pandas_object,
    )
    _assert_rows_equal(
        reference=reference.rows(named=True),
        other=other.rows(named=True),
        label=label,
    )


def _assert_rows_equal(
    *, reference: list[dict[str, Any]], other: list[dict[str, Any]], label: str
) -> None:
    """Assert two backends' row-lists carry the same data for one schedule.

    Parameters
    ----------
    reference : list[dict[str, Any]]
        The pandas-backend rows to compare against.
    other : list[dict[str, Any]]
        Another backend's rows for the same schedule and period.
    label : str
        Identifies the backend/schedule combination in failure messages.
    """
    assert len(other) == len(reference), (
        f"{label}: {len(other)} rows, expected {len(reference)}."
    )
    for index, (ref_row, other_row) in enumerate(zip(reference, other, strict=True)):
        assert other_row.keys() == ref_row.keys(), (
            f"{label} row {index}: column mismatch."
        )
        for key, ref_value in ref_row.items():
            other_value = other_row[key]
            if _is_missing(ref_value) and _is_missing(other_value):
                continue
            assert other_value == ref_value, (
                f"{label} row {index} column {key!r}: {other_value!r} != {ref_value!r}"
            )


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


def test_public_load_api_works_across_the_whole_archive() -> None:
    """`load_all` and `load_institutions` return data for the whole archive.

    The rest of this module reaches past the object-oriented interface,
    calling `get_layout` and `read_schedule_file` directly, so that a
    parsing regression fails the test rather than being recorded in
    `errors_`. That leaves `load`, `load_all`, and `load_institutions`,
    the methods callers actually use, untested against real data. This
    covers them, and asserts `errors_` is empty so their resilience
    cannot hide a regression.
    """
    report = _fetched_full_history_report()
    _assert_public_load_api(report=report, backend="pandas")


@pytest.mark.parametrize(
    "schedule", tuple(FCASchedule), ids=[schedule.value for schedule in FCASchedule]
)
def test_every_schedule_loads_through_the_public_load_method(
    loadable_archive_report: FCACallReport, schedule: FCASchedule
) -> None:
    """`load` returns stacked data for each schedule across the whole history.

    `load_all` covers the same ground in aggregate, but one failing
    schedule there is one failing test. Parametrizing names the schedule
    that broke, and covers the `ScheduleNotFoundError` path for any
    schedule the archive never contains.
    """
    if schedule not in loadable_archive_report.schedules_:
        with pytest.raises(ScheduleNotFoundError):
            loadable_archive_report.load(schedule=schedule)
        return
    _assert_schedule_loads(
        report=loadable_archive_report, schedule=schedule, backend="pandas"
    )


@pytest.mark.parametrize(
    "schedule", tuple(FCASchedule), ids=[schedule.value for schedule in FCASchedule]
)
def test_load_agrees_across_backends(
    loadable_archive_report: FCACallReport,
    backend_archive_reports: dict[str, FCACallReport],
    schedule: FCASchedule,
) -> None:
    """`load` returns the same schema and row count under every backend.

    The other cross-backend comparisons in this module reach past the
    interface, calling `get_layout` and `read_schedule_file` directly, so
    they compare parsing but never `load`'s own stacking of a schedule
    across periods. This compares what a caller actually receives.

    Values are compared per release by
    `test_exhaustive_every_release_matches_across_backends`. What is
    specific to `load` is the stacked result's shape and schema, so that
    is what this checks, avoiding a cell-by-cell comparison of a frame
    with tens of thousands of rows.
    """
    if schedule not in loadable_archive_report.schedules_:
        pytest.skip(f"{schedule.value} is not present in the archive.")

    frames: dict[str, nw.DataFrame[Any]] = {}
    for backend, report in backend_archive_reports.items():
        with config_context(dataframe_backend=backend):
            if backend == "polars" and schedule.value in POLARS_LOAD_FAILURES:
                # Issue #43. Asserting it still raises is what makes the
                # pinned set self-correcting: fixing #43 fails here.
                with pytest.raises(Exception, match=r"(?i)schema|null|int64"):
                    report.load(schedule=schedule)
                continue
            frames[backend] = _eager(report.load(schedule=schedule))

    reference = frames["pandas"]
    for backend, frame in frames.items():
        if backend == "pandas":
            continue
        assert len(frame) == len(reference), (
            f"{backend}: load(schedule={schedule.value!r}) returned "
            f"{len(frame)} rows, pandas returned {len(reference)}."
        )
        if schedule.value in LOAD_SCHEMA_DIVERGENCE:
            # Issue #43. Assert the disagreement is still there, so that
            # fixing it fails this test and forces the pin out.
            with pytest.raises(AssertionError):
                _assert_schemas_equivalent(
                    reference=reference,
                    other=frame,
                    label=f"{backend}:{schedule.value}",
                )
            continue
        _assert_schemas_equivalent(
            reference=reference, other=frame, label=f"{backend}:{schedule.value}"
        )


def _load_institutions_frames(
    *, report: FCACallReport, period: ReportingPeriod
) -> dict[str, nw.DataFrame[Any]]:
    """Load one release's institution roster under every backend.

    Parameters
    ----------
    report : FCACallReport
        Already `fetch`-ed report to locate the release directory.
    period : ReportingPeriod
        The release period to load the roster for.

    Returns
    -------
    dict[str, narwhals.DataFrame]
        Backend name to that backend's roster frame.
    """
    manifest = report.releases_.get(period)
    assert manifest is not None, f"{period.label} was not resolved by fetch()."

    frames: dict[str, nw.DataFrame[Any]] = {}
    for backend in ALL_BACKENDS:
        with config_context(dataframe_backend=backend):
            frames[backend] = _eager(
                read_institutions(release_dir=manifest.release_dir)
            )
    return frames


@pytest.mark.parametrize(
    "period",
    CROSS_BACKEND_SAMPLE_PERIODS,
    ids=[period.label for period in CROSS_BACKEND_SAMPLE_PERIODS],
)
def test_release_institutions_match_across_backends(
    archive_report: FCACallReport, period: ReportingPeriod
) -> None:
    """A release's institution roster is identical no matter which backend parsed it.

    The roster carries the institution names and addresses that the
    point-in-time lookups are built on, so a backend-specific difference
    in how a name or ZIP parses would be a data bug, not a cosmetic one.
    """
    frames = _load_institutions_frames(report=archive_report, period=period)
    reference = frames["pandas"]
    for backend, frame in frames.items():
        if backend == "pandas":
            continue
        _assert_frames_equal(
            reference=reference, other=frame, label=f"{backend}:institutions"
        )


def test_public_query_api_agrees_with_the_archive() -> None:
    """The period and schedule query methods describe the archive correctly.

    `periods_available` and `periods_missing` are documented as
    complements. Checking that against the whole release history covers
    schedules that FCA introduced or retired part way through, which is
    where a partition bug would show up.
    """
    report = _fetched_full_history_report()

    assert report.available_periods() == ALL_KNOWN_PERIODS
    assert report.available_schedules(), "available_schedules() returned nothing."

    requested = set(ALL_KNOWN_PERIODS)
    for schedule in report.schedules_:
        available = set(report.periods_available(schedule=schedule))
        missing = set(report.periods_missing(schedule=schedule))
        assert available, f"{schedule} is in schedules_ but has no periods."
        assert available | missing == requested, (
            f"{schedule}: available plus missing is not every requested period."
        )
        assert not (available & missing), (
            f"{schedule}: {sorted(available & missing)} are both available and missing."
        )


@pytest.mark.parametrize("backend", ALL_BACKENDS)
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


@pytest.mark.parametrize("backend", ALL_BACKENDS)
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


@pytest.mark.parametrize(
    "period",
    EQUALITY_CHECK_PERIODS,
    ids=[period.label for period in EQUALITY_CHECK_PERIODS],
)
def test_release_schedules_match_across_backends(
    archive_report: FCACallReport, period: ReportingPeriod
) -> None:
    """A release's schedule schema and data are identical across backends.

    Loads every schedule for `period` three times -- once per backend --
    through the same production `get_layout`/`read_schedule_file` path the
    other tests in this module use, then asserts pandas, polars, and pyarrow
    all built the same columns, in the same order, with equivalent dtypes,
    holding the same values.
    """
    manifest = archive_report.releases_.get(period)
    assert manifest is not None, f"{period.label} was not resolved by fetch()."
    schedule_roots = tuple(
        sorted(root for root in manifest.files if root != INSTITUTIONS_ROOT)
    )
    assert schedule_roots, (
        f"{period.label} has no non-institution schedules to compare."
    )

    reference = _load_schedule_frames(
        report=archive_report,
        period=period,
        schedule_roots=schedule_roots,
        backend="pandas",
    )
    for backend in ("polars", "pyarrow"):
        other = _load_schedule_frames(
            report=archive_report,
            period=period,
            schedule_roots=schedule_roots,
            backend=backend,
        )
        for root in schedule_roots:
            _assert_frames_equal(
                reference=reference[root], other=other[root], label=f"{backend}:{root}"
            )


def _to_wide_format_frame(
    *, period: ReportingPeriod, backend: str
) -> nw.DataFrame[Any]:
    """Build to_wide_format() for one real release under one backend.

    Scoped to a single-period `FCACallReport` (rather than reusing the
    full-history `archive_report` fixture) since `to_wide_format()`
    reshapes every period an instance was fetched for -- comparing across
    backends only needs one release at a time. Rows are sorted by UNINUM
    (unique within a single period) so backends whose pivot doesn't
    preserve a particular row order can still be compared position-by-position.

    Parameters
    ----------
    period : ReportingPeriod
        The release to build the wide-format frame for.
    backend : str
        The dataframe backend to configure while building it.

    Returns
    -------
    narwhals.DataFrame
        The wide-format frame, sorted by UNINUM.
    """
    with config_context(dataframe_backend=backend):
        report = FCACallReport(
            start=period.period_end,
            end=period.period_end,
            transport=PackagedArchiveTransport(),
        )
        wide = report.to_wide_format()
    return _eager(wide).sort("UNINUM")


@pytest.mark.parametrize(
    "period",
    EQUALITY_CHECK_PERIODS,
    ids=[period.label for period in EQUALITY_CHECK_PERIODS],
)
def test_wide_format_matches_across_backends(period: ReportingPeriod) -> None:
    """to_wide_format()'s schema and data agree no matter which backend built it.

    Builds the full wide-format frame for one real release, once per
    backend -- including pyarrow, which has no native pivot and instead
    goes through the manual filter-and-join fallback (see
    `call_report.core._backend.pivot`) -- and asserts pandas, polars, and
    pyarrow all produced the same columns, in the same order, with
    equivalent dtypes, holding the same values.
    """
    reference = _to_wide_format_frame(period=period, backend="pandas")
    assert len(reference) > 0, f"{period.label}: wide format built 0 rows."
    for backend in ("polars", "pyarrow"):
        other = _to_wide_format_frame(period=period, backend=backend)
        _assert_frames_equal(
            reference=reference,
            other=other,
            label=f"{backend}:{period.label}",
            allow_pandas_object=RESHAPE_PANDAS_OBJECT_DTYPES,
        )


def _long_format_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """Build a total-order sort key for a long-format row, tolerant of null codes.

    `code_column`/`code_value` are null for a non-coded variable, and
    ``None``/``nan`` can't be compared against real values when sorting
    -- substitutes a low sentinel for either, purely to get a stable,
    backend-independent row order to compare position-by-position.
    """
    code_column = row["code_column"]
    if _is_missing(code_column):
        code_column = ""
    code_value = row["code_value"]
    if _is_missing(code_value):
        code_value = -1.0
    return (
        row["UNINUM"],
        row["schedule"],
        code_column,
        code_value,
        row["variable_name"],
    )


def _to_long_format_frame(
    *, period: ReportingPeriod, backend: str
) -> nw.DataFrame[Any]:
    """Build to_long_format() for one real release under one backend.

    Mirrors `_to_wide_format_frame`. Row order is left to the caller,
    which sorts by `_long_format_sort_key` since UNINUM alone isn't
    unique at the long-format grain and the sort has to tolerate nulls.

    Parameters
    ----------
    period : ReportingPeriod
        The release to build the long-format frame for.
    backend : str
        The dataframe backend to configure while building it.

    Returns
    -------
    narwhals.DataFrame
        The long-format frame, in whatever order the backend produced.
    """
    with config_context(dataframe_backend=backend):
        report = FCACallReport(
            start=period.period_end,
            end=period.period_end,
            transport=PackagedArchiveTransport(),
        )
        long_ = report.to_long_format()
    return _eager(long_)


@pytest.mark.parametrize(
    "period",
    EQUALITY_CHECK_PERIODS,
    ids=[period.label for period in EQUALITY_CHECK_PERIODS],
)
def test_long_format_matches_across_backends(period: ReportingPeriod) -> None:
    """to_long_format()'s schema and data agree no matter which backend built it.

    Mirrors `test_wide_format_matches_across_backends` for the long-format
    path -- pandas, polars, and pyarrow must all produce the same columns
    and the same rows for the same real release.
    """
    reference = _to_long_format_frame(period=period, backend="pandas")
    assert len(reference) > 0, f"{period.label}: long format built 0 rows."
    reference_rows = sorted(reference.rows(named=True), key=_long_format_sort_key)
    for backend in ("polars", "pyarrow"):
        other = _to_long_format_frame(period=period, backend=backend)
        label = f"{backend}:{period.label}"
        _assert_schemas_equivalent(
            reference=reference,
            other=other,
            label=label,
            allow_pandas_object=RESHAPE_PANDAS_OBJECT_DTYPES,
        )
        _assert_rows_equal(
            reference=reference_rows,
            other=sorted(other.rows(named=True), key=_long_format_sort_key),
            label=label,
        )


@pytest.mark.parametrize(
    "period",
    EQUALITY_CHECK_PERIODS,
    ids=[period.label for period in EQUALITY_CHECK_PERIODS],
)
def test_wide_and_long_format_round_trip_agree_on_real_data(
    period: ReportingPeriod,
) -> None:
    """to_wide_format/to_long_format, converted into each other, carry the same data.

    Real archived data genuinely has gaps (one institution reports a code
    another never does), so wide -> long -> wide is expected to match
    exactly, while long -> wide -> long picks up extra, structurally null
    rows from pivot's grid-completion -- see
    `convert_wide_format_to_long_format`'s docstring. This is the
    real-data proof of both the hermetic tests in test_reshape.py and
    test_report.py's small-fixture version.
    """
    _assert_round_trip(period=period)


def _assert_round_trip(*, period: ReportingPeriod) -> None:
    """Convert one release's wide and long formats into each other and compare.

    Parameters
    ----------
    period : ReportingPeriod
        The release to round trip.
    """
    report = FCACallReport(
        start=period.period_end,
        end=period.period_end,
        transport=PackagedArchiveTransport(),
    )
    wide = report.to_wide_format()
    long_ = report.to_long_format()

    converted_long_rows = _native_rows(convert_wide_format_to_long_format(wide=wide))
    converted_wide_rows = _native_rows(convert_long_format_to_wide_format(long=long_))
    long_rows = _native_rows(long_)
    wide_rows = _native_rows(wide)

    # Both round trips compare one row list against another, and two empty
    # lists compare equal, so an empty starting frame would make every
    # assertion below vacuous.
    assert wide_rows, f"{period.label}: wide format built 0 rows."
    assert long_rows, f"{period.label}: long format built 0 rows."

    # wide -> long -> wide: exact match, no grid-completion gaps to create.
    # Schema first: a round trip that preserved every value but changed a
    # column's dtype or dropped a column would otherwise pass.
    _assert_schemas_equivalent(
        reference=_eager(wide),
        other=_eager(convert_long_format_to_wide_format(long=long_)),
        label=f"wide-round-trip-schema:{period.label}",
        allow_pandas_object=RESHAPE_PANDAS_OBJECT_DTYPES,
    )
    _assert_rows_equal(
        reference=sorted(wide_rows, key=lambda row: row["UNINUM"]),
        other=sorted(converted_wide_rows, key=lambda row: row["UNINUM"]),
        label=f"wide-round-trip:{period.label}",
    )

    # long -> wide -> long: every non-null-value row must still match. The
    # round trip may have extra, structurally null rows beyond that (pivot
    # grid-completion) -- and the original long_rows can itself already
    # contain real null-value rows (a genuinely blank source field), so
    # both sides are filtered to non-null before comparing.
    _assert_schemas_equivalent(
        reference=_eager(long_),
        other=_eager(convert_wide_format_to_long_format(wide=wide)),
        label=f"long-round-trip-schema:{period.label}",
        allow_pandas_object=RESHAPE_PANDAS_OBJECT_DTYPES,
        # `to_long_format` and `convert_wide_format_to_long_format` return
        # the same eight columns in different orders, so the round trip is
        # not order-stable. Names and dtypes are still compared exactly.
        ignore_column_order=True,
    )

    non_null_long = [row for row in long_rows if not _is_missing(row["value"])]
    non_null_converted_long = [
        row for row in converted_long_rows if not _is_missing(row["value"])
    ]
    assert len(non_null_converted_long) == len(non_null_long), (
        f"{period.label}: round-tripped non-null row count "
        f"({len(non_null_converted_long)}) != original non-null long-format "
        f"row count ({len(non_null_long)})."
    )
    _assert_rows_equal(
        reference=sorted(non_null_long, key=_long_format_sort_key),
        other=sorted(non_null_converted_long, key=_long_format_sort_key),
        label=f"long-round-trip:{period.label}",
    )


# ---------------------------------------------------------------------------
# Exhaustive: every archived release against every backend.
#
# The tests above sample: the full history runs under pandas alone, a seeded
# stratified sample of 20 periods runs under all three backends, and 4 evenly
# spaced periods are compared value-for-value across backends. That keeps
# ordinary pull requests to a few minutes.
#
# The tests below drop the sampling and run the whole cross product. They are
# skipped unless --run-exhaustive is passed (see tests/conftest.py), so they
# cost nothing on a normal run, and are wired to a manually dispatched
# workflow in .github/workflows/exhaustive-regression.yml for use before a
# release.
# ---------------------------------------------------------------------------


@pytest.mark.exhaustive
@pytest.mark.parametrize("backend", ALL_BACKENDS)
@pytest.mark.parametrize(
    "period", ALL_KNOWN_PERIODS, ids=[period.label for period in ALL_KNOWN_PERIODS]
)
def test_exhaustive_every_release_loads_under_every_backend(
    archive_report: FCACallReport, period: ReportingPeriod, backend: str
) -> None:
    """Every schedule of every archived release parses under every backend.

    The sampled version of this test covers 20 periods. Sampling is a
    reasonable trade for pull requests, but a parsing quirk confined to one
    quarter is exactly the kind of thing a sample misses.
    """
    with config_context(dataframe_backend=backend):
        _assert_all_schedules_load(report=archive_report, period=period)


@pytest.mark.exhaustive
@pytest.mark.parametrize("backend", ALL_BACKENDS)
@pytest.mark.parametrize(
    "period", ALL_KNOWN_PERIODS, ids=[period.label for period in ALL_KNOWN_PERIODS]
)
def test_exhaustive_every_release_institutions_load_under_every_backend(
    archive_report: FCACallReport, period: ReportingPeriod, backend: str
) -> None:
    """Every archived release's institution roster parses under every backend."""
    with config_context(dataframe_backend=backend):
        _assert_institutions_load(report=archive_report, period=period)


@pytest.mark.exhaustive
@pytest.mark.parametrize(
    "backend",
    [
        "pandas",
        pytest.param(
            "polars",
            marks=pytest.mark.xfail(
                reason=(
                    "load_all() raises SchemaError under polars: a column that "
                    "is all null in one quarter and populated in another gets "
                    "Null against Int64, and the cross-period concat cannot "
                    "stack them. See issue #43."
                ),
                raises=Exception,
            ),
        ),
        "pyarrow",
    ],
)
def test_exhaustive_public_load_api_works_under_every_backend(backend: str) -> None:
    """The public load API returns the whole archive under every backend.

    The sampled version of this runs under pandas alone.
    `load_all` stacks each schedule across all 105 releases with
    `concat(how=schema_policy)`, so this is the only test that exercises
    cross-period schema drift through the interface a caller uses. That
    is how it caught the polars failure marked above.
    """
    with config_context(dataframe_backend=backend):
        report = _fetched_full_history_report()
        _assert_public_load_api(report=report, backend=backend)


@pytest.mark.exhaustive
@pytest.mark.parametrize(
    "period", ALL_KNOWN_PERIODS, ids=[period.label for period in ALL_KNOWN_PERIODS]
)
def test_exhaustive_every_release_matches_across_backends(
    archive_report: FCACallReport, period: ReportingPeriod
) -> None:
    """Every archived release parses to identical schemas and values across backends.

    The strongest check in this module, and the most expensive: it loads
    each release three times and compares every column and every value.
    The sampled version covers 4 periods. A backend-specific dtype or
    null-handling difference that only shows up on one quarter's data
    would pass that sample and fail here.
    """
    manifest = archive_report.releases_.get(period)
    assert manifest is not None, f"{period.label} was not resolved by fetch()."
    schedule_roots = tuple(
        sorted(root for root in manifest.files if root != INSTITUTIONS_ROOT)
    )
    assert schedule_roots, f"{period.label} has no non-institution schedules."

    reference = _load_schedule_frames(
        report=archive_report,
        period=period,
        schedule_roots=schedule_roots,
        backend="pandas",
    )
    for backend in ("polars", "pyarrow"):
        other = _load_schedule_frames(
            report=archive_report,
            period=period,
            schedule_roots=schedule_roots,
            backend=backend,
        )
        assert set(other) == set(reference), (
            f"{period.label}: {backend} produced schedules {sorted(other)}, "
            f"expected {sorted(reference)}."
        )
        for root in schedule_roots:
            _assert_frames_equal(
                reference=reference[root],
                other=other[root],
                label=f"{period.label}:{backend}:{root}",
            )


@pytest.mark.exhaustive
@pytest.mark.parametrize(
    "period", ALL_KNOWN_PERIODS, ids=[period.label for period in ALL_KNOWN_PERIODS]
)
def test_exhaustive_wide_format_matches_across_backends(
    period: ReportingPeriod,
) -> None:
    """Every release's wide format agrees across backends, not just four of them.

    The sampled version covers 4 evenly spaced periods. This is the same
    check over the whole history, which matters most for the pyarrow
    backend: it has no native pivot and goes through `_manual_pivot`, a
    different code path from the one the other two backends use.
    """
    reference = _to_wide_format_frame(period=period, backend="pandas")
    assert len(reference) > 0, f"{period.label}: wide format built 0 rows."

    for backend in ("polars", "pyarrow"):
        other = _to_wide_format_frame(period=period, backend=backend)
        _assert_frames_equal(
            reference=reference,
            other=other,
            label=f"{backend}:{period.label}",
            allow_pandas_object=RESHAPE_PANDAS_OBJECT_DTYPES,
        )


@pytest.mark.exhaustive
@pytest.mark.parametrize(
    "period", ALL_KNOWN_PERIODS, ids=[period.label for period in ALL_KNOWN_PERIODS]
)
def test_exhaustive_long_format_matches_across_backends(
    period: ReportingPeriod,
) -> None:
    """Every release's long format agrees across backends, not just four of them."""
    reference = _to_long_format_frame(period=period, backend="pandas")
    assert len(reference) > 0, f"{period.label}: long format built 0 rows."
    reference_rows = sorted(reference.rows(named=True), key=_long_format_sort_key)
    for backend in ("polars", "pyarrow"):
        other = _to_long_format_frame(period=period, backend=backend)
        label = f"{backend}:{period.label}"
        _assert_schemas_equivalent(
            reference=reference,
            other=other,
            label=label,
            allow_pandas_object=RESHAPE_PANDAS_OBJECT_DTYPES,
        )
        _assert_rows_equal(
            reference=reference_rows,
            other=sorted(other.rows(named=True), key=_long_format_sort_key),
            label=label,
        )


@pytest.mark.exhaustive
@pytest.mark.parametrize(
    "period", ALL_KNOWN_PERIODS, ids=[period.label for period in ALL_KNOWN_PERIODS]
)
def test_exhaustive_wide_and_long_round_trip_on_every_release(
    period: ReportingPeriod,
) -> None:
    """The wide/long round trips hold on every release, not just four of them."""
    _assert_round_trip(period=period)


@pytest.mark.exhaustive
@pytest.mark.parametrize(
    "period", ALL_KNOWN_PERIODS, ids=[period.label for period in ALL_KNOWN_PERIODS]
)
def test_exhaustive_institutions_match_across_backends(
    archive_report: FCACallReport, period: ReportingPeriod
) -> None:
    """Every release's institution roster is identical across backends.

    The sampled version covers 20 releases. The roster is small, so
    covering all of them costs little, and it carries the names and
    addresses the institution lineage work depends on.
    """
    frames = _load_institutions_frames(report=archive_report, period=period)
    reference = frames["pandas"]
    for backend, frame in frames.items():
        if backend == "pandas":
            continue
        _assert_frames_equal(
            reference=reference,
            other=frame,
            label=f"{period.label}:{backend}:institutions",
        )
