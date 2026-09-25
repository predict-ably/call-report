"""The cross-period history of one FCA charter, keyed by its UNINUM.

FCA publishes one institution roster (``INST``) per quarter. Each row names
one charter by its UNINUM, the concatenation of its system, district, and
association codes, alongside its short name and address. A charter's name
and address change over time, and a charter can be absent from some
quarters and return in a later one.

:class:`FCAInstitution` gathers every quarter one UNINUM filed into a single
object. Each name and address attribute is held as a tuple of
:class:`InstitutionAttributeVersion`, one per span of quarters over which
the value held. A new version starts when the value changes, or when the
charter returns after a gap. :class:`FCAInstitutionSnapshot` is the same
charter as it stood in one quarter.

A UNINUM identifies a charter, not a continuing business. When a charter is
renumbered, the new UNINUM is a separate :class:`FCAInstitution`.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from typing import TYPE_CHECKING, Any, Literal, overload

import narwhals as nw

from call_report.config import DataFrameBackend
from call_report.core._backend import (
    DataFrameType,
    build_frame,
    convert_dataframe_type,
    date_dtype,
    finalize,
)
from call_report.core._periods import PeriodRange, ReportingPeriod
from call_report.core._schema import _backend_context, _coerce_period
from call_report.exceptions import (
    InstitutionError,
    InvalidPeriodError,
    PeriodNotAvailableError,
)

if TYPE_CHECKING:
    import pandas
    import polars
    import pyarrow

    from call_report.core._backend import NativeDataFrame

_CODE_COLUMNS = ("SYSTEM", "DIST", "ASSOC")

# Each roster column that is versioned over time, mapped to the stem of the
# attribute names FCAInstitution and FCAInstitutionSnapshot use for it.
_VERSIONED_COLUMNS: dict[str, str] = {
    "SHORTNAME": "short_name",
    "MAIL_ADDR": "mail_addr",
    "STREET_ADDR": "street_addr",
    "CITY": "city",
    "STATE": "state",
    "ZIP": "zip",
}

_REQUIRED_COLUMNS = ("UNINUM", *_CODE_COLUMNS, *_VERSIONED_COLUMNS)

_MOST_RECENT_COLUMNS = tuple(
    f"most_recent_{stem}" for stem in _VERSIONED_COLUMNS.values()
)

_FRAME_COLUMNS = (
    "UNINUM",
    "period",
    *_CODE_COLUMNS,
    *_VERSIONED_COLUMNS,
    *_MOST_RECENT_COLUMNS,
)


@dataclass(frozen=True, kw_only=True)
class InstitutionAttributeVersion:
    """One span of quarters over which a charter attribute held one value.

    `FCAInstitution` holds each name and address attribute as a tuple of
    these, oldest first.

    Attributes
    ----------
    value : str or None
        The attribute's value over `periods`, exactly as FCA published it.
        ``None`` means the roster left the attribute blank.
    periods : PeriodRange
        The contiguous span of quarters this version covers.

    Examples
    --------
    >>> from call_report.core import PeriodRange
    >>> from call_report.fca import InstitutionAttributeVersion
    >>> version = InstitutionAttributeVersion(
    ...     value="Mid-America ACA",
    ...     periods=PeriodRange(start="2000-03-31", end="2011-09-30"),
    ... )
    >>> version.periods[-1].label
    '2011Q3'
    """  # numpydoc ignore=PR01

    value: str | None
    periods: PeriodRange


@dataclass(frozen=True, kw_only=True)
class FCAInstitutionSnapshot:
    """One FCA charter as it stood in a single quarter.

    Returned by `FCAInstitution.as_of`.

    Attributes
    ----------
    uninum : int
        The charter's UNINUM.
    period : ReportingPeriod
        The quarter this snapshot describes.
    system : int
        The charter's system code (``SYSTEM``).
    district : int
        The charter's district code (``DIST``).
    association : int
        The charter's association code (``ASSOC``).
    short_name : str or None
        The short name (``SHORTNAME``) in effect in `period`.
    mail_addr : str or None
        The mailing address (``MAIL_ADDR``) in effect in `period`.
    street_addr : str or None
        The street address (``STREET_ADDR``) in effect in `period`.
    city : str or None
        The city (``CITY``) in effect in `period`.
    state : str or None
        The state (``STATE``) in effect in `period`.
    zip : str or None
        The ZIP code (``ZIP``) in effect in `period`.

    Examples
    --------
    >>> from call_report.fca import FCAInstitution
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
    >>> institution = FCAInstitution.from_roster_rows(
    ...     rows=[("2011-09-30", row), ("2011-12-31", renamed)]
    ... )
    >>> snapshot = institution.as_of(period="2011-09-30")
    >>> snapshot.short_name, snapshot.city
    ('Mid-America ACA', 'Louisville')
    """  # numpydoc ignore=PR01

    uninum: int
    period: ReportingPeriod
    system: int
    district: int
    association: int
    short_name: str | None
    mail_addr: str | None
    street_addr: str | None
    city: str | None
    state: str | None
    zip: str | None


@dataclass(frozen=True, kw_only=True, repr=False)
class FCAInstitution:
    """The history of one FCA charter across every quarter it filed.

    One instance covers one UNINUM. The system, district, and association
    codes are fixed for a UNINUM, because the UNINUM is built from them.
    The short name and the address attributes are each a tuple of
    `InstitutionAttributeVersion`, oldest first. A new version starts when
    the value changes, or when the charter returns after a gap. A value can
    recur later in the history, for example an address that changes and
    then changes back.

    Build one from roster rows with `from_roster_rows`. Constructing one
    directly validates that every history covers exactly the quarters in
    `periods`.

    Attributes
    ----------
    uninum : int
        The charter's UNINUM.
    system : int
        The charter's system code (``SYSTEM``).
    district : int
        The charter's district code (``DIST``).
    association : int
        The charter's association code (``ASSOC``).
    periods : tuple[PeriodRange, ...]
        One or more chronologically ordered, non-overlapping, non-adjacent
        spans of the quarters this charter filed. More than one span means
        the charter was absent for a stretch and later returned.
    short_name_history : tuple[InstitutionAttributeVersion, ...]
        The versions of the short name (``SHORTNAME``).
    mail_addr_history : tuple[InstitutionAttributeVersion, ...]
        The versions of the mailing address (``MAIL_ADDR``).
    street_addr_history : tuple[InstitutionAttributeVersion, ...]
        The versions of the street address (``STREET_ADDR``).
    city_history : tuple[InstitutionAttributeVersion, ...]
        The versions of the city (``CITY``).
    state_history : tuple[InstitutionAttributeVersion, ...]
        The versions of the state (``STATE``).
    zip_history : tuple[InstitutionAttributeVersion, ...]
        The versions of the ZIP code (``ZIP``).

    Raises
    ------
    InstitutionError
        If `periods` is empty or its spans are out of order, overlapping,
        or adjacent. Also if any history does not cover exactly the
        quarters in `periods`, in order, or has two adjacent versions with
        the same value.

    See Also
    --------
    call_report.fca.institutions.read_institutions : Read one quarter's roster.

    Examples
    --------
    >>> from call_report.fca import FCAInstitution
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
    >>> institution = FCAInstitution.from_roster_rows(
    ...     rows=[("2011-09-30", row), ("2011-12-31", renamed)]
    ... )
    >>> [version.value for version in institution.short_name_history]
    ['Mid-America ACA', 'Farm Credit Mid-America ACA']
    """  # numpydoc ignore=PR01

    uninum: int
    system: int
    district: int
    association: int
    periods: tuple[PeriodRange, ...]
    short_name_history: tuple[InstitutionAttributeVersion, ...]
    mail_addr_history: tuple[InstitutionAttributeVersion, ...]
    street_addr_history: tuple[InstitutionAttributeVersion, ...]
    city_history: tuple[InstitutionAttributeVersion, ...]
    state_history: tuple[InstitutionAttributeVersion, ...]
    zip_history: tuple[InstitutionAttributeVersion, ...]

    def __post_init__(self) -> None:
        """Validate the presence spans and every attribute history.

        Runs automatically after construction, since this dataclass is
        frozen and cannot be validated any other way.

        Raises
        ------
        InstitutionError
            If the spans or any history break the rules `FCAInstitution`
            documents.
        """
        label = f"UNINUM {self.uninum}"
        _validate_presence(self.periods, label)
        quarters = [quarter for span in self.periods for quarter in span]
        for column in _VERSIONED_COLUMNS:
            _validate_history(
                self._history(column), quarters, f"{label} {column} history"
            )

    def _history(self, column: str) -> tuple[InstitutionAttributeVersion, ...]:
        """Return the history for one versioned roster column.

        Parameters
        ----------
        column : str
            A roster column name, e.g. ``"SHORTNAME"``.

        Returns
        -------
        tuple[InstitutionAttributeVersion, ...]
            That column's versions.
        """
        history: tuple[InstitutionAttributeVersion, ...] = getattr(
            self, f"{_VERSIONED_COLUMNS[column]}_history"
        )
        return history

    @classmethod
    def from_roster_rows(
        cls,
        *,
        rows: Iterable[tuple[str | date | ReportingPeriod, Mapping[str, Any]]],
    ) -> FCAInstitution:
        """Build a charter's history from its roster rows, one per quarter.

        Each item pairs a quarter with that quarter's roster row for this
        charter. The rows can arrive in any order. Values are kept exactly
        as FCA published them, except that a missing value (``None`` or a
        float NaN, as pandas spells it) becomes ``None``.

        A row carrying ``YEAR`` and ``MONTH`` values, as FCA's own roster
        does, must name the quarter it is paired with.

        Parameters
        ----------
        rows : Iterable[tuple[str | datetime.date | ReportingPeriod, Mapping[str, Any]]]
            ``(period, row)`` pairs. Each row maps roster column names to
            values and must include ``UNINUM``, ``SYSTEM``, ``DIST``,
            ``ASSOC``, ``SHORTNAME``, ``MAIL_ADDR``, ``STREET_ADDR``,
            ``CITY``, ``STATE``, and ``ZIP``. Other columns are ignored.

        Returns
        -------
        FCAInstitution
            The charter's history across every quarter in `rows`.

        Raises
        ------
        InstitutionError
            If `rows` is empty, names the same quarter twice, has a row
            missing a required column or a required code, or mixes more
            than one UNINUM or code set. Also if a row's ``YEAR`` and
            ``MONTH`` do not match its quarter.

        Examples
        --------
        >>> from call_report.fca import FCAInstitution
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
        >>> institution = FCAInstitution.from_roster_rows(
        ...     rows=[("2011-12-31", renamed), ("2011-09-30", row)]
        ... )
        >>> institution.first_period.label, institution.last_period.label
        ('2011Q3', '2011Q4')
        """
        ordered = sorted(
            ((_coerce_period(period), row) for period, row in rows),
            key=lambda pair: pair[0],
        )
        if not ordered:
            raise InstitutionError("an institution needs at least one roster row.")
        for (previous, _), (period, _) in pairwise(ordered):
            if period == previous:
                raise InstitutionError(
                    f"more than one roster row was supplied for {period.label}."
                )
        for period, row in ordered:
            _check_row(period, row)

        first_period, first_row = ordered[0]
        uninum = _code(first_row, "UNINUM", first_period)
        codes = tuple(
            _code(first_row, column, first_period) for column in _CODE_COLUMNS
        )
        for period, row in ordered[1:]:
            seen = _code(row, "UNINUM", period)
            if seen != uninum:
                raise InstitutionError(
                    f"roster rows mix UNINUM {uninum} and UNINUM {seen} "
                    f"(at {period.label}). Build one institution per UNINUM."
                )
            seen_codes = tuple(_code(row, column, period) for column in _CODE_COLUMNS)
            if seen_codes != codes:
                raise InstitutionError(
                    f"UNINUM {uninum} has codes {codes} at {first_period.label} "
                    f"but {seen_codes} at {period.label}."
                )

        quarters = [period for period, _ in ordered]
        histories = {
            f"{stem}_history": _build_history(
                [(period, _text(row[column])) for period, row in ordered]
            )
            for column, stem in _VERSIONED_COLUMNS.items()
        }
        return cls(
            uninum=uninum,
            system=codes[0],
            district=codes[1],
            association=codes[2],
            periods=_spans(quarters),
            **histories,
        )

    @property
    def first_period(self) -> ReportingPeriod:
        """Return the earliest quarter this charter filed.

        This is the start of the earliest of this charter's `periods` spans.

        Returns
        -------
        ReportingPeriod
            The first quarter of the earliest of `periods`.

        Examples
        --------
        >>> from call_report.fca import FCAInstitution
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
        >>> institution = FCAInstitution.from_roster_rows(rows=[("2011-09-30", row)])
        >>> institution.first_period.label
        '2011Q3'
        """
        return self.periods[0][0]

    @property
    def last_period(self) -> ReportingPeriod:
        """Return the latest quarter this charter filed.

        This is the end of the latest of this charter's `periods` spans.

        Returns
        -------
        ReportingPeriod
            The last quarter of the latest of `periods`.

        Examples
        --------
        >>> from call_report.fca import FCAInstitution
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
        >>> institution = FCAInstitution.from_roster_rows(
        ...     rows=[("2011-09-30", row), ("2012-03-31", row)]
        ... )
        >>> institution.last_period.label
        '2012Q1'
        """
        return self.periods[-1][-1]

    @property
    def most_recent_short_name(self) -> str | None:
        """Return the short name from the last quarter this charter filed.

        This is the value a time series of this charter can carry as one
        stable label, whatever the short name was in earlier quarters.

        Returns
        -------
        str or None
            The value of the latest `short_name_history` version.

        Examples
        --------
        >>> from call_report.fca import FCAInstitution
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
        >>> institution = FCAInstitution.from_roster_rows(
        ...     rows=[("2011-09-30", row), ("2011-12-31", renamed)]
        ... )
        >>> institution.most_recent_short_name
        'Farm Credit Mid-America ACA'
        """
        return self.short_name_history[-1].value

    @property
    def most_recent_mail_addr(self) -> str | None:
        """Return the mailing address from the last quarter this charter filed.

        This is the value a time series of this charter can carry as one
        stable label, whatever the mailing address was in earlier quarters.

        Returns
        -------
        str or None
            The value of the latest `mail_addr_history` version.

        Examples
        --------
        >>> from call_report.fca import FCAInstitution
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
        >>> institution = FCAInstitution.from_roster_rows(rows=[("2011-09-30", row)])
        >>> institution.most_recent_mail_addr
        'P.O. Box 34390'
        """
        return self.mail_addr_history[-1].value

    @property
    def most_recent_street_addr(self) -> str | None:
        """Return the street address from the last quarter this charter filed.

        This is the value a time series of this charter can carry as one
        stable label, whatever the street address was in earlier quarters.

        Returns
        -------
        str or None
            The value of the latest `street_addr_history` version.

        Examples
        --------
        >>> from call_report.fca import FCAInstitution
        >>> row = {
        ...     "UNINUM": 722825,
        ...     "SYSTEM": 7,
        ...     "DIST": 22,
        ...     "ASSOC": 825,
        ...     "SHORTNAME": "Farm Credit Mid-America ACA",
        ...     "MAIL_ADDR": "P.O. Box 34390",
        ...     "STREET_ADDR": "1601 UPS Drive",
        ...     "CITY": "Louisville",
        ...     "STATE": "KY",
        ...     "ZIP": "40223-4390",
        ... }
        >>> moved = {**row, "STREET_ADDR": "12501 Lakefront Place", "ZIP": "40299-4894"}
        >>> institution = FCAInstitution.from_roster_rows(
        ...     rows=[("2022-12-31", row), ("2023-03-31", moved)]
        ... )
        >>> institution.most_recent_street_addr
        '12501 Lakefront Place'
        """
        return self.street_addr_history[-1].value

    @property
    def most_recent_city(self) -> str | None:
        """Return the city from the last quarter this charter filed.

        This is the value a time series of this charter can carry as one
        stable label, whatever the city was in earlier quarters.

        Returns
        -------
        str or None
            The value of the latest `city_history` version.

        Examples
        --------
        >>> from call_report.fca import FCAInstitution
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
        >>> institution = FCAInstitution.from_roster_rows(rows=[("2011-09-30", row)])
        >>> institution.most_recent_city
        'Louisville'
        """
        return self.city_history[-1].value

    @property
    def most_recent_state(self) -> str | None:
        """Return the state from the last quarter this charter filed.

        This is the value a time series of this charter can carry as one
        stable label, whatever the state was in earlier quarters.

        Returns
        -------
        str or None
            The value of the latest `state_history` version.

        Examples
        --------
        >>> from call_report.fca import FCAInstitution
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
        >>> institution = FCAInstitution.from_roster_rows(rows=[("2011-09-30", row)])
        >>> institution.most_recent_state
        'KY'
        """
        return self.state_history[-1].value

    @property
    def most_recent_zip(self) -> str | None:
        """Return the ZIP code from the last quarter this charter filed.

        This is the value a time series of this charter can carry as one
        stable label, whatever the ZIP code was in earlier quarters.

        Returns
        -------
        str or None
            The value of the latest `zip_history` version.

        Examples
        --------
        >>> from call_report.fca import FCAInstitution
        >>> row = {
        ...     "UNINUM": 722825,
        ...     "SYSTEM": 7,
        ...     "DIST": 22,
        ...     "ASSOC": 825,
        ...     "SHORTNAME": "Farm Credit Mid-America ACA",
        ...     "MAIL_ADDR": "P.O. Box 34390",
        ...     "STREET_ADDR": "1601 UPS Drive",
        ...     "CITY": "Louisville",
        ...     "STATE": "KY",
        ...     "ZIP": "40223-4390",
        ... }
        >>> moved = {**row, "STREET_ADDR": "12501 Lakefront Place", "ZIP": "40299-4894"}
        >>> institution = FCAInstitution.from_roster_rows(
        ...     rows=[("2022-12-31", row), ("2023-03-31", moved)]
        ... )
        >>> institution.most_recent_zip
        '40299-4894'
        """
        return self.zip_history[-1].value

    def as_of(self, *, period: str | date | ReportingPeriod) -> FCAInstitutionSnapshot:
        """Return this charter as it stood in one quarter.

        Each name and address attribute takes the value of the version
        covering `period`.

        Parameters
        ----------
        period : str, datetime.date, or ReportingPeriod
            The quarter-end to take the snapshot at.

        Returns
        -------
        FCAInstitutionSnapshot
            The codes, and the name and address values in effect in
            `period`.

        Raises
        ------
        PeriodNotAvailableError
            If this charter did not file in `period`.

        Examples
        --------
        >>> from call_report.fca import FCAInstitution
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
        >>> institution = FCAInstitution.from_roster_rows(
        ...     rows=[("2011-09-30", row), ("2011-12-31", renamed)]
        ... )
        >>> institution.as_of(period="2011-12-31").short_name
        'Farm Credit Mid-America ACA'
        """
        resolved = _coerce_period(period)
        if not any(resolved in span for span in self.periods):
            raise PeriodNotAvailableError(
                f"UNINUM {self.uninum} did not file in {resolved.label}. It "
                f"filed from {self.first_period.label} to {self.last_period.label}"
                f" in {len(self.periods)} span(s)."
            )
        values = {
            stem: next(
                version.value
                for version in self._history(column)
                if resolved in version.periods
            )
            for column, stem in _VERSIONED_COLUMNS.items()
        }
        return FCAInstitutionSnapshot(
            uninum=self.uninum,
            period=resolved,
            system=self.system,
            district=self.district,
            association=self.association,
            **values,
        )

    @overload
    def to_dataframe(
        self,
        *,
        backend: DataFrameBackend | None = None,
        dataframe_type: None = None,
    ) -> NativeDataFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_dataframe(
        self,
        *,
        backend: DataFrameBackend | None = None,
        dataframe_type: Literal["pandas"],
    ) -> pandas.DataFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_dataframe(
        self,
        *,
        backend: DataFrameBackend | None = None,
        dataframe_type: Literal["pyarrow_table"],
    ) -> pyarrow.Table:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_dataframe(
        self,
        *,
        backend: DataFrameBackend | None = None,
        dataframe_type: Literal["polars_dataframe"],
    ) -> polars.DataFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_dataframe(
        self,
        *,
        backend: DataFrameBackend | None = None,
        dataframe_type: Literal["polars_lazyframe"],
    ) -> polars.LazyFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    def to_dataframe(
        self,
        *,
        backend: DataFrameBackend | None = None,
        dataframe_type: DataFrameType | None = None,
    ) -> NativeDataFrame:
        """Return this charter's history as a dataframe, one row per quarter filed.

        Each row holds the codes and the name and address values in effect
        in that quarter, under FCA's own roster column names. The
        ``most_recent_*`` columns repeat the value from the last quarter
        this charter filed on every row, so a time series can carry one
        stable label. ``UNINUM`` and ``period`` match the key columns of
        `FCACallReport.to_long_format` and `FCACallReport.to_wide_format`,
        so the result joins onto call report data directly.

        Parameters
        ----------
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
            A native dataframe with columns ``UNINUM``, ``period``,
            ``SYSTEM``, ``DIST``, ``ASSOC``, ``SHORTNAME``, ``MAIL_ADDR``,
            ``STREET_ADDR``, ``CITY``, ``STATE``, ``ZIP``,
            ``most_recent_short_name``, ``most_recent_mail_addr``,
            ``most_recent_street_addr``, ``most_recent_city``,
            ``most_recent_state``, and ``most_recent_zip``, ordered by
            ``period``.

        See Also
        --------
        call_report.fca.FCACallReport.to_long_format : Call report data keyed
            by the same ``UNINUM`` and ``period`` columns.

        Examples
        --------
        >>> from call_report.fca import FCAInstitution
        >>> row = {
        ...     "UNINUM": 722825, "SYSTEM": 7, "DIST": 22, "ASSOC": 825,
        ...     "SHORTNAME": "Mid-America ACA", "MAIL_ADDR": "P.O. Box 34390",
        ...     "STREET_ADDR": "1601 UPS Drive", "CITY": "Louisville",
        ...     "STATE": "KY", "ZIP": "40223-4390",
        ... }
        >>> renamed = {**row, "SHORTNAME": "Farm Credit Mid-America ACA"}
        >>> institution = FCAInstitution.from_roster_rows(
        ...     rows=[("2011-09-30", row), ("2011-12-31", renamed)]
        ... )
        >>> frame = institution.to_dataframe(dataframe_type="polars_dataframe")
        >>> frame.select("period", "SHORTNAME", "most_recent_short_name").rows()
        ... # doctest: +NORMALIZE_WHITESPACE
        [(datetime.date(2011, 9, 30), 'Mid-America ACA',
          'Farm Credit Mid-America ACA'),
         (datetime.date(2011, 12, 31), 'Farm Credit Mid-America ACA',
          'Farm Credit Mid-America ACA')]
        """
        columns: dict[str, list[Any]] = {column: [] for column in _FRAME_COLUMNS}
        most_recent = {
            name: self._history(column)[-1].value
            for name, column in zip(
                _MOST_RECENT_COLUMNS, _VERSIONED_COLUMNS, strict=True
            )
        }
        codes = dict(zip(_CODE_COLUMNS, self._codes(), strict=True))
        for span in self.periods:
            for quarter in span:
                columns["UNINUM"].append(self.uninum)
                columns["period"].append(quarter.period_end)
                for column, code in codes.items():
                    columns[column].append(code)
                for name, value in most_recent.items():
                    columns[name].append(value)
        for column in _VERSIONED_COLUMNS:
            for version in self._history(column):
                columns[column].extend(version.value for _ in version.periods)
        with _backend_context(backend):
            native = finalize(frame=build_frame(data=columns, schema=_frame_schema()))
        return convert_dataframe_type(data=native, dataframe_type=dataframe_type)

    def _codes(self) -> tuple[int, int, int]:
        """Return the system, district, and association codes, in that order.

        Returns
        -------
        tuple[int, int, int]
            The codes, ordered as FCA's ``SYSTEM``, ``DIST``, ``ASSOC``.
        """
        return (self.system, self.district, self.association)

    def to_json(self, *, indent: int | None = 2) -> str:
        """Return this charter's history as a JSON string.

        Each attribute is stored as its list of versions rather than one
        entry per quarter, which keeps the output compact and diffable.

        Parameters
        ----------
        indent : int, optional
            Passed through to `json.dumps`. The default of ``2`` produces
            human-readable, diffable output. Pass ``None`` for the most
            compact representation.

        Returns
        -------
        str
            A JSON object with ``uninum``, ``system``, ``district``,
            ``association``, ``periods``, and ``attributes`` keys.
            ``attributes`` maps each roster column name to its list of
            versions, each with ``value``, ``start``, and ``end``.

        Examples
        --------
        >>> from call_report.fca import FCAInstitution
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
        >>> institution = FCAInstitution.from_roster_rows(rows=[("2011-09-30", row)])
        >>> FCAInstitution.from_json(text=institution.to_json()) == institution
        True
        """
        return json.dumps(_institution_to_dict(self), indent=indent)

    @classmethod
    def from_json(cls, *, text: str) -> FCAInstitution:
        """Reconstruct a charter's history from JSON built by `to_json`.

        The inverse of `to_json`. Round-tripping a charter through both
        reconstructs an equal `FCAInstitution`, and the result is validated
        the same way direct construction is.

        Parameters
        ----------
        text : str
            A JSON string in the shape `to_json` produces.

        Returns
        -------
        FCAInstitution
            The reconstructed charter.

        Raises
        ------
        InstitutionError
            If `text` is not valid JSON, is not a JSON object, or does not
            otherwise match the shape `to_json` produces.

        Examples
        --------
        >>> from call_report.fca import FCAInstitution
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
        >>> text = FCAInstitution.from_roster_rows(rows=[("2011-09-30", row)]).to_json()
        >>> FCAInstitution.from_json(text=text).most_recent_short_name
        'Mid-America ACA'
        """
        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            raise InstitutionError(
                f"{text[:80]!r} is not valid JSON: {error}"
            ) from error
        return _institution_from_dict(data)

    def __repr__(self) -> str:
        """Return a compact summary naming the charter and its span.

        Returns
        -------
        str
            The UNINUM, most recent short name, and first and last
            quarters filed.
        """
        return (
            f"FCAInstitution(uninum={self.uninum}, "
            f"most_recent_short_name={self.most_recent_short_name!r}, "
            f"first_period={self.first_period.label}, "
            f"last_period={self.last_period.label})"
        )


def _frame_schema() -> dict[str, nw.dtypes.DType]:
    """Return the dtype of every `FCAInstitution.to_dataframe` column.

    A function rather than a constant because the ``period`` dtype depends
    on the configured backend (see `call_report.core._backend.date_dtype`).

    Returns
    -------
    dict[str, narwhals.dtypes.DType]
        Column name to dtype, for every column in `_FRAME_COLUMNS`.
    """
    schema: dict[str, nw.dtypes.DType] = {"UNINUM": nw.Int64(), "period": date_dtype()}
    schema.update({column: nw.Int64() for column in _CODE_COLUMNS})
    schema.update({column: nw.String() for column in _VERSIONED_COLUMNS})
    schema.update({column: nw.String() for column in _MOST_RECENT_COLUMNS})
    return schema


def _validate_presence(periods: tuple[PeriodRange, ...], label: str) -> None:
    """Validate that presence spans are non-empty, ordered, and non-touching.

    Parameters
    ----------
    periods : tuple[PeriodRange, ...]
        The spans to validate, in the order they were supplied.
    label : str
        Names the charter in the error message.

    Raises
    ------
    InstitutionError
        If `periods` is empty, or any two spans are out of order,
        overlapping, or adjacent (adjacent spans should be one span).
    """
    if not periods:
        raise InstitutionError(f"{label} must have at least one period span.")
    for previous, span in pairwise(periods):
        if span[0] <= previous[-1].next():
            raise InstitutionError(
                f"{label} period spans must be chronologically ordered, "
                f"non-overlapping, and non-adjacent. The span starting "
                f"{span[0].label} is not far enough after the span ending "
                f"{previous[-1].label}."
            )


def _validate_history(
    history: tuple[InstitutionAttributeVersion, ...],
    quarters: list[ReportingPeriod],
    label: str,
) -> None:
    """Validate that a history covers exactly the charter's quarters, in order.

    Comparing the history's quarters, flattened in order, against the
    charter's own quarters catches an empty history, versions out of
    order, overlapping versions, and a version that spans a gap, all at
    once.

    Parameters
    ----------
    history : tuple[InstitutionAttributeVersion, ...]
        The versions to validate.
    quarters : list[ReportingPeriod]
        Every quarter the charter filed, in order.
    label : str
        Names the charter and attribute in the error message.

    Raises
    ------
    InstitutionError
        If the history does not cover exactly `quarters`, in order, or two
        adjacent versions hold the same value.
    """
    covered = [quarter for version in history for quarter in version.periods]
    if covered != quarters:
        raise InstitutionError(
            f"{label} must cover exactly the quarters the institution filed, "
            f"in order, with one version per quarter."
        )
    for previous, version in pairwise(history):
        adjacent = version.periods[0] == previous.periods[-1].next()
        if adjacent and version.value == previous.value:
            raise InstitutionError(
                f"{label} has adjacent versions with the same value "
                f"{version.value!r} spanning {previous.periods[0].label} to "
                f"{version.periods[-1].label}. These should be one version."
            )


def _spans(quarters: list[ReportingPeriod]) -> tuple[PeriodRange, ...]:
    """Merge ordered, distinct quarters into contiguous spans.

    Parameters
    ----------
    quarters : list[ReportingPeriod]
        Distinct quarters in chronological order. Must not be empty.

    Returns
    -------
    tuple[PeriodRange, ...]
        One span per run of consecutive quarters.
    """
    spans: list[PeriodRange] = []
    start = end = quarters[0]
    for quarter in quarters[1:]:
        if quarter != end.next():
            spans.append(PeriodRange(start=start, end=end))
            start = quarter
        end = quarter
    spans.append(PeriodRange(start=start, end=end))
    return tuple(spans)


def _build_history(
    observations: list[tuple[ReportingPeriod, str | None]],
) -> tuple[InstitutionAttributeVersion, ...]:
    """Collapse one attribute's per-quarter values into versions.

    A new version starts when the value changes, or when the quarter does
    not directly follow the previous one.

    Parameters
    ----------
    observations : list[tuple[ReportingPeriod, str | None]]
        ``(quarter, value)`` pairs, in chronological order, with distinct
        quarters. Must not be empty.

    Returns
    -------
    tuple[InstitutionAttributeVersion, ...]
        The attribute's versions, oldest first.
    """
    versions: list[InstitutionAttributeVersion] = []
    start, value = observations[0]
    end = start
    for quarter, observed in observations[1:]:
        if quarter != end.next() or observed != value:
            versions.append(
                InstitutionAttributeVersion(
                    value=value, periods=PeriodRange(start=start, end=end)
                )
            )
            start, value = quarter, observed
        end = quarter
    versions.append(
        InstitutionAttributeVersion(
            value=value, periods=PeriodRange(start=start, end=end)
        )
    )
    return tuple(versions)


def _check_row(period: ReportingPeriod, row: Mapping[str, Any]) -> None:
    """Validate one roster row's columns and its ``YEAR``/``MONTH`` values.

    Parameters
    ----------
    period : ReportingPeriod
        The quarter `row` was supplied for.
    row : Mapping[str, Any]
        One roster row.

    Raises
    ------
    InstitutionError
        If `row` is missing a required column, or carries ``YEAR`` and
        ``MONTH`` values that name a different quarter.
    """
    missing = [column for column in _REQUIRED_COLUMNS if column not in row]
    if missing:
        raise InstitutionError(
            f"the roster row for {period.label} is missing column(s) "
            f"{', '.join(missing)}."
        )
    year: Any = row.get("YEAR")
    month: Any = row.get("MONTH")
    if _is_missing(year) or _is_missing(month):
        return
    if (int(year), int(month)) != (period.period_end.year, period.period_end.month):
        raise InstitutionError(
            f"the roster row supplied for {period.label} reports YEAR={year} "
            f"and MONTH={month}."
        )


def _code(row: Mapping[str, Any], column: str, period: ReportingPeriod) -> int:
    """Return one of a row's integer codes, rejecting a missing value.

    Parameters
    ----------
    row : Mapping[str, Any]
        One roster row.
    column : str
        ``"UNINUM"``, ``"SYSTEM"``, ``"DIST"``, or ``"ASSOC"``.
    period : ReportingPeriod
        The quarter `row` was supplied for, named in the error message.

    Returns
    -------
    int
        The code as a plain ``int``, whatever numeric type the backend
        produced.

    Raises
    ------
    InstitutionError
        If the value is missing.
    """
    value = row[column]
    if _is_missing(value):
        raise InstitutionError(f"the roster row for {period.label} has no {column}.")
    return int(value)


def _text(value: Any) -> str | None:
    """Return a roster text value as a ``str``, or ``None`` when missing.

    Parameters
    ----------
    value : Any
        One cell of a versioned roster column.

    Returns
    -------
    str or None
        ``None`` for a missing value, otherwise `value` as a string.
    """
    return None if _is_missing(value) else str(value)


def _is_missing(value: Any) -> bool:
    """Return whether a cell is missing, however the backend spells it.

    pandas spells a missing value as a float NaN. polars and pyarrow use
    ``None``.

    Parameters
    ----------
    value : Any
        One cell.

    Returns
    -------
    bool
        ``True`` for ``None`` or a float NaN.
    """
    return value is None or (isinstance(value, float) and math.isnan(value))


def _institution_to_dict(institution: FCAInstitution) -> dict[str, Any]:
    """Return a charter's history as a JSON-ready dict.

    Parameters
    ----------
    institution : FCAInstitution
        The charter to serialize.

    Returns
    -------
    dict[str, Any]
        The payload `FCAInstitution.to_json` writes.
    """
    return {
        "uninum": institution.uninum,
        "system": institution.system,
        "district": institution.district,
        "association": institution.association,
        "periods": [_span_to_dict(span) for span in institution.periods],
        "attributes": {
            column: [
                {"value": version.value, **_span_to_dict(version.periods)}
                for version in institution._history(column)
            ]
            for column in _VERSIONED_COLUMNS
        },
    }


def _span_to_dict(span: PeriodRange) -> dict[str, str]:
    """Return a span as ISO ``start`` and ``end`` dates.

    Parameters
    ----------
    span : PeriodRange
        The span to serialize.

    Returns
    -------
    dict[str, str]
        ``start`` and ``end`` as ``YYYY-MM-DD`` strings.
    """
    return {
        "start": span[0].period_end.isoformat(),
        "end": span[-1].period_end.isoformat(),
    }


def _institution_from_dict(data: Any) -> FCAInstitution:
    """Reconstruct a charter's history from `_institution_to_dict` output.

    Parameters
    ----------
    data : Any
        A decoded JSON value, expected to be the payload
        `_institution_to_dict` produces.

    Returns
    -------
    FCAInstitution
        The reconstructed, validated charter.

    Raises
    ------
    InstitutionError
        If `data` is not a dict or does not match the expected shape.
    """
    if not isinstance(data, dict):
        raise InstitutionError(f"expected a JSON object, got {type(data).__name__}.")
    try:
        attributes = data["attributes"]
        histories = {
            f"{stem}_history": tuple(
                InstitutionAttributeVersion(
                    value=version["value"],
                    periods=PeriodRange(start=version["start"], end=version["end"]),
                )
                for version in attributes[column]
            )
            for column, stem in _VERSIONED_COLUMNS.items()
        }
        return FCAInstitution(
            uninum=data["uninum"],
            system=data["system"],
            district=data["district"],
            association=data["association"],
            periods=tuple(
                PeriodRange(start=span["start"], end=span["end"])
                for span in data["periods"]
            ),
            **histories,
        )
    except (KeyError, TypeError, InvalidPeriodError) as error:
        raise InstitutionError(f"malformed institution JSON: {error!r}") from error
