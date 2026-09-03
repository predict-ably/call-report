"""The estimator-style entry point for FCA Call Report data."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self, overload

import narwhals as nw

from call_report.core import (
    BaseCallReport,
    FieldSchema,
    FileMetadata,
    PeriodRange,
    ReportingPeriod,
)
from call_report.core._backend import (
    DataFrameType,
    FrameOrLazy,
    concat,
    date_dtype,
    finalize,
    finalize_as,
    pivot,
)
from call_report.exceptions import (
    CallReportError,
    DownloadError,
    InvalidPeriodError,
    LayoutParseError,
    ReshapeError,
    ScheduleNotFoundError,
)
from call_report.fca import _reshape
from call_report.fca._discovery import ReleaseFiles, scan_release
from call_report.fca._domain_datasets import get_fca_domain_dataset
from call_report.fca._schedule_metadata import get_fca_file_metadata
from call_report.fca.catalog import construct_fca_download_url
from call_report.fca.enums import FCADomainDataset, FCASchedule
from call_report.fca.institutions import INSTITUTIONS_ROOT, _read_institutions_frame
from call_report.fca.layout import FCALayout, parse_layout
from call_report.fca.reader import _read_schedule_frame
from call_report.fca.transport import FCATransport

if TYPE_CHECKING:
    import pandas
    import polars
    import pyarrow

    from call_report.core._backend import NativeDataFrame

SchemaPolicy = Literal["union", "intersection", "strict"]


@dataclass(frozen=True, kw_only=True)
class FCAReleaseManifest:
    """The resolved files for one period, discovered during `fetch`.

    Internal bookkeeping consulted by `_load`, `_load_all`,
    `_load_institutions`, and `get_layout` to find each schedule's files.

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

    Follows the sklearn convention. ``__init__`` only stores its
    parameters, doing no validation or I/O. `fetch` validates those
    parameters and resolves release files, populating trailing-underscore
    attributes. The `load`-family methods call `fetch` automatically if it
    has not run yet.

    Parameters
    ----------
    start : str or datetime.date
        The first quarter-end in the requested range.
    end : str or datetime.date, optional
        The last quarter-end in the requested range (inclusive). Must be
        supplied explicitly. There is no single-quarter default, so a
        request's bounds are never ambiguous.
    schema_policy : {"union", "intersection", "strict"}, default "union"
        How to reconcile schema differences when stacking multiple
        periods' data together for one schedule.
    transport : FCATransport
        The transport used to resolve each period's local files, e.g. a
        :class:`~call_report.fca.transport.LocalDirectoryTransport` pointed
        at a directory of already-extracted releases, or a
        :class:`~call_report.fca.transport.PackagedArchiveTransport` for
        the historical releases shipped with this repository.

    Examples
    --------
    >>> from call_report.fca.transport import PackagedArchiveTransport
    >>> report = FCACallReport(
    ...     start="2026-03-31",
    ...     end="2026-03-31",
    ...     transport=PackagedArchiveTransport(),
    ... )
    >>> frame = report.load(schedule="RCB")
    >>> frame.shape
    (2240, 12)
    """

    def __init__(
        self,
        *,
        start: str | date,
        end: str | date | None = None,
        schema_policy: SchemaPolicy = "union",
        transport: FCATransport,
    ) -> None:
        self.start = start
        self.end = end
        self.schema_policy = schema_policy
        self.transport = transport

    def fetch(self) -> Self:
        """Validate parameters and resolve every requested period's files.

        Populates `periods_`, `releases_`, `schedules_`, and `errors_`. A
        period that is within FCA's catalog bounds but whose local files
        cannot be resolved is skipped and recorded in `errors_` rather than
        aborting the whole call. An out-of-bounds request, or one missing
        `end`, is an immediate error, since neither is a partial-data
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
            If every requested period failed to resolve.

        Examples
        --------
        >>> from call_report.fca.transport import PackagedArchiveTransport
        >>> report = FCACallReport(
        ...     start="2026-03-31",
        ...     end="2026-03-31",
        ...     transport=PackagedArchiveTransport(),
        ... )
        >>> report.fetch() is report
        True
        >>> report.periods_
        PeriodRange(start='2026Q1', end='2026Q1')
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

        releases: dict[ReportingPeriod, FCAReleaseManifest] = {}
        errors: list[FCAIssue] = []
        for period in periods:
            try:
                release_dir = self.transport.resolve(period=period)
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

        Used by `_load`, `_load_all`, and `_load_institutions` to record a
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

    def _load(self, *, schedule: FCASchedule | str) -> Any:
        """Load one schedule, stacked across every period that has it.

        The `BaseCallReport._load` implementation backing the public
        `load`, which is where `dataframe_type` is handled. This method
        applies no conversion itself.

        A period and schedule combination that fails to parse is skipped
        and recorded in `errors_` rather than aborting the whole call.

        Parameters
        ----------
        schedule : FCASchedule or str
            The schedule to load. A string is matched case-insensitively.

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
        schedule_enum = FCASchedule.coerce(value=schedule)

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

    def _load_all(self) -> dict[FCASchedule, Any]:
        """Load every schedule discovered across the requested periods.

        The `BaseCallReport._load_all` implementation backing the public
        `load_all`, which is where `dataframe_type` is handled. This method
        applies no conversion itself.

        A schedule that fails to load entirely (see `_load`) is omitted
        from the result rather than aborting the whole call, and `errors_`
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
                result[schedule] = self._load(schedule=schedule)
            except ScheduleNotFoundError:
                continue
        return result

    def _load_institutions(self) -> Any:
        """Load the institution roster, stacked across every requested period.

        The `BaseCallReport._load_institutions` implementation backing the
        public `load_institutions`, which is where `dataframe_type` is
        handled. This method applies no conversion itself.

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
            The schedule to describe. A string is matched
            case-insensitively.
        period : str, datetime.date, ReportingPeriod, optional
            A specific period to describe. If omitted, returns the layout
            for every period in the requested range that has `schedule`.

        Returns
        -------
        FCALayout or dict[ReportingPeriod, FCALayout]
            A single layout if `period` was supplied, otherwise a mapping
            from period to layout.

        Raises
        ------
        InvalidPeriodError
            If `period` was supplied but falls outside the fetched range.
        ScheduleNotFoundError
            If `schedule` is not present for the requested period (or, if
            `period` was omitted, for any period in range).

        Examples
        --------
        >>> from call_report.fca.transport import PackagedArchiveTransport
        >>> report = FCACallReport(
        ...     start="2026-03-31",
        ...     end="2026-03-31",
        ...     transport=PackagedArchiveTransport(),
        ... )
        >>> layout = report.get_layout(schedule="RCB", period="2026-03-31")
        >>> layout.scenario
        'single_multiple'
        """
        self._ensure_fetched()
        schedule_enum = FCASchedule.coerce(value=schedule)

        if period is not None:
            _, manifest = self._release_for(schedule_enum=schedule_enum, period=period)
            return parse_layout(path=manifest.files[schedule_enum.value].layout_path)

        return {
            found: parse_layout(
                path=self.releases_[found].files[schedule_enum.value].layout_path
            )
            for found in self._periods_with_schedule(schedule_enum=schedule_enum)
        }

    def _release_for(
        self, *, schedule_enum: FCASchedule, period: str | date | ReportingPeriod
    ) -> tuple[ReportingPeriod, FCAReleaseManifest]:
        """Resolve one period and the release that has `schedule_enum` in it.

        The single-period validation shared by `get_layout` and `get_schema`,
        so both reject the same requests with the same messages. Assumes
        `fetch` has already run.

        Parameters
        ----------
        schedule_enum : FCASchedule
            The schedule that must be present.
        period : str, datetime.date, or ReportingPeriod
            The period to resolve.

        Returns
        -------
        tuple[ReportingPeriod, FCAReleaseManifest]
            The resolved period and its manifest.

        Raises
        ------
        InvalidPeriodError
            If `period` falls outside the fetched range.
        ScheduleNotFoundError
            If `schedule_enum` is not present for `period`.
        """
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
        return target, manifest

    def _periods_with_schedule(
        self, *, schedule_enum: FCASchedule
    ) -> tuple[ReportingPeriod, ...]:
        """Return every fetched period that has `schedule_enum`, or raise.

        The ``period=None`` counterpart to `_release_for`, shared by
        `get_layout` and `get_schema`. Unlike `periods_available`, an empty
        result is an error rather than an empty tuple, since a caller asking
        for a schedule's layouts or schemas across a range gets nothing
        usable back. Assumes `fetch` has already run.

        Parameters
        ----------
        schedule_enum : FCASchedule
            The schedule to look for.

        Returns
        -------
        tuple[ReportingPeriod, ...]
            The periods that have `schedule_enum`, oldest first.

        Raises
        ------
        ScheduleNotFoundError
            If `schedule_enum` is absent from every fetched period.
        """
        periods_with_schedule = self.schedules_.get(schedule_enum, ())
        if not periods_with_schedule:
            raise ScheduleNotFoundError(
                f"{schedule_enum.value} was not found in any period of "
                f"{self.periods_[0].label}-{self.periods_[-1].label}."
            )
        return periods_with_schedule

    def get_schema(
        self,
        *,
        schedule: FCASchedule | str,
        period: str | date | ReportingPeriod | None = None,
    ) -> FieldSchema | dict[ReportingPeriod, FieldSchema]:
        """Return a schedule's canonical schema as of one period, or every period.

        The point-in-time counterpart to `get_file_metadata`: the fields the
        package believes `schedule` had at a given quarter, each narrowed to
        the definition that applied then rather than today's. Pairs with
        `get_layout`, which returns what a fetched release actually
        declared, so the two can be compared on identical arguments.

        Parameters
        ----------
        schedule : FCASchedule or str
            The schedule to describe. A string is matched
            case-insensitively.
        period : str, datetime.date, ReportingPeriod, optional
            A specific period to describe. If omitted, returns the schema
            for every period in the requested range that has `schedule`.

        Returns
        -------
        FieldSchema or dict[ReportingPeriod, FieldSchema]
            A single schema if `period` was supplied, otherwise a mapping
            from period to schema.

        Raises
        ------
        InvalidPeriodError
            If `period` was supplied but falls outside the fetched range.
        ScheduleNotFoundError
            If `schedule` is not present for the requested period (or, if
            `period` was omitted, for any period in range).
        PeriodNotAvailableError
            If a fetched release has `schedule` for a period the canonical
            metadata says it was not published in. That is a disagreement
            between the release and the shipped metadata, not a missing
            schedule, so it is reported as its own error.

        Examples
        --------
        >>> from call_report.fca.transport import PackagedArchiveTransport
        >>> report = FCACallReport(
        ...     start="2026-03-31",
        ...     end="2026-03-31",
        ...     transport=PackagedArchiveTransport(),
        ... )
        >>> schema = report.get_schema(schedule="RCB", period="2026-03-31")
        >>> schema.names[:3]
        ('SYSTEM', 'DIST', 'ASSOC')
        >>> schema["UNINUM"].versions[0].periods[0].label
        '2026Q1'
        """
        self._ensure_fetched()
        schedule_enum = FCASchedule.coerce(value=schedule)
        metadata = get_fca_file_metadata(schedule=schedule_enum)

        if period is not None:
            target, _ = self._release_for(schedule_enum=schedule_enum, period=period)
            return metadata.as_of(period=target).file_schema

        return {
            found: metadata.as_of(period=found).file_schema
            for found in self._periods_with_schedule(schedule_enum=schedule_enum)
        }

    def get_file_metadata(self, *, schedule: FCASchedule | str) -> FileMetadata:
        """Return a schedule's canonical, cross-time field metadata.

        This is the metadata this package ships, generated from FCA's own
        published archives, covering the schedule's whole known history
        rather than one period. It does not depend on this instance's
        `start`, `end`, or `transport`, so it does not require `fetch` to
        have run. Use `get_schema` for a single period's snapshot, or
        `get_layout` for what a fetched release actually declared.

        Parameters
        ----------
        schedule : FCASchedule or str
            The schedule to describe. A string is matched
            case-insensitively.

        Returns
        -------
        FileMetadata
            `schedule`'s canonical, cross-time field metadata.

        Raises
        ------
        ScheduleNotFoundError
            If `schedule` does not name a known FCA schedule.

        Examples
        --------
        >>> from call_report.fca.transport import PackagedArchiveTransport
        >>> report = FCACallReport(
        ...     start="2026-03-31",
        ...     end="2026-03-31",
        ...     transport=PackagedArchiveTransport(),
        ... )
        >>> metadata = report.get_file_metadata(schedule="RCB")
        >>> metadata.first_period.label
        '2000Q1'
        >>> metadata.last_period.label
        '2026Q1'
        """
        return get_fca_file_metadata(schedule=FCASchedule.coerce(value=schedule))

    def available_periods(self) -> tuple[ReportingPeriod, ...]:
        """Return every period FCA is known to publish.

        Reflects FCA's overall catalog (`call_report.fca.catalog`),
        independent of this instance's `start` and `end`. It does not
        require `fetch` to have run.

        Returns
        -------
        tuple[ReportingPeriod, ...]
            The known-available periods, oldest first.

        Examples
        --------
        >>> from call_report.fca.transport import PackagedArchiveTransport
        >>> report = FCACallReport(
        ...     start="2026-03-31",
        ...     end="2026-03-31",
        ...     transport=PackagedArchiveTransport(),
        ... )
        >>> periods = report.available_periods()
        >>> periods[0].label
        '2000Q1'
        >>> periods[-1].label
        '2026Q1'
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

        Examples
        --------
        >>> from call_report.fca.transport import PackagedArchiveTransport
        >>> report = FCACallReport(
        ...     start="2026-03-31",
        ...     end="2026-03-31",
        ...     transport=PackagedArchiveTransport(),
        ... )
        >>> schedules = report.available_schedules()
        >>> len(schedules)
        37
        >>> schedules[:3]
        (<FCASchedule.RC: 'RC'>, <FCASchedule.RC1: 'RC1'>, <FCASchedule.RCB: 'RCB'>)
        """
        return tuple(FCASchedule)

    def available_domain_datasets(self) -> tuple[FCADomainDataset, ...]:
        """Return every curated domain dataset this package ships.

        The domain-dataset counterpart to `available_schedules`. Does not
        require `fetch` to have run.

        Returns
        -------
        tuple[FCADomainDataset, ...]
            Every `FCADomainDataset` member.

        Examples
        --------
        >>> from call_report.fca.transport import PackagedArchiveTransport
        >>> report = FCACallReport(
        ...     start="2026-03-31",
        ...     end="2026-03-31",
        ...     transport=PackagedArchiveTransport(),
        ... )
        >>> report.available_domain_datasets()
        (<FCADomainDataset.LOAN_PORTFOLIO: 'loan_portfolio'>,)
        """
        return tuple(FCADomainDataset)

    def periods_available(
        self, *, schedule: FCASchedule | str
    ) -> tuple[ReportingPeriod, ...]:
        """Return the requested periods in which a schedule is present.

        The complement of `periods_missing` for the same schedule.

        Parameters
        ----------
        schedule : FCASchedule or str
            The schedule to check. A string is matched
            case-insensitively.

        Returns
        -------
        tuple[ReportingPeriod, ...]
            The subset of `periods_` that have `schedule`.

        Examples
        --------
        >>> from call_report.fca.transport import PackagedArchiveTransport
        >>> report = FCACallReport(
        ...     start="2026-03-31",
        ...     end="2026-03-31",
        ...     transport=PackagedArchiveTransport(),
        ... )
        >>> report.periods_available(schedule="RCB")
        (ReportingPeriod(year=2026, quarter=<Quarter.Q1: 1>),)
        """
        self._ensure_fetched()
        schedule_enum = FCASchedule.coerce(value=schedule)
        return self.schedules_.get(schedule_enum, ())

    def periods_missing(
        self, *, schedule: FCASchedule | str
    ) -> tuple[ReportingPeriod, ...]:
        """Return the requested periods in which a schedule is absent.

        The complement of `periods_available` for the same schedule.

        Parameters
        ----------
        schedule : FCASchedule or str
            The schedule to check. A string is matched
            case-insensitively.

        Returns
        -------
        tuple[ReportingPeriod, ...]
            The subset of `periods_` that do not have `schedule`.

        Examples
        --------
        >>> from call_report.fca.transport import PackagedArchiveTransport
        >>> report = FCACallReport(
        ...     start="2026-03-31",
        ...     end="2026-03-31",
        ...     transport=PackagedArchiveTransport(),
        ... )
        >>> report.periods_missing(schedule="RCB")
        ()
        """
        self._ensure_fetched()
        available = set(self.periods_available(schedule=schedule))
        return tuple(period for period in self.periods_ if period not in available)

    @overload
    def to_wide_format(
        self,
        *,
        schedules: Iterable[FCASchedule | str] | None = None,
        dataframe_type: None = None,
    ) -> NativeDataFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_wide_format(
        self,
        *,
        schedules: Iterable[FCASchedule | str] | None = None,
        dataframe_type: Literal["pandas"],
    ) -> pandas.DataFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_wide_format(
        self,
        *,
        schedules: Iterable[FCASchedule | str] | None = None,
        dataframe_type: Literal["pyarrow_table"],
    ) -> pyarrow.Table:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_wide_format(
        self,
        *,
        schedules: Iterable[FCASchedule | str] | None = None,
        dataframe_type: Literal["polars_dataframe"],
    ) -> polars.DataFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_wide_format(
        self,
        *,
        schedules: Iterable[FCASchedule | str] | None = None,
        dataframe_type: Literal["polars_lazyframe"],
    ) -> polars.LazyFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    def to_wide_format(
        self,
        *,
        schedules: Iterable[FCASchedule | str] | None = None,
        dataframe_type: DataFrameType | None = None,
    ) -> NativeDataFrame:
        """Stack every loaded schedule into one wide, (UNINUM, period)-grain frame.

        Produces one row per institution per period and one column per
        schedule variable. A plain (non-code) field becomes
        ``{schedule}__{variable}``. A field that repeats once per reported
        code becomes ``{schedule}__{code_column}_{code_value}__{variable}``
        (e.g. ``RCB__INV_CODE_15__BKVAL``). Works on every configured
        dataframe backend, including pyarrow, which lacks a native pivot
        (see `call_report.core._backend.pivot`).

        Parameters
        ----------
        schedules : Iterable[FCASchedule or str], optional
            The schedules to include. Each is matched case-insensitively.
            Leave this ``None`` (the default) to include every schedule
            discovered across the requested periods.
        dataframe_type : {"pandas", "pyarrow_table", "polars_lazyframe", \
"polars_dataframe"}, optional
            The dataframe type to convert the result to as a final step.
            Leave this ``None`` (the default) to get back whatever backend
            `call_report.config.get_config` currently has configured. Set
            it when the code that consumes this result needs a specific
            type, for example a pandas DataFrame while the package is
            configured to use polars.

        Returns
        -------
        NativeDataFrame
            A native dataframe of the configured backend, or of
            `dataframe_type` if it was supplied.

        Raises
        ------
        ScheduleNotFoundError
            If `schedules` resolves to zero schedules, or an explicitly
            named schedule has zero surviving periods.
        ReshapeError
            If, after melting every included schedule, the same
            ``(UNINUM, period, column)`` combination has more than one
            value, for example because of a duplicated row in the source
            data.

        Examples
        --------
        >>> from call_report.fca.transport import PackagedArchiveTransport
        >>> report = FCACallReport(
        ...     start="2026-03-31",
        ...     end="2026-03-31",
        ...     transport=PackagedArchiveTransport(),
        ... )
        >>> wide = report.to_wide_format(schedules=["RC", "RCB"])
        >>> "RCB__INV_CODE_15__BKVAL" in wide.columns
        True
        """
        return finalize_as(
            frame=self._to_wide_format(schedules=schedules),
            dataframe_type=dataframe_type,
        )

    def _to_wide_format(
        self, *, schedules: Iterable[FCASchedule | str] | None
    ) -> nw.DataFrame[Any]:
        """Build the wide-format frame, the private hook behind `to_wide_format`.

        Loads each resolved schedule via `_load_reshape_inputs`, then
        delegates the melt, concat, and pivot work to
        `call_report.fca._reshape.to_wide_format`. A schedule loaded lazily
        (``lazy=True`` configured, polars backend) is passed through as a
        `narwhals.LazyFrame` rather than collected here. The melt and
        concat steps stay lazy too, and only `to_wide_format`'s final
        `call_report.core._backend.pivot` call needs to materialize it.

        Parameters
        ----------
        schedules : Iterable[FCASchedule or str], optional
            The schedules to include, or ``None`` for every schedule
            discovered across the requested periods.

        Returns
        -------
        narwhals.DataFrame
            The eager, un-finalized wide-format frame.

        Raises
        ------
        ScheduleNotFoundError
            If `schedules` resolves to zero schedules.
        """
        frames, code_columns, trailing_columns = self._load_reshape_inputs(
            schedules=schedules
        )
        return _reshape.to_wide_format(
            frames=frames, code_columns=code_columns, trailing_columns=trailing_columns
        )

    @overload
    def to_long_format(
        self,
        *,
        schedules: Iterable[FCASchedule | str] | None = None,
        dataframe_type: None = None,
    ) -> NativeDataFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_long_format(
        self,
        *,
        schedules: Iterable[FCASchedule | str] | None = None,
        dataframe_type: Literal["pandas"],
    ) -> pandas.DataFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_long_format(
        self,
        *,
        schedules: Iterable[FCASchedule | str] | None = None,
        dataframe_type: Literal["pyarrow_table"],
    ) -> pyarrow.Table:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_long_format(
        self,
        *,
        schedules: Iterable[FCASchedule | str] | None = None,
        dataframe_type: Literal["polars_dataframe"],
    ) -> polars.DataFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_long_format(
        self,
        *,
        schedules: Iterable[FCASchedule | str] | None = None,
        dataframe_type: Literal["polars_lazyframe"],
    ) -> polars.LazyFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    def to_long_format(
        self,
        *,
        schedules: Iterable[FCASchedule | str] | None = None,
        dataframe_type: DataFrameType | None = None,
    ) -> NativeDataFrame:
        """Stack every loaded schedule into one long, tidy-shaped frame.

        Produces one row per ``(UNINUM, period, schedule, code_column,
        code_value, variable_name)``. A plain (non-code) field has
        ``code_column`` and ``code_value`` null and ``is_multiple``
        ``False``. A field that repeats once per reported code has them set
        to the code's field name and value, with ``is_multiple`` ``True``,
        matching `~call_report.fca.layout.FCALayout`'s own
        "single"/"multiple" scenario vocabulary. `value`, and `code_value`
        when present, is always ``Float64``, the most generic type that
        represents every schedule's measures. See
        `~call_report.fca.convert_long_format_to_wide_format` to pivot this
        back to `to_wide_format`'s shape.

        Columns are always returned in the order ``UNINUM``, ``period``,
        ``schedule``, ``code_column``, ``code_value``, ``variable_name``,
        ``value``, ``is_multiple``. That order is part of the contract, so a
        positional read of this frame matches one built by the other route.

        Parameters
        ----------
        schedules : Iterable[FCASchedule or str], optional
            The schedules to include. Each is matched case-insensitively.
            Leave this ``None`` (the default) to include every schedule
            discovered across the requested periods.
        dataframe_type : {"pandas", "pyarrow_table", "polars_lazyframe", \
"polars_dataframe"}, optional
            The dataframe type to convert the result to as a final step.
            Leave this ``None`` (the default) to get back whatever backend
            `call_report.config.get_config` currently has configured. Set
            it when the code that consumes this result needs a specific
            type, for example a pandas DataFrame while the package is
            configured to use polars.

        Returns
        -------
        NativeDataFrame
            A native dataframe of the configured backend, or of
            `dataframe_type` if it was supplied.

        Raises
        ------
        ScheduleNotFoundError
            If `schedules` resolves to zero schedules, or an explicitly
            named schedule has zero surviving periods.
        ReshapeError
            If ``(UNINUM, period, schedule, code_column, code_value,
            variable_name)`` is not a unique grain, for example because of
            a duplicated row in the source data.

        Examples
        --------
        >>> from call_report.fca.transport import PackagedArchiveTransport
        >>> report = FCACallReport(
        ...     start="2026-03-31",
        ...     end="2026-03-31",
        ...     transport=PackagedArchiveTransport(),
        ... )
        >>> long = report.to_long_format(schedules=["RC", "RCB"])
        >>> list(long.columns)
        ['UNINUM', 'period', 'schedule', 'code_column', 'code_value', \
'variable_name', 'value', 'is_multiple']
        """
        return finalize_as(
            frame=self._to_long_format(schedules=schedules),
            dataframe_type=dataframe_type,
        )

    def _to_long_format(
        self, *, schedules: Iterable[FCASchedule | str] | None
    ) -> nw.DataFrame[Any]:
        """Build the long-format frame, the private hook behind `to_long_format`.

        Loads each resolved schedule via `_load_reshape_inputs`, then
        delegates to `call_report.fca._reshape.to_long_format`. Unlike
        `_to_wide_format`, there is no pivot, so the melt, concat, and flag
        steps all stay lazy if a schedule was loaded lazily. The one place
        this collects is `to_long_format`'s own grain-uniqueness check,
        since checking the data is not a lazy-safe operation, so the result
        here is always eager, the same as `_to_wide_format`'s.

        Parameters
        ----------
        schedules : Iterable[FCASchedule or str], optional
            The schedules to include, or ``None`` for every schedule
            discovered across the requested periods.

        Returns
        -------
        narwhals.DataFrame
            The eager, un-finalized long-format frame.

        Raises
        ------
        ScheduleNotFoundError
            If `schedules` resolves to zero schedules.
        ReshapeError
            If the long-format grain is not unique in the source data.
        """
        frames, code_columns, trailing_columns = self._load_reshape_inputs(
            schedules=schedules
        )
        return _reshape.to_long_format(
            frames=frames, code_columns=code_columns, trailing_columns=trailing_columns
        )

    @overload
    def to_code_grain_format(
        self,
        *,
        schedules: Iterable[FCASchedule | str] | None = None,
        dataframe_type: None = None,
    ) -> NativeDataFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_code_grain_format(
        self,
        *,
        schedules: Iterable[FCASchedule | str] | None = None,
        dataframe_type: Literal["pandas"],
    ) -> pandas.DataFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_code_grain_format(
        self,
        *,
        schedules: Iterable[FCASchedule | str] | None = None,
        dataframe_type: Literal["pyarrow_table"],
    ) -> pyarrow.Table:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_code_grain_format(
        self,
        *,
        schedules: Iterable[FCASchedule | str] | None = None,
        dataframe_type: Literal["polars_dataframe"],
    ) -> polars.DataFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_code_grain_format(
        self,
        *,
        schedules: Iterable[FCASchedule | str] | None = None,
        dataframe_type: Literal["polars_lazyframe"],
    ) -> polars.LazyFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    def to_code_grain_format(
        self,
        *,
        schedules: Iterable[FCASchedule | str] | None = None,
        dataframe_type: DataFrameType | None = None,
    ) -> NativeDataFrame:
        """Stack every loaded schedule at the grain of the code it reports.

        The third architecture, alongside `to_wide_format` and
        `to_long_format`. Produces one row per ``(UNINUM, period,
        code_column, code_value)`` and one ``{schedule}__{variable}``
        column per variable, so the code stays a row key that can be
        filtered and grouped on instead of being folded into hundreds of
        column names. Two schedules reporting at the same code contribute
        columns to the same row, which is what a sub-architecture such as
        a loan-portfolio dataset needs.

        Only code-bearing schedules take part. A ``"single"``-scenario
        schedule (`~call_report.fca.layout.FCALayout`'s own vocabulary)
        reports no code, so it has no code grain. Leaving `schedules`
        unset skips those, the same leniency ``None`` already has
        elsewhere. Naming one explicitly is an error instead, since the
        request cannot be honored. A
        ``single_multiple_single`` schedule's trailing single-occurrence
        fields are institution-level in the same way and are dropped too.

        Schedules whose code columns differ are stacked, not joined.
        `code_column` is part of the grain, so RCB's ``INV_CODE`` rows and
        RCF's ``LOANSTATUS`` rows coexist, each populating only its own
        schedule's columns. Two schedules can even share a code column
        name while using different code universes (RCF's ``LOANSTATUS``
        is a performance status, RCF1's is a loan portfolio), which the
        schedule-prefixed column names keep separable.

        Parameters
        ----------
        schedules : Iterable[FCASchedule or str], optional
            The schedules to include. Each is matched case-insensitively.
            Leave this ``None`` (the default) to include every schedule
            discovered across the requested periods.
        dataframe_type : {"pandas", "pyarrow_table", "polars_lazyframe", \
"polars_dataframe"}, optional
            The dataframe type to convert the result to as a final step.
            Leave this ``None`` (the default) to get back whatever backend
            `call_report.config.get_config` currently has configured. Set
            it when the code that consumes this result needs a specific
            type, for example a pandas DataFrame while the package is
            configured to use polars.

        Returns
        -------
        NativeDataFrame
            A native dataframe of the configured backend, or of
            `dataframe_type` if it was supplied.

        Raises
        ------
        ScheduleNotFoundError
            If `schedules` resolves to zero code-bearing schedules, or an
            explicitly named schedule has zero surviving periods.
        ReshapeError
            If an explicitly named schedule has no code column, or if
            ``(UNINUM, period, code_column, code_value, column)`` is not a
            unique grain, for example because of a duplicated row in the
            source data.

        Examples
        --------
        >>> from call_report.fca.transport import PackagedArchiveTransport
        >>> report = FCACallReport(
        ...     start="2026-03-31",
        ...     end="2026-03-31",
        ...     transport=PackagedArchiveTransport(),
        ... )
        >>> code_grain = report.to_code_grain_format(schedules=["RCB", "RCF1"])
        >>> list(code_grain.columns)[:4]
        ['UNINUM', 'period', 'code_column', 'code_value']
        """
        return finalize_as(
            frame=self._to_code_grain_format(schedules=schedules),
            dataframe_type=dataframe_type,
        )

    def _to_code_grain_format(
        self, *, schedules: Iterable[FCASchedule | str] | None
    ) -> nw.DataFrame[Any]:
        """Build the code-grain frame, the private hook behind `to_code_grain_format`.

        Resolves and loads the code-bearing schedules via
        `_load_code_grain_inputs`, then delegates to
        `call_report.fca._reshape.to_code_grain_format`. Like
        `_to_wide_format`, the melt and concat steps stay lazy if a
        schedule was loaded lazily, and the final pivot is what collects.

        Parameters
        ----------
        schedules : Iterable[FCASchedule or str], optional
            The schedules to include, or ``None`` for every schedule
            discovered across the requested periods.

        Returns
        -------
        narwhals.DataFrame
            The eager, un-finalized code-grain frame.

        Raises
        ------
        ScheduleNotFoundError
            If `schedules` resolves to zero code-bearing schedules.
        ReshapeError
            If an explicitly named schedule has no code column.
        """
        frames, code_columns, trailing_columns = self._load_code_grain_inputs(
            schedules=schedules
        )
        return _reshape.to_code_grain_format(
            frames=frames, code_columns=code_columns, trailing_columns=trailing_columns
        )

    def _load_code_grain_inputs(
        self, *, schedules: Iterable[FCASchedule | str] | None
    ) -> tuple[
        dict[str, FrameOrLazy], dict[str, str | None], dict[str, tuple[str, ...]]
    ]:
        """Narrow `schedules` to the code-bearing ones, then load each.

        The code-grain counterpart to `_load_reshape_inputs`, which it
        hands the surviving schedules to. Narrowing happens before
        loading, so a schedule that cannot contribute is never read off
        disk.

        Whether a schedule has a code column is read from its layout, so
        a schedule with no available period is passed through untouched.
        `_load` then raises its own `ScheduleNotFoundError` for it, rather
        than this reporting a missing schedule as a non-coded one.

        Parameters
        ----------
        schedules : Iterable[FCASchedule or str], optional
            The schedules to include, or ``None`` for every schedule
            discovered across the requested periods.

        Returns
        -------
        tuple[dict[str, FrameOrLazy], dict[str, str or None], \
dict[str, tuple[str, ...]]]
            `frames`, `code_columns`, and `trailing_columns`, each keyed
            by schedule root name.

        Raises
        ------
        ScheduleNotFoundError
            If `schedules` resolves to zero code-bearing schedules.
        ReshapeError
            If an explicitly named schedule has no code column.
        """
        coded: list[FCASchedule] = []
        uncoded: list[FCASchedule] = []
        for schedule in self._resolve_reshape_schedules(schedules=schedules):
            # The first test short-circuits the second: a schedule with no
            # available period has no layout to read a code column from.
            if (
                not self.periods_available(schedule=schedule)
                or self._layout_for_schedule(schedule=schedule).multi_columns
            ):
                coded.append(schedule)
            else:
                uncoded.append(schedule)

        if schedules is not None and uncoded:
            names = sorted(schedule.value for schedule in uncoded)
            raise ReshapeError(
                f"Schedules {names} report no code, so they have no code grain; "
                "drop them from `schedules`, or leave `schedules` unset to "
                "include every code-bearing schedule automatically."
            )
        if not coded:
            raise ScheduleNotFoundError(
                "No schedules to reshape: `schedules` resolved to no code-bearing "
                "schedule. Either an empty `schedules` was passed, or no schedule "
                "discovered across this instance's requested periods reports a "
                "code; see errors_ for details."
            )
        return self._load_reshape_inputs(schedules=coded)

    @overload
    def to_domain_dataset(
        self,
        *,
        domain_dataset: FCADomainDataset | str,
        include_totals: bool = False,
        wide: bool = False,
        dataframe_type: None = None,
    ) -> NativeDataFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_domain_dataset(
        self,
        *,
        domain_dataset: FCADomainDataset | str,
        include_totals: bool = False,
        wide: bool = False,
        dataframe_type: Literal["pandas"],
    ) -> pandas.DataFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_domain_dataset(
        self,
        *,
        domain_dataset: FCADomainDataset | str,
        include_totals: bool = False,
        wide: bool = False,
        dataframe_type: Literal["pyarrow_table"],
    ) -> pyarrow.Table:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_domain_dataset(
        self,
        *,
        domain_dataset: FCADomainDataset | str,
        include_totals: bool = False,
        wide: bool = False,
        dataframe_type: Literal["polars_dataframe"],
    ) -> polars.DataFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_domain_dataset(
        self,
        *,
        domain_dataset: FCADomainDataset | str,
        include_totals: bool = False,
        wide: bool = False,
        dataframe_type: Literal["polars_lazyframe"],
    ) -> polars.LazyFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    def to_domain_dataset(
        self,
        *,
        domain_dataset: FCADomainDataset | str,
        include_totals: bool = False,
        wide: bool = False,
        dataframe_type: DataFrameType | None = None,
    ) -> NativeDataFrame:
        """Build one curated domain dataset over the requested periods.

        A domain dataset is a view this package curates rather than
        derives: which schedules compose it, which code each row is keyed
        by, and what every output column is called are all chosen. Rows
        are keyed by ``(UNINUM, period, code_column, code_value)``, and
        each column is named for what it measures, such as ``charge_off``
        or ``allowance``, with no schedule prefix, unlike
        `to_code_grain_format`. Use
        `call_report.fca.get_domain_dataset_codes` to turn the codes into
        names.

        Only the schedules a dataset declares are loaded, and only those
        of them present in the requested range. A range that spans a
        schedule split loads both sides and keeps the series in one
        column.

        Parameters
        ----------
        domain_dataset : FCADomainDataset or str
            The domain dataset to look up. A string is matched
            case-insensitively.
        include_totals : bool, default False
            Whether to keep the codes the dataset marks as reported
            subtotals. The default excludes them, so an aggregation over
            every returned row does not double count. Set this ``True``
            to also get the source's own reported subtotal rows.
        wide : bool, default False
            Whether to pivot every code into its own set of columns.
            The default keys rows by ``(UNINUM, period, code_column,
            code_value)``, one column per measure. Set this ``True`` to
            key rows by ``(UNINUM, period)`` alone, with one column per
            ``{code_value}__{measure}`` combination, e.g.
            ``100__accruing``.
        dataframe_type : {"pandas", "pyarrow_table", "polars_lazyframe", \
"polars_dataframe"}, optional
            The dataframe type to convert the result to as a final step.
            Leave this ``None`` (the default) to get back whatever backend
            `call_report.config.get_config` currently has configured. Set
            it when the code that consumes this result needs a specific
            type, for example a pandas DataFrame while the package is
            configured to use polars.

        Returns
        -------
        NativeDataFrame
            A native dataframe of the configured backend, or of
            `dataframe_type` if it was supplied.

        Raises
        ------
        DomainDatasetNotFoundError
            If `domain_dataset` does not name a shipped dataset.
        ScheduleNotFoundError
            If none of the dataset's schedules is present in any
            requested period.
        ReshapeError
            If the resulting grain (``(UNINUM, period, code_column,
            code_value, column)``, or ``(UNINUM, period, column)`` when
            `wide` is ``True``) is not unique, for example because of a
            duplicated row in the source data.

        Examples
        --------
        >>> from call_report.fca.transport import PackagedArchiveTransport
        >>> report = FCACallReport(
        ...     start="2026-03-31",
        ...     end="2026-03-31",
        ...     transport=PackagedArchiveTransport(),
        ... )
        >>> loans = report.to_domain_dataset(domain_dataset="loan_portfolio")
        >>> list(loans.columns)[:4]
        ['UNINUM', 'period', 'code_column', 'code_value']
        >>> agribusiness = loans[
        ...     (loans["UNINUM"] == 620000) & (loans["code_value"] == 110.0)
        ... ].iloc[0]
        >>> float(agribusiness["accruing"])
        3265454.0
        >>> wide = report.to_domain_dataset(
        ...     domain_dataset="loan_portfolio", wide=True
        ... )
        >>> "110__accruing" in wide.columns
        True
        """
        return finalize_as(
            frame=self._to_domain_dataset(
                domain_dataset=domain_dataset, include_totals=include_totals, wide=wide
            ),
            dataframe_type=dataframe_type,
        )

    def _to_domain_dataset(
        self,
        *,
        domain_dataset: FCADomainDataset | str,
        include_totals: bool,
        wide: bool,
    ) -> nw.DataFrame[Any]:
        """Build the curated frame, the private hook behind `to_domain_dataset`.

        Narrows the dataset's declared schedules to those the requested
        range actually has, loads them through `_load_reshape_inputs`,
        then melts, decodes, and pivots them into the dataset's own
        terms. Unlike `to_code_grain_format`, the pivot is on the
        curated `variable_name` alone, so an output column is named for
        what it measures rather than for the schedule it came from.
        That is what lets two schedules covering different eras fill one
        continuous column.

        `_load_code_grain_inputs` is deliberately not used here. It rejects
        a schedule whose layout declares no code column, and a curated
        dataset draws on exactly such schedules, supplying the codes from
        its own definition instead.

        Everything before `pivot` stays lazy if the loaded schedules are
        lazy. `pivot` is the one step that must collect, and it also
        enforces that `_reshape.CODE_GRAIN_INDEX` plus the output column
        is a unique grain. `wide` runs a second pivot afterward, via
        `_reshape.pivot_domain_dataset_wide`, on the already-eager result.

        Parameters
        ----------
        domain_dataset : FCADomainDataset or str
            The domain dataset to look up. A string is matched
            case-insensitively.
        include_totals : bool
            Whether to keep the dataset's reported subtotal codes.
        wide : bool
            Whether to pivot every code into its own set of columns.

        Returns
        -------
        narwhals.DataFrame
            The eager, un-finalized curated frame.

        Raises
        ------
        DomainDatasetNotFoundError
            If `domain_dataset` does not name a shipped dataset.
        ScheduleNotFoundError
            If none of the dataset's schedules is present in any
            requested period.
        ReshapeError
            If `_reshape.CODE_GRAIN_INDEX` plus the output column is not
            a unique grain, if `wide` is ``True`` and `_reshape.RESHAPE_INDEX`
            plus the output column is not a unique grain, or if the
            requested schedules contributed no measurement columns at
            all, for example because every one resolved to zero rows
            for the requested periods.
        """
        dataset = get_fca_domain_dataset(domain_dataset=domain_dataset)
        self._ensure_fetched()
        available = [
            schedule
            for schedule in dataset.schedules
            if self.periods_available(schedule=schedule)
        ]
        if not available:
            raise ScheduleNotFoundError(
                f"None of {list(dataset.schedules)}, the schedules the "
                f"{dataset.name!r} domain dataset draws on, was found in any period "
                f"of {self.periods_[0].label}-{self.periods_[-1].label}."
            )
        frames, code_columns, trailing_columns = self._load_reshape_inputs(
            schedules=available
        )
        decoded = [
            _reshape.apply_domain_dataset_decoding(
                frame=_reshape.melt_schedule_frame(
                    frame=frame,
                    schedule=schedule,
                    code_column=code_columns[schedule],
                    trailing_columns=trailing_columns[schedule],
                ),
                source=dataset.source_by_schedule[schedule],
                code_column=dataset.code_column,
            )
            for schedule, frame in frames.items()
        ]
        combined = concat(frames=decoded, how="union")
        if not include_totals:
            combined = _reshape.exclude_reported_totals(
                frame=combined, total_codes=dataset.total_codes
            )
        pivoted = pivot(
            frame=combined,
            on="variable_name",
            index=list(_reshape.CODE_GRAIN_INDEX),
            values="value",
        )
        _reshape.assert_pivot_has_measurements(pivoted=pivoted)
        result = _reshape.add_derived_columns(frame=pivoted, derived=dataset.derived)
        if wide:
            return _reshape.pivot_domain_dataset_wide(frame=result)
        return result

    def _load_reshape_inputs(
        self, *, schedules: Iterable[FCASchedule | str] | None
    ) -> tuple[
        dict[str, FrameOrLazy], dict[str, str | None], dict[str, tuple[str, ...]]
    ]:
        """Resolve `schedules` and load each one's frame, code, and trailing columns.

        Shared by `_to_wide_format`, `_to_long_format`, and
        `_to_domain_dataset`, which differ only in which
        `call_report.fca._reshape` function they hand this to. A
        schedule loaded lazily (``lazy=True`` configured, polars
        backend) is passed through as a `narwhals.LazyFrame` rather than
        collected here.

        Parameters
        ----------
        schedules : Iterable[FCASchedule or str], optional
            The schedules to include, or ``None`` for every schedule
            discovered across the requested periods.

        Returns
        -------
        tuple[dict[str, FrameOrLazy], dict[str, str or None], \
dict[str, tuple[str, ...]]]
            `frames`, `code_columns`, and `trailing_columns`, each keyed
            by schedule root name.

        Raises
        ------
        ScheduleNotFoundError
            If `schedules` resolves to zero schedules.
        """
        resolved = self._resolve_reshape_schedules(schedules=schedules)
        if not resolved:
            raise ScheduleNotFoundError(
                "No schedules to reshape: `schedules` resolved to an empty "
                "selection. Either an empty `schedules` was passed, or "
                "this instance has no schedules discovered across its "
                "requested periods; see errors_ for details."
            )

        frames: dict[str, FrameOrLazy] = {}
        code_columns: dict[str, str | None] = {}
        trailing_columns: dict[str, tuple[str, ...]] = {}
        for schedule in resolved:
            frames[schedule.value] = nw.from_native(self._load(schedule=schedule))
            layout = self._layout_for_schedule(schedule=schedule)
            code_columns[schedule.value] = (
                layout.multi_columns[0] if layout.multi_columns else None
            )
            trailing_columns[schedule.value] = layout.trailing_columns
        return frames, code_columns, trailing_columns

    def _resolve_reshape_schedules(
        self, *, schedules: Iterable[FCASchedule | str] | None
    ) -> tuple[FCASchedule, ...]:
        """Resolve a reshape method's `schedules` argument to a concrete tuple.

        ``None`` mirrors `_load_all`'s lenient behavior and selects every
        schedule discovered across the requested periods. An explicit
        iterable is coerced and used as-is, so a named schedule with zero
        surviving periods surfaces as `ScheduleNotFoundError` from `_load`,
        matching `load`'s stricter behavior.

        Parameters
        ----------
        schedules : Iterable[FCASchedule or str], optional
            The schedules to include, or ``None`` for every schedule
            discovered across the requested periods.

        Returns
        -------
        tuple[FCASchedule, ...]
            The resolved schedules, in the order they should be loaded.
        """
        self._ensure_fetched()
        if schedules is None:
            return tuple(self.schedules_)
        return tuple(FCASchedule.coerce(value=item) for item in schedules)

    def _layout_for_schedule(self, *, schedule: FCASchedule) -> FCALayout:
        """Return `schedule`'s layout, from its first available period.

        Used by `_load_reshape_inputs` to determine a schedule's code and
        trailing columns, and by `_load_code_grain_inputs` to decide
        whether a schedule has a code at all. Whether a schedule has a
        code column, and its overall scenario, is stable across periods,
        so the first period is representative. A layout's own code list
        can be restated without the data changing, as RCF1's was in 2015,
        which is a reason to read codes from the data rather than from
        the layout text.

        Parameters
        ----------
        schedule : FCASchedule
            The schedule to inspect.

        Returns
        -------
        FCALayout
            `schedule`'s layout, from its first available period.
        """
        period = self.periods_available(schedule=schedule)[0]
        return parse_layout(
            path=self.releases_[period].files[schedule.value].layout_path
        )


