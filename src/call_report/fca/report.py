"""The FCA Call Report estimator-style entry point: FCACallReport."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal, Self

import narwhals as nw

from call_report._backend import concat, finalize
from call_report.base import BaseCallReport
from call_report.exceptions import (
    CallReportError,
    DownloadError,
    InvalidPeriodError,
    LayoutParseError,
    ScheduleNotFoundError,
)
from call_report.fca._discovery import ReleaseFiles, scan_release
from call_report.fca.catalog import construct_fca_download_url
from call_report.fca.enums import FCASchedule, coerce_fca_call_report_schedule
from call_report.fca.institutions import INSTITUTIONS_ROOT, _read_institutions_frame
from call_report.fca.layout import FCALayout, parse_layout
from call_report.fca.reader import _read_schedule_frame
from call_report.fca.transport import FCATransport, LocalDirectoryTransport
from call_report.periods import PeriodRange, ReportingPeriod

SchemaPolicy = Literal["union", "intersection", "strict"]


@dataclass(frozen=True, kw_only=True)
class FCAReleaseManifest:
    """The resolved files for one period, discovered during `fetch`.

    Internal bookkeeping consulted by `load`, `load_all`,
    `load_institutions`, and `get_layout` to find each schedule's files.

    Attributes
    ----------
    period : ReportingPeriod
        The period this manifest describes.
    release_dir : pathlib.Path
        The resolved local directory containing this period's files.
    files : dict[str, ReleaseFiles]
        Every matched layout/data file pair found in `release_dir`, keyed
        by root name (including ``"INST"``).
    """

    period: ReportingPeriod
    release_dir: Path
    files: dict[str, ReleaseFiles]


@dataclass(frozen=True, kw_only=True)
class FCAIssue:
    """A non-fatal problem encountered while fetching or loading.

    Collected in `FCACallReport.errors_` rather than raised, so a problem
    with one period or schedule doesn't abort an otherwise-valid
    multi-period request.

    Attributes
    ----------
    period : ReportingPeriod
        The period the issue occurred for.
    schedule : FCASchedule or None
        The schedule the issue occurred for, or ``None`` for a
        release-level issue (e.g. an unresolvable directory).
    error : CallReportError
        The underlying exception that was caught.
    """

    period: ReportingPeriod
    schedule: FCASchedule | None
    error: CallReportError


class FCACallReport(BaseCallReport):
    """The estimator-style entry point for FCA Call Report data.

    Follows the sklearn convention: ``__init__`` only stores its
    parameters, doing no validation or I/O; `fetch` validates parameters
    and resolves release files, populating trailing-underscore attributes;
    `load`-family methods call `fetch` automatically if it hasn't run.

    Parameters
    ----------
    start : str or datetime.date
        The first quarter-end in the requested range.
    end : str or datetime.date, optional
        The last quarter-end in the requested range (inclusive). Must be
        supplied explicitly -- there is no single-quarter default, so a
        request's bounds are never ambiguous.
    schema_policy : {"union", "intersection", "strict"}, default "union"
        How to reconcile schema differences when stacking multiple
        periods' data together for one schedule.
    data_dir : str or pathlib.Path, optional
        A directory containing one already-extracted release subdirectory
        per period. Used to build a default `LocalDirectoryTransport` when
        `transport` is not supplied.
    transport : FCATransport, optional
        A custom transport for resolving each period's local files.
        Overrides `data_dir` entirely when supplied.

    Examples
    --------
    >>> report = FCACallReport(
    ...     start="2024-03-31", end="2025-12-31", data_dir="fca_data"
    ... )
    >>> report.load(schedule="RCB")  # doctest: +SKIP
    """

    def __init__(
        self,
        *,
        start: str | date,
        end: str | date | None = None,
        schema_policy: SchemaPolicy = "union",
        data_dir: str | Path | None = None,
        transport: FCATransport | None = None,
    ) -> None:
        self.start = start
        self.end = end
        self.schema_policy = schema_policy
        self.data_dir = data_dir
        self.transport = transport

    def fetch(self) -> Self:
        """Validate parameters and resolve every requested period's files.

        Populates `periods_`, `releases_`, `schedules_`, and `errors_`. A
        period that is within FCA's catalog bounds but whose local files
        can't be resolved is skipped and recorded in `errors_` rather than
        aborting the whole call; an out-of-bounds request, or one missing
        `end`, is a hard, immediate error since it isn't a partial-data
        situation.

        Returns
        -------
        Self
            This instance, to support method chaining.

        Raises
        ------
        InvalidPeriodError
            If `end` was not supplied, or `start`/`end` is not a valid
            quarter-end date.
        PeriodNotAvailableError
            If the requested range falls outside FCA's known-published
            bounds.
        DownloadError
            If no transport is configured, or if every requested period
            failed to resolve.
        """
        if self.end is None:
            raise InvalidPeriodError(
                "end must be supplied explicitly (e.g. "
                "FCACallReport(start=..., end=...)); pass the same value as start "
                "for a single quarter."
            )
        periods = PeriodRange(start=self.start, end=self.end)

        # Validate the whole range against FCA's catalog bounds up front --
        # an out-of-bounds request is invalid, not a partial-data situation.
        construct_fca_download_url(period=periods[0])
        construct_fca_download_url(period=periods[-1])

        transport = self._resolve_transport()

        releases: dict[ReportingPeriod, FCAReleaseManifest] = {}
        errors: list[FCAIssue] = []
        for period in periods:
            try:
                release_dir = transport.resolve(period=period)
            except DownloadError as error:
                errors.append(FCAIssue(period=period, schedule=None, error=error))
                continue
            files = scan_release(release_dir=release_dir)
            releases[period] = FCAReleaseManifest(
                period=period, release_dir=release_dir, files=files
            )

        if not releases:
            raise DownloadError(
                f"None of the {len(periods)} requested period(s) could be resolved; "
                "see errors_ for details."
            )

        self.periods_ = periods
        self.releases_ = releases
        self.schedules_ = _build_schedule_presence_map(releases=releases)
        self.errors_: tuple[FCAIssue, ...] = tuple(errors)
        return self

    def _resolve_transport(self) -> FCATransport:
        """Return the transport this instance should use.

        An explicit `transport` always wins over `data_dir` when both are
        supplied.

        Returns
        -------
        FCATransport
            `transport` if supplied; otherwise a `LocalDirectoryTransport`
            built from `data_dir`.

        Raises
        ------
        DownloadError
            If neither `transport` nor `data_dir` was supplied.
        """
        if self.transport is not None:
            return self.transport
        if self.data_dir is not None:
            return LocalDirectoryTransport(data_dir=Path(self.data_dir))
        raise DownloadError(
            "No transport configured: pass data_dir=... pointing at a directory of "
            "already-extracted FCA release folders, or transport=... for a custom "
            "retrieval strategy. Network downloading is not yet implemented."
        )

    def _ensure_fetched(self) -> None:
        """Call `fetch` if it has not already run.

        Checks for `periods_` since trailing-underscore attributes are, by
        convention, only ever set by `fetch`.
        """
        if not hasattr(self, "periods_"):
            self.fetch()

    def _record_issue(
        self,
        *,
        period: ReportingPeriod,
        schedule: FCASchedule | None,
        error: CallReportError,
    ) -> None:
        """Append one issue to `errors_`.

        Used by `load`, `load_all`, and `load_institutions` to record a
        parse-stage failure without aborting the whole call.

        Parameters
        ----------
        period : ReportingPeriod
            The period the issue occurred for.
        schedule : FCASchedule or None
            The schedule the issue occurred for, or ``None``.
        error : CallReportError
            The underlying exception that was caught.
        """
        self.errors_ = (
            *self.errors_,
            FCAIssue(period=period, schedule=schedule, error=error),
        )

    def load(self, *, schedule: FCASchedule | str) -> Any:
        """Load one schedule, stacked across every period that has it.

        A period+schedule combination that fails to parse is skipped and
        recorded in `errors_` rather than aborting the whole call.

        Parameters
        ----------
        schedule : FCASchedule or str
            The schedule to load; a string is matched case-insensitively.

        Returns
        -------
        Any
            A native dataframe of the configured backend, carrying a
            ``period`` column alongside the schedule's own columns.

        Raises
        ------
        ScheduleNotFoundError
            If `schedule` has zero surviving periods (either because it
            was never present, or every attempt to parse it failed).
        LayoutParseError
            If `schema_policy` is ``"strict"`` and the surviving periods'
            columns are not identical.
        """
        self._ensure_fetched()
        schedule_enum = coerce_fca_call_report_schedule(value=schedule)

        frames: list[nw.DataFrame[Any]] = []
        for period in self.schedules_.get(schedule_enum, ()):
            files = self.releases_[period].files[schedule_enum.value]
            try:
                layout = parse_layout(path=files.layout_path)
                frame = _read_schedule_frame(data_path=files.data_path, layout=layout)
            except LayoutParseError as error:
                self._record_issue(period=period, schedule=schedule_enum, error=error)
                continue
            frames.append(_with_period_column(frame=frame, period=period))

        if not frames:
            raise ScheduleNotFoundError(
                f"{schedule_enum.value} was not found in any period of "
                f"{self.periods_[0].label}-{self.periods_[-1].label}."
            )
        return finalize(frame=concat(frames=frames, how=self.schema_policy))

    def load_all(self) -> dict[FCASchedule, Any]:
        """Load every schedule discovered across the requested periods.

        A schedule that fails to load entirely (see `load`) is omitted
        from the result rather than aborting the whole call; `errors_`
        still records why.

        Returns
        -------
        dict[FCASchedule, Any]
            A mapping from schedule to its stacked native dataframe.
        """
        self._ensure_fetched()
        result: dict[FCASchedule, Any] = {}
        for schedule in self.schedules_:
            try:
                result[schedule] = self.load(schedule=schedule)
            except ScheduleNotFoundError:
                continue
        return result

    def load_institutions(self) -> Any:
        """Load the institution roster, stacked across every requested period.

        A period whose roster fails to parse is skipped and recorded in
        `errors_` rather than aborting the whole call.

        Returns
        -------
        Any
            A native dataframe of the configured backend, carrying a
            ``period`` column alongside the roster's own columns.

        Raises
        ------
        DownloadError
            If no period's roster could be loaded.
        """
        self._ensure_fetched()
        frames: list[nw.DataFrame[Any]] = []
        for period, manifest in self.releases_.items():
            if INSTITUTIONS_ROOT not in manifest.files:
                continue
            try:
                frame = _read_institutions_frame(release_dir=manifest.release_dir)
            except (DownloadError, LayoutParseError) as error:
                self._record_issue(period=period, schedule=None, error=error)
                continue
            frames.append(_with_period_column(frame=frame, period=period))

        if not frames:
            raise DownloadError(
                "No institution roster could be loaded for any requested period; "
                "see errors_ for details."
            )
        return finalize(frame=concat(frames=frames, how=self.schema_policy))

    def get_layout(
        self,
        *,
        schedule: FCASchedule | str,
        period: str | date | ReportingPeriod | None = None,
    ) -> FCALayout | dict[ReportingPeriod, FCALayout]:
        """Return a schedule's layout for one period, or for every period in range.

        Layouts can drift across periods, so callers can inspect either a
        single period's layout or how it evolved across the whole range.

        Parameters
        ----------
        schedule : FCASchedule or str
            The schedule to describe; a string is matched case-insensitively.
        period : str, datetime.date, ReportingPeriod, optional
            A specific period to describe. If omitted, returns the layout
            for every period in the requested range that has `schedule`.

        Returns
        -------
        FCALayout or dict[ReportingPeriod, FCALayout]
            A single layout if `period` was supplied; otherwise a mapping
            from period to layout.

        Raises
        ------
        InvalidPeriodError
            If `period` was supplied but falls outside the fetched range.
        ScheduleNotFoundError
            If `schedule` is not present for the requested period (or, if
            `period` was omitted, for any period in range).
        """
        self._ensure_fetched()
        schedule_enum = coerce_fca_call_report_schedule(value=schedule)

        if period is not None:
            target = (
                period
                if isinstance(period, ReportingPeriod)
                else ReportingPeriod.from_period_end(value=period)
            )
            if target not in self.periods_:
                raise InvalidPeriodError(
                    f"{target.label} is outside the fetched range "
                    f"({self.periods_[0].label}-{self.periods_[-1].label}); construct "
                    "a new FCACallReport(start=..., end=...) to inspect that period."
                )
            manifest = self.releases_.get(target)
            if manifest is None or schedule_enum.value not in manifest.files:
                raise ScheduleNotFoundError(
                    f"{schedule_enum.value} was not found in {target.label}."
                )
            return parse_layout(path=manifest.files[schedule_enum.value].layout_path)

        periods_with_schedule = self.schedules_.get(schedule_enum, ())
        if not periods_with_schedule:
            raise ScheduleNotFoundError(
                f"{schedule_enum.value} was not found in any period of "
                f"{self.periods_[0].label}-{self.periods_[-1].label}."
            )
        return {
            found: parse_layout(
                path=self.releases_[found].files[schedule_enum.value].layout_path
            )
            for found in periods_with_schedule
        }

    def available_periods(self) -> tuple[ReportingPeriod, ...]:
        """Return every period FCA is known to publish.

        Reflects FCA's overall catalog (`call_report.fca.catalog`),
        independent of this instance's `start`/`end`; does not require
        `fetch` to have run.

        Returns
        -------
        tuple[ReportingPeriod, ...]
            The known-available periods, oldest first.
        """
        from call_report.fca.catalog import EARLIEST_PERIOD, LATEST_KNOWN_PERIOD

        return tuple(PeriodRange(start=EARLIEST_PERIOD, end=LATEST_KNOWN_PERIOD))

    def available_schedules(self) -> tuple[FCASchedule, ...]:
        """Return every schedule FCA's format has ever used.

        Does not require `fetch` to have run.

        Returns
        -------
        tuple[FCASchedule, ...]
            Every `FCASchedule` member.
        """
        return tuple(FCASchedule)

    def periods_available(
        self, *, schedule: FCASchedule | str
    ) -> tuple[ReportingPeriod, ...]:
        """Return the requested periods in which a schedule is present.

        The complement of `periods_missing` for the same schedule.

        Parameters
        ----------
        schedule : FCASchedule or str
            The schedule to check; a string is matched case-insensitively.

        Returns
        -------
        tuple[ReportingPeriod, ...]
            The subset of `periods_` that have `schedule`.
        """
        self._ensure_fetched()
        schedule_enum = coerce_fca_call_report_schedule(value=schedule)
        return self.schedules_.get(schedule_enum, ())

    def periods_missing(
        self, *, schedule: FCASchedule | str
    ) -> tuple[ReportingPeriod, ...]:
        """Return the requested periods in which a schedule is absent.

        The complement of `periods_available` for the same schedule.

        Parameters
        ----------
        schedule : FCASchedule or str
            The schedule to check; a string is matched case-insensitively.

        Returns
        -------
        tuple[ReportingPeriod, ...]
            The subset of `periods_` that do not have `schedule`.
        """
        self._ensure_fetched()
        available = set(self.periods_available(schedule=schedule))
        return tuple(period for period in self.periods_ if period not in available)


def _with_period_column(
    *, frame: nw.DataFrame[Any], period: ReportingPeriod
) -> nw.DataFrame[Any]:
    """Attach a ``period`` column holding a period's quarter-end date.

    Used by `load`, `load_all`, and `load_institutions` so every stacked
    frame can be traced back to which period each row came from.

    Parameters
    ----------
    frame : narwhals.DataFrame
        The frame to annotate.
    period : ReportingPeriod
        The period `frame` was parsed from.

    Returns
    -------
    narwhals.DataFrame
        `frame` with an added ``period`` column.
    """
    return frame.with_columns(nw.lit(period.period_end).alias("period"))


def _build_schedule_presence_map(
    *, releases: dict[ReportingPeriod, FCAReleaseManifest]
) -> dict[FCASchedule, tuple[ReportingPeriod, ...]]:
    """Build a schedule-to-periods presence map from resolved releases.

    Unrecognized root names (present in a release but not in
    `FCASchedule`) are silently ignored; this has not been observed in any
    real FCA release.

    Parameters
    ----------
    releases : dict[ReportingPeriod, FCAReleaseManifest]
        The successfully resolved releases, as populated by `fetch`.

    Returns
    -------
    dict[FCASchedule, tuple[ReportingPeriod, ...]]
        Each schedule found in at least one release, mapped to the sorted
        periods it was found in.
    """
    working: dict[FCASchedule, list[ReportingPeriod]] = {}
    for period, manifest in releases.items():
        for root in manifest.files:
            if root == INSTITUTIONS_ROOT:
                continue
            try:
                schedule = coerce_fca_call_report_schedule(value=root)
            except ScheduleNotFoundError:
                continue
            working.setdefault(schedule, []).append(period)
    return {schedule: tuple(sorted(periods)) for schedule, periods in working.items()}
