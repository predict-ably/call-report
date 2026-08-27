"""The estimator-style entry point for FCA Call Report data."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self, overload

import narwhals as nw

from call_report.core import BaseCallReport, PeriodRange, ReportingPeriod
from call_report.core._backend import (
    DataFrameType,
    FrameOrLazy,
    concat,
    date_dtype,
    finalize,
    finalize_as,
)
from call_report.exceptions import (
    CallReportError,
    DownloadError,
    InvalidPeriodError,
    LayoutParseError,
    ScheduleNotFoundError,
)
from call_report.fca import _reshape
from call_report.fca._discovery import ReleaseFiles, scan_release
from call_report.fca.catalog import construct_fca_download_url
from call_report.fca.enums import FCASchedule, coerce_fca_call_report_schedule
from call_report.fca.institutions import INSTITUTIONS_ROOT, _read_institutions_frame
from call_report.fca.layout import FCALayout, parse_layout
from call_report.fca.reader import _read_schedule_frame
from call_report.fca.transport import FCATransport

if TYPE_CHECKING:
    import pandas as pd
    import polars as pl
    import pyarrow as pa

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
        schedule_enum = coerce_fca_call_report_schedule(value=schedule)

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
    ) -> pd.DataFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_wide_format(
        self,
        *,
        schedules: Iterable[FCASchedule | str] | None = None,
        dataframe_type: Literal["pyarrow_table"],
    ) -> pa.Table:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_wide_format(
        self,
        *,
        schedules: Iterable[FCASchedule | str] | None = None,
        dataframe_type: Literal["polars_dataframe"],
    ) -> pl.DataFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_wide_format(
        self,
        *,
        schedules: Iterable[FCASchedule | str] | None = None,
        dataframe_type: Literal["polars_lazyframe"],
    ) -> pl.LazyFrame:  # numpydoc ignore=GL08
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
    ) -> pd.DataFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_long_format(
        self,
        *,
        schedules: Iterable[FCASchedule | str] | None = None,
        dataframe_type: Literal["pyarrow_table"],
    ) -> pa.Table:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_long_format(
        self,
        *,
        schedules: Iterable[FCASchedule | str] | None = None,
        dataframe_type: Literal["polars_dataframe"],
    ) -> pl.DataFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_long_format(
        self,
        *,
        schedules: Iterable[FCASchedule | str] | None = None,
        dataframe_type: Literal["polars_lazyframe"],
    ) -> pl.LazyFrame:  # numpydoc ignore=GL08
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

    def _load_reshape_inputs(
        self, *, schedules: Iterable[FCASchedule | str] | None
    ) -> tuple[
        dict[str, FrameOrLazy], dict[str, str | None], dict[str, tuple[str, ...]]
    ]:
        """Resolve `schedules` and load each one's frame, code, and trailing columns.

        Shared by `_to_wide_format` and `_to_long_format`, which differ
        only in which `call_report.fca._reshape` function they hand this
        to. A schedule loaded lazily (``lazy=True`` configured, polars
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
        """Resolve `to_wide_format`/`to_long_format`'s `schedules` to a concrete tuple.

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
        return tuple(coerce_fca_call_report_schedule(value=item) for item in schedules)

    def _layout_for_schedule(self, *, schedule: FCASchedule) -> FCALayout:
        """Return `schedule`'s layout, from its first available period.

        Used by `_to_wide_format` to determine a schedule's code and
        trailing columns. Whether a schedule has a code column, and its
        overall scenario, is stable across periods. Only a code list's own
        contents drift over time, as with RCF1's 2015 meaning change, so
        the first period is representative.

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
                schedule = coerce_fca_call_report_schedule(value=root)
            except ScheduleNotFoundError:
                continue
            working.setdefault(schedule, []).append(period)
    return {schedule: tuple(sorted(periods)) for schedule, periods in working.items()}