def _with_period_column(
    *, frame: nw.DataFrame[Any], period: ReportingPeriod
) -> nw.DataFrame[Any]:
    """Attach a ``period`` column holding a period's quarter-end date.

    Used by `_load` and `_load_institutions` so every stacked frame can be
    traced back to which period each row came from.

    Parameters
    ----------
    frame : narwhals.DataFrame
        The frame to annotate.
    period : ReportingPeriod
        The period `frame` was parsed from.

    Returns
    -------
    narwhals.DataFrame
        `frame` with an added ``period`` column, of the backend's own
        date dtype (see `call_report.core._backend.date_dtype`).
    """
    return frame.with_columns(
        nw.lit(period.period_end).cast(date_dtype()).alias("period")
    )


def _build_schedule_presence_map(
    *, releases: dict[ReportingPeriod, FCAReleaseManifest]
) -> dict[FCASchedule, tuple[ReportingPeriod, ...]]:
    """Build a schedule-to-periods presence map from resolved releases.

    A root name that is present in a release but not in `FCASchedule` is
    ignored rather than raising.

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
                schedule = FCASchedule.coerce(value=root)
            except ScheduleNotFoundError:
                continue
            working.setdefault(schedule, []).append(period)
    return {schedule: tuple(sorted(periods)) for schedule, periods in working.items()}
