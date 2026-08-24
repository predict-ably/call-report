"""The shared, source-agnostic call report interface.

Every source-specific entry point (e.g. ``FCACallReport``) implements
:class:`BaseCallReport` so callers can switch regimes with minimal friction,
and inherits the sklearn-style `get_params`/`set_params`/`__repr__`
behavior for free.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Literal, Self, final, overload

from call_report.core._backend import DataFrameType, convert_dataframe_type
from call_report.core._periods import ReportingPeriod

if TYPE_CHECKING:
    import pandas as pd
    import polars as pl
    import pyarrow as pa

    from call_report.core._backend import NativeDataFrame

_REPR_LINE_LENGTH = 88  # matches this project's configured ruff line-length
_REPR_INDENT = " " * 4


def _format_repr(*, class_name: str, params: dict[str, Any]) -> str:
    """Render a class name and its parameters as a repr string.

    Mirrors scikit-learn's estimator repr style: everything on one line
    when it fits within the project's configured line length, otherwise
    one indented ``name=value,`` per line with the closing parenthesis on
    its own line.

    Parameters
    ----------
    class_name : str
        The name to render before the opening parenthesis.
    params : dict[str, Any]
        Parameter name/value pairs to render, in iteration order.

    Returns
    -------
    str
        The rendered repr string.
    """
    rendered = [f"{name}={value!r}" for name, value in params.items()]
    single_line = f"{class_name}({', '.join(rendered)})"
    if len(single_line) <= _REPR_LINE_LENGTH:
        return single_line

    lines = [f"{class_name}("]
    lines.extend(f"{_REPR_INDENT}{item}," for item in rendered)
    lines.append(")")
    return "\n".join(lines)


class BaseCallReport(ABC):
    """Abstract base for every source-specific call report entry point.

    Concrete subclasses (one per :class:`~call_report.core.Source`) accept
    `start` and `end` as their first two constructor parameters, store all
    constructor parameters verbatim as identically-named attributes (doing
    no validation work in ``__init__``, per the sklearn estimator
    convention), and implement the abstract methods below.

    Examples
    --------
    >>> from call_report.core import BaseCallReport
    >>> from call_report.fca import FCACallReport
    >>> from call_report.fca.transport import PackagedArchiveTransport
    >>> report = FCACallReport(
    ...     start="2026-03-31",
    ...     end="2026-03-31",
    ...     transport=PackagedArchiveTransport(),
    ... )
    >>> isinstance(report, BaseCallReport)
    True
    """

    @abstractmethod
    def fetch(self) -> Self:
        """Validate this instance's parameters and resolve its release files.

        Populates trailing-underscore attributes describing what was found
        (e.g. the resolved periods and any non-fatal issues encountered).
        Subsequent calls to `load`-family methods call this automatically
        if it has not run yet.

        Returns
        -------
        Self
            This instance, to support method chaining.

        Examples
        --------
        >>> from call_report.fca import FCACallReport
        >>> from call_report.fca.transport import PackagedArchiveTransport
        >>> report = FCACallReport(
        ...     start="2026-03-31",
        ...     end="2026-03-31",
        ...     transport=PackagedArchiveTransport(),
        ... )
        >>> report.fetch() is report
        True
        """

    @abstractmethod
    def _load(self, *, schedule: Any) -> NativeDataFrame:
        """Load a single schedule, stacked across every requested period.

        Every concrete source implements this method instead of `load`
        itself. It returns an already-finalized native dataframe of the
        configured backend and applies no `dataframe_type` conversion.
        `load` applies that conversion centrally, once, after calling
        this method.

        Parameters
        ----------
        schedule : Any
            The schedule to load, in whatever form the concrete source
            accepts (typically an enum member or a case-insensitive name).

        Returns
        -------
        NativeDataFrame
            A native dataframe of the configured backend.
        """

    @overload
    def load(
        self, *, schedule: Any, dataframe_type: None = None
    ) -> NativeDataFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def load(
        self, *, schedule: Any, dataframe_type: Literal["pandas"]
    ) -> pd.DataFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def load(
        self, *, schedule: Any, dataframe_type: Literal["pyarrow_table"]
    ) -> pa.Table:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def load(
        self, *, schedule: Any, dataframe_type: Literal["polars_dataframe"]
    ) -> pl.DataFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def load(
        self, *, schedule: Any, dataframe_type: Literal["polars_lazyframe"]
    ) -> pl.LazyFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @final
    def load(
        self, *, schedule: Any, dataframe_type: DataFrameType | None = None
    ) -> NativeDataFrame:
        """Load a single schedule, stacked across every requested period.

        Concrete sources implement `_load` rather than this method. `load`
        itself cannot be overridden, so every source applies
        `dataframe_type` the same way, in one place.

        Parameters
        ----------
        schedule : Any
            The schedule to load, in whatever form the concrete source
            accepts (typically an enum member or a case-insensitive name).
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

        Examples
        --------
        >>> from call_report.fca import FCACallReport
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
        return convert_dataframe_type(
            data=self._load(schedule=schedule), dataframe_type=dataframe_type
        )

    @abstractmethod
    def _load_all(self) -> dict[Any, NativeDataFrame]:
        """Load every schedule discovered across the requested periods.

        Every concrete source implements this method instead of
        `load_all` itself. It returns a mapping from schedule to an
        already-finalized native dataframe, typically built by calling
        `_load` per schedule, and applies no `dataframe_type` conversion.
        `load_all` applies that conversion centrally, once per value,
        after calling this method.

        Returns
        -------
        dict[Any, NativeDataFrame]
            A mapping from schedule to its stacked native dataframe.
        """

    @overload
    def load_all(
        self, *, dataframe_type: None = None
    ) -> dict[Any, NativeDataFrame]:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def load_all(
        self, *, dataframe_type: Literal["pandas"]
    ) -> dict[Any, pd.DataFrame]:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def load_all(
        self, *, dataframe_type: Literal["pyarrow_table"]
    ) -> dict[Any, pa.Table]:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def load_all(
        self, *, dataframe_type: Literal["polars_dataframe"]
    ) -> dict[Any, pl.DataFrame]:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def load_all(
        self, *, dataframe_type: Literal["polars_lazyframe"]
    ) -> dict[Any, pl.LazyFrame]:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @final
    def load_all(
        self, *, dataframe_type: DataFrameType | None = None
    ) -> dict[Any, Any]:
        """Load every schedule discovered across the requested periods.

        Equivalent to calling `load` once per schedule returned by
        `available_schedules` that is actually present in range. Concrete
        sources implement `_load_all` rather than this method. `load_all`
        itself cannot be overridden, so every source applies
        `dataframe_type` the same way, in one place.

        Parameters
        ----------
        dataframe_type : {"pandas", "pyarrow_table", "polars_lazyframe", \
"polars_dataframe"}, optional
            The dataframe type to convert every result to. Leave this
            ``None`` (the default) to get back whatever backend
            `call_report.config.get_config` currently has configured.

        Returns
        -------
        dict[Any, NativeDataFrame]
            A mapping from schedule to its stacked native dataframe.

        Examples
        --------
        >>> from call_report.fca import FCACallReport
        >>> from call_report.fca.transport import PackagedArchiveTransport
        >>> report = FCACallReport(
        ...     start="2026-03-31",
        ...     end="2026-03-31",
        ...     transport=PackagedArchiveTransport(),
        ... )
        >>> frames = report.load_all()
        >>> len(frames)
        35
        """
        return {
            schedule: convert_dataframe_type(data=frame, dataframe_type=dataframe_type)
            for schedule, frame in self._load_all().items()
        }

    @abstractmethod
    def _load_institutions(self) -> NativeDataFrame:
        """Load the institution roster, stacked across every requested period.

        Every concrete source implements this method instead of
        `load_institutions` itself. It returns an already-finalized
        native dataframe of the configured backend and applies no
        `dataframe_type` conversion. `load_institutions` applies that
        conversion centrally, once, after calling this method.

        Returns
        -------
        NativeDataFrame
            A native dataframe of the configured backend.
        """

    @overload
    def load_institutions(
        self, *, dataframe_type: None = None
    ) -> NativeDataFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def load_institutions(
        self, *, dataframe_type: Literal["pandas"]
    ) -> pd.DataFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def load_institutions(
        self, *, dataframe_type: Literal["pyarrow_table"]
    ) -> pa.Table:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def load_institutions(
        self, *, dataframe_type: Literal["polars_dataframe"]
    ) -> pl.DataFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def load_institutions(
        self, *, dataframe_type: Literal["polars_lazyframe"]
    ) -> pl.LazyFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @final
    def load_institutions(
        self, *, dataframe_type: DataFrameType | None = None
    ) -> NativeDataFrame:
        """Load the institution roster, stacked across every requested period.

        The roster is handled separately from `load` since it describes
        institutions themselves rather than a financial schedule. Concrete
        sources implement `_load_institutions` rather than this method.
        `load_institutions` itself cannot be overridden, so every source
        applies `dataframe_type` the same way, in one place.

        Parameters
        ----------
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

        Examples
        --------
        >>> from call_report.fca import FCACallReport
        >>> from call_report.fca.transport import PackagedArchiveTransport
        >>> report = FCACallReport(
        ...     start="2026-03-31",
        ...     end="2026-03-31",
        ...     transport=PackagedArchiveTransport(),
        ... )
        >>> institutions = report.load_institutions()
        >>> institutions.shape
        (64, 13)
        """
        return convert_dataframe_type(
            data=self._load_institutions(), dataframe_type=dataframe_type
        )

    @abstractmethod
    def get_layout(self, *, schedule: Any, period: Any = None) -> Any:
        """Return the variable-metadata layout for a schedule.

        Layouts can drift across periods (new variables added, scenarios
        changing), so callers can inspect either a single period or the
        whole requested range at once.

        Parameters
        ----------
        schedule : Any
            The schedule to describe.
        period : Any, optional
            A specific period to describe. If omitted, implementations
            typically return the layout for every period in range that has
            the schedule.

        Returns
        -------
        Any
            A single layout object, or a mapping of period to layout,
            depending on whether `period` was supplied.

        Examples
        --------
        >>> from call_report.fca import FCACallReport
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

    @abstractmethod
    def available_periods(self) -> tuple[ReportingPeriod, ...]:
        """Return every period the source is known to publish.

        This reflects the source's overall catalog, independent of
        whatever `start`/`end` this particular instance was constructed
        with.

        Returns
        -------
        tuple[ReportingPeriod, ...]
            The known-available periods, oldest first.

        Examples
        --------
        >>> from call_report.fca import FCACallReport
        >>> from call_report.fca.transport import PackagedArchiveTransport
        >>> report = FCACallReport(
        ...     start="2026-03-31",
        ...     end="2026-03-31",
        ...     transport=PackagedArchiveTransport(),
        ... )
        >>> periods = report.available_periods()
        >>> periods[0].label
        '2000Q1'
        """

    @abstractmethod
    def available_schedules(self) -> tuple[Any, ...]:
        """Return every schedule the source's format has ever used.

        Like `available_periods`, this reflects the source's overall
        catalog rather than what this particular instance requested.

        Returns
        -------
        tuple[Any, ...]
            The known schedules, in whatever form the concrete source uses.

        Examples
        --------
        >>> from call_report.fca import FCACallReport
        >>> from call_report.fca.transport import PackagedArchiveTransport
        >>> report = FCACallReport(
        ...     start="2026-03-31",
        ...     end="2026-03-31",
        ...     transport=PackagedArchiveTransport(),
        ... )
        >>> len(report.available_schedules())
        37
        """

    def get_params(self, *, deep: bool = True) -> dict[str, Any]:
        """Return this instance's constructor parameters and current values.

        Parameter names are discovered by introspecting the concrete
        subclass's ``__init__`` signature, so subclasses never need to
        redeclare them here.

        Parameters
        ----------
        deep : bool, default True
            Reserved for future nested-estimator composition, matching the
            sklearn convention. It has no effect for the sources currently
            in this package.

        Returns
        -------
        dict[str, Any]
            A mapping from constructor parameter name to its current value.

        Examples
        --------
        >>> from call_report.fca import FCACallReport
        >>> from call_report.fca.transport import PackagedArchiveTransport
        >>> report = FCACallReport(
        ...     start="2026-03-31",
        ...     end="2026-03-31",
        ...     transport=PackagedArchiveTransport(),
        ... )
        >>> report.get_params()["start"]
        '2026-03-31'
        >>> sorted(report.get_params())
        ['end', 'schema_policy', 'start', 'transport']
        """
        del deep
        signature = inspect.signature(type(self).__init__)
        param_names = [name for name in signature.parameters if name != "self"]
        return {name: getattr(self, name) for name in param_names}

    def set_params(self, **params: Any) -> Self:
        """Update one or more constructor parameters in place.

        This does not re-run `fetch`, so any previously fetched state
        becomes stale until `fetch` is called again.

        Parameters
        ----------
        **params : Any
            Parameter name/value pairs. Each name must be one of this
            instance's constructor parameters.

        Returns
        -------
        Self
            This instance, to support method chaining.

        Raises
        ------
        ValueError
            If a name in `params` is not a valid constructor parameter.

        Examples
        --------
        >>> from call_report.fca import FCACallReport
        >>> from call_report.fca.transport import PackagedArchiveTransport
        >>> report = FCACallReport(
        ...     start="2026-03-31",
        ...     end="2026-03-31",
        ...     transport=PackagedArchiveTransport(),
        ... )
        >>> report.set_params(schema_policy="strict") is report
        True
        >>> report.schema_policy
        'strict'
        """
        valid_names = set(self.get_params())
        unknown = sorted(set(params) - valid_names)
        if unknown:
            raise ValueError(
                f"Invalid parameter(s) {unknown}; valid parameters are "
                f"{sorted(valid_names)}."
            )
        for name, value in params.items():
            setattr(self, name, value)
        return self

    def __repr__(self) -> str:
        """Return a repr echoing this instance's current constructor parameters.

        Wraps onto multiple indented lines if the single-line form would
        exceed this project's configured line length.

        Returns
        -------
        str
            A string of the form ``ClassName(param1=value1, param2=value2)``.

        Examples
        --------
        >>> from call_report.fca import FCACallReport
        >>> from call_report.fca.transport import PackagedArchiveTransport
        >>> report = FCACallReport(
        ...     start="2026-03-31",
        ...     end="2026-03-31",
        ...     transport=PackagedArchiveTransport(),
        ... )
        >>> report  # doctest: +ELLIPSIS
        FCACallReport(
            start='2026-03-31',
            end='2026-03-31',
            schema_policy='union',
            transport=PackagedArchiveTransport(...),
        )
        """
        return _format_repr(class_name=type(self).__name__, params=self.get_params())
