"""Every FCA charter's history, held as one collection keyed by UNINUM.

:class:`FCAInstitutionRegistry` is built from FCA's quarterly institution
rosters (``INST``). Each roster lists one row per charter that filed that
quarter. The registry groups every row across every quarter by UNINUM and
builds one :class:`~call_report.fca.FCAInstitution` per UNINUM.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from datetime import date
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, overload

import narwhals as nw

from call_report.config import DataFrameBackend
from call_report.core._backend import DataFrameType
from call_report.core._periods import ReportingPeriod
from call_report.core._schema import _coerce_period, _rows
from call_report.exceptions import InstitutionError, PeriodNotAvailableError
from call_report.fca._institution import (
    FCAInstitution,
    FCAInstitutionSnapshot,
    _check_row,
    _code,
    _columns_to_native,
    _empty_columns,
    _institution_from_dict,
    _institution_to_dict,
)

if TYPE_CHECKING:
    import pandas
    import polars
    import pyarrow

    from call_report.core._backend import NativeDataFrame


class FCAInstitutionRegistry(Mapping[int, FCAInstitution]):
    """An immutable mapping of UNINUM to that charter's `FCAInstitution`.

    Iterating yields UNINUMs in ascending order. Most callers build a
    registry with `from_dataframe` or `from_rosters` rather than from
    `FCAInstitution` objects directly.

    Parameters
    ----------
    institutions : Iterable[FCAInstitution]
        The charters making up the registry, in any order.

    Raises
    ------
    InstitutionError
        If `institutions` is empty, or two share a UNINUM.

    See Also
    --------
    FCAInstitution : One charter's history.

    Examples
    --------
    >>> from call_report.fca import FCAInstitutionRegistry
    >>> texas = {
    ...     "UNINUM": 610000,
    ...     "SYSTEM": 6,
    ...     "DIST": 10,
    ...     "ASSOC": 0,
    ...     "SHORTNAME": "FCB of Texas",
    ...     "MAIL_ADDR": "P.O. Box 202590",
    ...     "STREET_ADDR": "4801 Plaza on the Lake Drive",
    ...     "CITY": "Austin",
    ...     "STATE": "TX",
    ...     "ZIP": "78746",
    ... }
    >>> agfirst = {
    ...     "UNINUM": 620000,
    ...     "SYSTEM": 6,
    ...     "DIST": 20,
    ...     "ASSOC": 0,
    ...     "SHORTNAME": "AgFirst FCB",
    ...     "MAIL_ADDR": "P. O. Box 1499",
    ...     "STREET_ADDR": "1901 Main Street",
    ...     "CITY": "Columbia",
    ...     "STATE": "SC",
    ...     "ZIP": "29201",
    ... }
    >>> registry = FCAInstitutionRegistry.from_rosters(
    ...     rosters=[("2026-03-31", [texas, agfirst])]
    ... )
    >>> list(registry)
    [610000, 620000]
    >>> registry[620000].most_recent_short_name
    'AgFirst FCB'
    """

    def __init__(self, *, institutions: Iterable[FCAInstitution]) -> None:
        by_uninum: dict[int, FCAInstitution] = {}
        for institution in institutions:
            if institution.uninum in by_uninum:
                raise InstitutionError(
                    f"UNINUM {institution.uninum} appears more than once."
                )
            by_uninum[institution.uninum] = institution
        if not by_uninum:
            raise InstitutionError("a registry needs at least one institution.")
        self._by_uninum: dict[int, FCAInstitution] = dict(sorted(by_uninum.items()))

    @classmethod
    def from_rosters(
        cls,
        *,
        rosters: Iterable[tuple[str | date | ReportingPeriod, Any]],
    ) -> FCAInstitutionRegistry:
        """Build a registry from FCA institution rosters, one per quarter.

        Every row across every roster is grouped by UNINUM, and each group
        becomes one `FCAInstitution`. The rosters can arrive in any order.

        Parameters
        ----------
        rosters : Iterable[tuple[str | datetime.date | ReportingPeriod, Any]]
            ``(period, roster)`` pairs. A roster is either a native
            dataframe of any backend narwhals supports, such as the result
            of `call_report.fca.institutions.read_institutions`, or an
            iterable of row mappings. Each row must include ``UNINUM``,
            ``SYSTEM``, ``DIST``, ``ASSOC``, ``SHORTNAME``, ``MAIL_ADDR``,
            ``STREET_ADDR``, ``CITY``, ``STATE``, and ``ZIP``. Other
            columns are ignored.

        Returns
        -------
        FCAInstitutionRegistry
            One `FCAInstitution` per UNINUM found in `rosters`.

        Raises
        ------
        InstitutionError
            If `rosters` holds no rows, names the same quarter twice, or
            lists a UNINUM more than once in one quarter. Also for any row
            `FCAInstitution.from_roster_rows` rejects.

        Examples
        --------
        >>> from call_report.fca import FCAInstitutionRegistry
        >>> row = {
        ...     "UNINUM": 722825,
        ...     "SYSTEM": 7,
        ...     "DIST": 22,
        ...     "ASSOC": 825,
        ...     "SHORTNAME": "Mid-America ACA",
        ...     "MAIL_ADDR": "P.O. Box 34390",
        ...     "STREET_ADDR": "1601 UPS Drive",
        ...     "CITY": "Louisville",
        ...     "STATE": "KY",
        ...     "ZIP": "40223-4390",
        ... }
        >>> renamed = {**row, "SHORTNAME": "Farm Credit Mid-America ACA"}
        >>> registry = FCAInstitutionRegistry.from_rosters(
        ...     rosters=[("2011-12-31", [renamed]), ("2011-09-30", [row])]
        ... )
        >>> [v.value for v in registry[722825].short_name_history]
        ['Mid-America ACA', 'Farm Credit Mid-America ACA']
        """
        groups: dict[int, list[tuple[ReportingPeriod, Mapping[str, Any]]]] = {}
        seen_periods: set[ReportingPeriod] = set()
        for period_value, roster in rosters:
            period = _coerce_period(period_value)
            if period in seen_periods:
                raise InstitutionError(
                    f"more than one roster was supplied for {period.label}."
                )
            seen_periods.add(period)
            seen_uninums: set[int] = set()
            for row in _roster_rows(roster):
                _check_row(period, row)
                uninum = _code(row, "UNINUM", period)
                if uninum in seen_uninums:
                    raise InstitutionError(
                        f"UNINUM {uninum} appears more than once in the "
                        f"{period.label} roster."
                    )
                seen_uninums.add(uninum)
                groups.setdefault(uninum, []).append((period, row))
        if not groups:
            raise InstitutionError("the rosters supplied hold no rows.")
        return cls(
            institutions=(
                FCAInstitution.from_roster_rows(rows=rows) for rows in groups.values()
            )
        )

    @classmethod
    def from_dataframe(cls, *, data: Any) -> FCAInstitutionRegistry:
        """Build a registry from rosters stacked into one frame.

        `data` is one row per charter per quarter, with a ``period``
        column naming each row's quarter. That is the shape
        `FCACallReport.load_institutions` returns, and the shape
        `to_dataframe` returns, so a registry round-trips through its own
        frame. The ``most_recent_*`` columns are derived from the history,
        so they are ignored on the way in.

        Parameters
        ----------
        data : Any
            A native dataframe of any backend narwhals supports, eager or
            lazy, with a ``period`` column and the roster columns
            `from_rosters` requires.

        Returns
        -------
        FCAInstitutionRegistry
            One `FCAInstitution` per UNINUM found in `data`.

        Raises
        ------
        InstitutionError
            If `data` has no ``period`` column, or for any roster
            `from_rosters` rejects.

        Examples
        --------
        >>> from call_report.fca import FCAInstitutionRegistry
        >>> row = {
        ...     "UNINUM": 722825,
        ...     "SYSTEM": 7,
        ...     "DIST": 22,
        ...     "ASSOC": 825,
        ...     "SHORTNAME": "Mid-America ACA",
        ...     "MAIL_ADDR": "P.O. Box 34390",
        ...     "STREET_ADDR": "1601 UPS Drive",
        ...     "CITY": "Louisville",
        ...     "STATE": "KY",
        ...     "ZIP": "40223-4390",
        ... }
        >>> registry = FCAInstitutionRegistry.from_rosters(
        ...     rosters=[("2011-09-30", [row]), ("2011-12-31", [row])]
        ... )
        >>> FCAInstitutionRegistry.from_dataframe(data=registry.to_dataframe()) == (
        ...     registry
        ... )
        True
        """
        by_period: dict[ReportingPeriod, list[dict[str, Any]]] = {}
        for row in _rows(data):
            if "period" not in row:
                raise InstitutionError("the frame supplied has no 'period' column.")
            by_period.setdefault(_coerce_period(row["period"]), []).append(row)
        return cls.from_rosters(rosters=by_period.items())

    @property
    def first_period(self) -> ReportingPeriod:
        """Return the earliest quarter any charter in the registry filed.

        This is the smallest `FCAInstitution.first_period` in the registry.

        Returns
        -------
        ReportingPeriod
            The earliest quarter filed.

        Examples
        --------
        >>> from call_report.fca import FCAInstitutionRegistry
        >>> row = {
        ...     "UNINUM": 722825,
        ...     "SYSTEM": 7,
        ...     "DIST": 22,
        ...     "ASSOC": 825,
        ...     "SHORTNAME": "Mid-America ACA",
        ...     "MAIL_ADDR": "P.O. Box 34390",
        ...     "STREET_ADDR": "1601 UPS Drive",
        ...     "CITY": "Louisville",
        ...     "STATE": "KY",
        ...     "ZIP": "40223-4390",
        ... }
        >>> registry = FCAInstitutionRegistry.from_rosters(
        ...     rosters=[("2011-09-30", [row]), ("2011-12-31", [row])]
        ... )
        >>> registry.first_period.label
        '2011Q3'
        """
        return min(institution.first_period for institution in self.values())

    @property
    def last_period(self) -> ReportingPeriod:
        """Return the latest quarter any charter in the registry filed.

        This is the largest `FCAInstitution.last_period` in the registry.

        Returns
        -------
        ReportingPeriod
            The latest quarter filed.

        Examples
        --------
        >>> from call_report.fca import FCAInstitutionRegistry
        >>> row = {
        ...     "UNINUM": 722825,
        ...     "SYSTEM": 7,
        ...     "DIST": 22,
        ...     "ASSOC": 825,
        ...     "SHORTNAME": "Mid-America ACA",
        ...     "MAIL_ADDR": "P.O. Box 34390",
        ...     "STREET_ADDR": "1601 UPS Drive",
        ...     "CITY": "Louisville",
        ...     "STATE": "KY",
        ...     "ZIP": "40223-4390",
        ... }
        >>> registry = FCAInstitutionRegistry.from_rosters(
        ...     rosters=[("2011-09-30", [row]), ("2011-12-31", [row])]
        ... )
        >>> registry.last_period.label
        '2011Q4'
        """
        return max(institution.last_period for institution in self.values())

    def as_of(
        self, *, period: str | date | ReportingPeriod
    ) -> Mapping[int, FCAInstitutionSnapshot]:
        """Return every charter that filed in one quarter, as it stood then.

        Charters that did not file in `period` are left out.

        Parameters
        ----------
        period : str, datetime.date, or ReportingPeriod
            The quarter-end to take the snapshot at.

        Returns
        -------
        Mapping[int, FCAInstitutionSnapshot]
            A read-only mapping of UNINUM to that charter's snapshot, in
            ascending UNINUM order.

        Raises
        ------
        PeriodNotAvailableError
            If no charter in the registry filed in `period`.

        Examples
        --------
        >>> from call_report.fca import FCAInstitutionRegistry
        >>> row = {
        ...     "UNINUM": 722825,
        ...     "SYSTEM": 7,
        ...     "DIST": 22,
        ...     "ASSOC": 825,
        ...     "SHORTNAME": "Mid-America ACA",
        ...     "MAIL_ADDR": "P.O. Box 34390",
        ...     "STREET_ADDR": "1601 UPS Drive",
        ...     "CITY": "Louisville",
        ...     "STATE": "KY",
        ...     "ZIP": "40223-4390",
        ... }
        >>> renamed = {**row, "SHORTNAME": "Farm Credit Mid-America ACA"}
        >>> registry = FCAInstitutionRegistry.from_rosters(
        ...     rosters=[("2011-09-30", [row]), ("2011-12-31", [renamed])]
        ... )
        >>> registry.as_of(period="2011-09-30")[722825].short_name
        'Mid-America ACA'
        """
        resolved = _coerce_period(period)
        snapshots = {
            uninum: institution.as_of(period=resolved)
            for uninum, institution in self.items()
            if any(resolved in span for span in institution.periods)
        }
        if not snapshots:
            raise PeriodNotAvailableError(
                f"no institution in the registry filed in {resolved.label}. The "
                f"registry spans {self.first_period.label} to "
                f"{self.last_period.label}."
            )
        return MappingProxyType(snapshots)

    @overload
    def to_dataframe(
        self,
        *,
        latest_only: bool = False,
        backend: DataFrameBackend | None = None,
        dataframe_type: None = None,
    ) -> NativeDataFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_dataframe(
        self,
        *,
        latest_only: bool = False,
        backend: DataFrameBackend | None = None,
        dataframe_type: Literal["pandas"],
    ) -> pandas.DataFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_dataframe(
        self,
        *,
        latest_only: bool = False,
        backend: DataFrameBackend | None = None,
        dataframe_type: Literal["pyarrow_table"],
    ) -> pyarrow.Table:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_dataframe(
        self,
        *,
        latest_only: bool = False,
        backend: DataFrameBackend | None = None,
        dataframe_type: Literal["polars_dataframe"],
    ) -> polars.DataFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_dataframe(
        self,
        *,
        latest_only: bool = False,
        backend: DataFrameBackend | None = None,
        dataframe_type: Literal["polars_lazyframe"],
    ) -> polars.LazyFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    def to_dataframe(
        self,
        *,
        latest_only: bool = False,
        backend: DataFrameBackend | None = None,
        dataframe_type: DataFrameType | None = None,
    ) -> NativeDataFrame:
        """Return every charter's history as one dataframe.

        By default this stacks `FCAInstitution.to_dataframe` for every
        charter, giving one row per charter per quarter filed. Join it onto
        `FCACallReport.to_long_format` or `FCACallReport.to_wide_format`
        output on ``UNINUM`` and ``period`` to label each row with the name
        and address in effect that quarter.

        Parameters
        ----------
        latest_only : bool, default False
            Keep only each charter's last filed quarter, giving one row per
            charter. The ``period`` column then holds that last quarter.
        backend : {"pandas", "polars", "pyarrow"}, optional
            The dataframe library used to build the frame. If omitted, uses
            whatever backend is currently configured via
            `call_report.config.get_config`. Most users can leave this at
            its default.
        dataframe_type : {"pandas", "pyarrow_table", "polars_lazyframe", \
"polars_dataframe"}, optional
            The dataframe type to convert the result to as a final step,
            regardless of `backend`. Leave this ``None`` (the default) to
            get back whatever `backend` produced. Set it when the code that
            consumes this result needs a specific type, for example a
            pandas DataFrame while the package is configured to use polars.
            The conversion is zero-copy when the requested type already
            matches.

        Returns
        -------
        NativeDataFrame
            A native dataframe with the columns `FCAInstitution.to_dataframe`
            returns, ordered by ``UNINUM`` and then ``period``.

        Examples
        --------
        >>> from call_report.fca import FCAInstitutionRegistry
        >>> row = {
        ...     "UNINUM": 722825,
        ...     "SYSTEM": 7,
        ...     "DIST": 22,
        ...     "ASSOC": 825,
        ...     "SHORTNAME": "Mid-America ACA",
        ...     "MAIL_ADDR": "P.O. Box 34390",
        ...     "STREET_ADDR": "1601 UPS Drive",
        ...     "CITY": "Louisville",
        ...     "STATE": "KY",
        ...     "ZIP": "40223-4390",
        ... }
        >>> renamed = {**row, "SHORTNAME": "Farm Credit Mid-America ACA"}
        >>> registry = FCAInstitutionRegistry.from_rosters(
        ...     rosters=[("2011-09-30", [row]), ("2011-12-31", [renamed])]
        ... )
        >>> frame = registry.to_dataframe(dataframe_type="polars_dataframe")
        >>> frame.height
        2
        >>> latest = registry.to_dataframe(
        ...     latest_only=True, dataframe_type="polars_dataframe"
        ... )
        >>> latest.select("UNINUM", "period", "SHORTNAME").rows()
        [(722825, datetime.date(2011, 12, 31), 'Farm Credit Mid-America ACA')]
        """
        columns = _empty_columns()
        for institution in self.values():
            institution._append_rows(columns, latest_only=latest_only)
        return _columns_to_native(
            columns, backend=backend, dataframe_type=dataframe_type
        )

    def to_json(self, *, indent: int | None = 2) -> str:
        """Return the registry as a JSON string.

        Each charter is stored in the shape `FCAInstitution.to_json`
        produces, in ascending UNINUM order.

        Parameters
        ----------
        indent : int, optional
            Passed through to `json.dumps`. The default of ``2`` produces
            human-readable, diffable output. Pass ``None`` for the most
            compact representation.

        Returns
        -------
        str
            A JSON object with one key, ``institutions``, holding a list
            of charters.

        Examples
        --------
        >>> from call_report.fca import FCAInstitutionRegistry
        >>> row = {
        ...     "UNINUM": 722825,
        ...     "SYSTEM": 7,
        ...     "DIST": 22,
        ...     "ASSOC": 825,
        ...     "SHORTNAME": "Mid-America ACA",
        ...     "MAIL_ADDR": "P.O. Box 34390",
        ...     "STREET_ADDR": "1601 UPS Drive",
        ...     "CITY": "Louisville",
        ...     "STATE": "KY",
        ...     "ZIP": "40223-4390",
        ... }
        >>> registry = FCAInstitutionRegistry.from_rosters(
        ...     rosters=[("2011-09-30", [row])]
        ... )
        >>> FCAInstitutionRegistry.from_json(text=registry.to_json()) == registry
        True
        """
        payload = {
            "institutions": [
                _institution_to_dict(institution) for institution in self.values()
            ]
        }
        return json.dumps(payload, indent=indent)

    @classmethod
    def from_json(cls, *, text: str) -> FCAInstitutionRegistry:
        """Reconstruct a registry from JSON built by `to_json`.

        The inverse of `to_json`. Every charter is validated the same way
        direct construction validates it.

        Parameters
        ----------
        text : str
            A JSON string in the shape `to_json` produces.

        Returns
        -------
        FCAInstitutionRegistry
            The reconstructed registry.

        Raises
        ------
        InstitutionError
            If `text` is not valid JSON, is not a JSON object, or does not
            otherwise match the shape `to_json` produces.

        Examples
        --------
        >>> from call_report.fca import FCAInstitutionRegistry
        >>> row = {
        ...     "UNINUM": 722825,
        ...     "SYSTEM": 7,
        ...     "DIST": 22,
        ...     "ASSOC": 825,
        ...     "SHORTNAME": "Mid-America ACA",
        ...     "MAIL_ADDR": "P.O. Box 34390",
        ...     "STREET_ADDR": "1601 UPS Drive",
        ...     "CITY": "Louisville",
        ...     "STATE": "KY",
        ...     "ZIP": "40223-4390",
        ... }
        >>> text = FCAInstitutionRegistry.from_rosters(
        ...     rosters=[("2011-09-30", [row])]
        ... ).to_json()
        >>> list(FCAInstitutionRegistry.from_json(text=text))
        [722825]
        """
        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            message = f"{text[:80]!r} is not valid JSON: {error}"
            raise InstitutionError(message) from error
        if not isinstance(data, dict):
            message = f"expected a JSON object, got {type(data).__name__}."
            raise InstitutionError(message)
        institutions = data.get("institutions")
        if not isinstance(institutions, list):
            raise InstitutionError(
                "malformed registry JSON: expected an 'institutions' list."
            )
        return cls(institutions=[_institution_from_dict(item) for item in institutions])

    def __getitem__(self, uninum: int) -> FCAInstitution:
        """Return the charter with the given UNINUM.

        Implements ``registry[uninum]`` lookup.

        Parameters
        ----------
        uninum : int
            The UNINUM to look up.

        Returns
        -------
        FCAInstitution
            That charter's history.

        Raises
        ------
        KeyError
            If no charter in the registry has `uninum`.
        """
        try:
            return self._by_uninum[uninum]
        except KeyError:
            raise KeyError(
                f"UNINUM {uninum!r} is not in the registry, which spans "
                f"{self.first_period.label} to {self.last_period.label}."
            ) from None

    def __iter__(self) -> Iterator[int]:
        """Iterate over UNINUMs in ascending order.

        The order does not depend on the order charters were supplied in.

        Returns
        -------
        Iterator[int]
            The registry's UNINUMs.
        """
        return iter(self._by_uninum)

    def __len__(self) -> int:
        """Return the number of charters in the registry.

        Implements ``len(registry)``.

        Returns
        -------
        int
            The number of distinct UNINUMs.
        """
        return len(self._by_uninum)

    def __repr__(self) -> str:
        """Return a compact summary of the registry's size and span.

        The charters themselves are left out, since a registry can hold
        hundreds.

        Returns
        -------
        str
            The number of charters and the first and last quarters filed.
        """
        return (
            f"FCAInstitutionRegistry(institutions={len(self)}, "
            f"first_period={self.first_period.label}, "
            f"last_period={self.last_period.label})"
        )


def _roster_rows(roster: Any) -> Iterable[Mapping[str, Any]]:
    """Return one roster's rows, whether it is a dataframe or rows already.

    A dataframe is read through narwhals, so any backend is accepted.

    Parameters
    ----------
    roster : Any
        A native dataframe of any backend narwhals supports, or an
        iterable of row mappings.

    Returns
    -------
    Iterable[Mapping[str, Any]]
        The roster's rows.
    """
    wrapped = nw.from_native(roster, pass_through=True)
    if isinstance(wrapped, (nw.DataFrame, nw.LazyFrame)):
        return _rows(roster)
    rows: Iterable[Mapping[str, Any]] = roster
    return rows
