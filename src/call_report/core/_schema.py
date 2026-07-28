"""Source-agnostic, cross-time schema vocabulary: FileMetadata and FieldSchema.

A call report file (schedule) and the fields within it can both change shape
over time: fields get added, dropped, or occasionally retired and later
reintroduced as a source's forms evolve. :class:`FieldAttributes` and
:class:`FileMetadata` capture that history uniformly across sources, in
terms of the periods each field or file is actually present for, so a single
object can answer "what did this look like across every period" rather than
one period at a time.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import date
from typing import Any

import narwhals as nw

from call_report.config import DataFrameBackend, config_context
from call_report.core._backend import (
    DataFrameType,
    build_frame,
    concat,
    convert_dataframe_type,
    finalize,
)
from call_report.core._periods import PeriodRange, ReportingPeriod
from call_report.exceptions import PeriodNotAvailableError, SchemaError

_FIELD_COLUMNS = ("field_name", "dtype", "definition", "period_start", "period_end")
_FILE_COLUMNS = ("file_name", *_FIELD_COLUMNS)


def _backend_context(backend: DataFrameBackend | None) -> AbstractContextManager[None]:
    """Return a context manager that activates `backend`, if one is given.

    Used by every `to_dataframe` method so a one-off `backend` override
    behaves the same as a scoped `call_report.config.config_context` call.

    Parameters
    ----------
    backend : {"pandas", "polars", "pyarrow"} or None
        The backend to activate for the block, or ``None`` to use whatever
        backend is already configured.

    Returns
    -------
    AbstractContextManager[None]
        `call_report.config.config_context` scoped to `backend`, or a
        no-op context manager if `backend` is ``None``.
    """
    if backend is None:
        return nullcontext()
    return config_context(dataframe_backend=backend)


def _rows(data: Any) -> list[dict[str, Any]]:
    """Collect a native dataframe's rows as plain dicts, via narwhals.

    Used by every `from_dataframe` classmethod so they can read `data`
    without knowing which backend produced it.

    Parameters
    ----------
    data : Any
        A native dataframe of any backend narwhals supports.

    Returns
    -------
    list[dict[str, Any]]
        One dict per row, in the dataframe's original order.
    """
    frame = nw.from_native(data)
    if isinstance(frame, nw.LazyFrame):
        frame = frame.collect()
    return frame.rows(named=True)


def _coerce_period(period: str | date | ReportingPeriod) -> ReportingPeriod:
    """Normalize a flexible period value into a ReportingPeriod.

    Used by `FieldSchema.as_of` and `FileMetadata.as_of` so both accept the
    same range of period inputs as the rest of this package's public API.

    Parameters
    ----------
    period : str, datetime.date, or ReportingPeriod
        A quarter-end value, in any of the forms this package's public API
        accepts.

    Returns
    -------
    ReportingPeriod
        `period` itself if it already was one, otherwise the period it
        parses to.
    """
    if isinstance(period, ReportingPeriod):
        return period
    return ReportingPeriod.from_period_end(value=period)


def _validate_period_spans(periods: tuple[PeriodRange, ...], label: str) -> None:
    """Validate that period spans are non-empty, ordered, and non-touching.

    Shared by `FieldAttributes` and `FileMetadata`, both of which represent
    "when was this present" as one or more contiguous `PeriodRange` spans
    separated by real gaps, rather than a single span that always covers
    the full range.

    Parameters
    ----------
    periods : tuple[PeriodRange, ...]
        The spans to validate, in the order they were supplied.
    label : str
        A short description of the object being validated, used to make
        the error message actionable.

    Raises
    ------
    SchemaError
        If `periods` is empty, or if any two spans are out of order,
        overlapping, or adjacent (adjacent spans should be merged into one).
    """
    if not periods:
        raise SchemaError(f"{label} must have at least one period span.")
    previous = periods[0]
    for span in periods[1:]:
        if span[0] <= previous[-1].next():
            raise SchemaError(
                f"{label} period spans must be chronologically ordered, "
                f"non-overlapping, and non-adjacent; the span starting "
                f"{span[0].label} is not far enough after the span ending "
                f"{previous[-1].label}."
            )
        previous = span


@dataclass(frozen=True, kw_only=True)
class FieldAttributes:
    """Cross-time metadata for a single field within a call report file.

    Unlike a per-period layout entry, this describes a field across its
    whole known history rather than a single period.

    Attributes
    ----------
    name : str
        The field's name, as it appears in the source's layout files.
    dtype : str
        The field's declared data type, in the source's own vocabulary
        (e.g. FCA's ``"Numeric"`` or ``"Alphanum."``).
    definition : str
        The field's human-readable definition, taken from the source's
        layout file.
    periods : tuple[PeriodRange, ...]
        One or more chronologically ordered, non-overlapping, non-adjacent
        spans describing when this field was present. More than one span
        means the field was dropped and later reintroduced.

    Raises
    ------
    SchemaError
        If `periods` is empty, or its spans are out of order, overlapping,
        or adjacent.

    Examples
    --------
    >>> from call_report.core import FieldAttributes, PeriodRange
    >>> uninum = FieldAttributes(
    ...     name="UNINUM",
    ...     dtype="Numeric",
    ...     definition="System, District, and Association codes concatenated.",
    ...     periods=(PeriodRange(start="2000-03-31", end="2026-03-31"),),
    ... )
    >>> uninum.first_period.label
    '2000Q1'
    """  # numpydoc ignore=PR01

    name: str
    dtype: str
    definition: str
    periods: tuple[PeriodRange, ...]

    def __post_init__(self) -> None:
        """Validate that `periods` forms well-ordered, non-overlapping spans.

        Runs automatically after construction, since this dataclass is
        frozen and cannot be validated any other way.

        Raises
        ------
        SchemaError
            If `periods` is empty, or any two spans are out of order,
            overlapping, or adjacent.
        """
        _validate_period_spans(self.periods, f"field {self.name!r}")

    @property
    def first_period(self) -> ReportingPeriod:
        """Return the earliest period this field is present in.

        This is the start of the earliest of this field's `periods` spans,
        not necessarily the start of the file it belongs to.

        Returns
        -------
        ReportingPeriod
            The first period of this field's earliest span.

        Examples
        --------
        >>> from call_report.core import FieldAttributes, PeriodRange
        >>> field = FieldAttributes(
        ...     name="UNINUM",
        ...     dtype="Numeric",
        ...     definition="",
        ...     periods=(PeriodRange(start="2000-03-31", end="2005-12-31"),),
        ... )
        >>> field.first_period.label
        '2000Q1'
        """
        return self.periods[0][0]

    @property
    def last_period(self) -> ReportingPeriod:
        """Return the latest period this field is present in.

        This is the end of the latest of this field's `periods` spans, not
        necessarily the end of the file it belongs to.

        Returns
        -------
        ReportingPeriod
            The last period of this field's latest span.

        Examples
        --------
        >>> from call_report.core import FieldAttributes, PeriodRange
        >>> field = FieldAttributes(
        ...     name="UNINUM",
        ...     dtype="Numeric",
        ...     definition="",
        ...     periods=(PeriodRange(start="2000-03-31", end="2005-12-31"),),
        ... )
        >>> field.last_period.label
        '2005Q4'
        """
        return self.periods[-1][-1]


class FieldSchema(Mapping[str, FieldAttributes]):
    """An ordered, immutable mapping of field name to FieldAttributes.

    Preserves the order fields were supplied in. Since instances are
    immutable, `subset` and `add_fields` return new `FieldSchema` instances
    rather than modifying this one.

    Parameters
    ----------
    fields : Iterable[FieldAttributes]
        The fields making up this schema, in order.

    Raises
    ------
    SchemaError
        If two or more fields in `fields` share the same name.

    Examples
    --------
    >>> from call_report.core import FieldAttributes, FieldSchema, PeriodRange
    >>> uninum = FieldAttributes(
    ...     name="UNINUM",
    ...     dtype="Numeric",
    ...     definition="",
    ...     periods=(PeriodRange(start="2000-03-31", end="2026-03-31"),),
    ... )
    >>> schema = FieldSchema(fields=[uninum])
    >>> schema["UNINUM"] is uninum
    True
    """

    def __init__(self, *, fields: Iterable[FieldAttributes]) -> None:
        ordered = tuple(fields)
        names = [field.name for field in ordered]
        duplicates = sorted(
            {name for name, count in Counter(names).items() if count > 1}
        )
        if duplicates:
            raise SchemaError(f"duplicate field name(s): {duplicates!r}.")
        self._ordered_fields: tuple[FieldAttributes, ...] = ordered
        self._by_name: dict[str, FieldAttributes] = {
            field.name: field for field in ordered
        }
        self._order: tuple[str, ...] = tuple(names)

    @property
    def names(self) -> tuple[str, ...]:
        """Return the field names in schema order.

        A convenience for inspecting field order without iterating.

        Returns
        -------
        tuple[str, ...]
            The field names, in the order fields were defined.

        Examples
        --------
        >>> from call_report.core import FieldAttributes, FieldSchema, PeriodRange
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM", dtype="Numeric", definition="", periods=(span,)
        ... )
        >>> FieldSchema(fields=[field]).names
        ('UNINUM',)
        """
        return self._order

    def subset(self, *, names: Iterable[str]) -> FieldSchema:
        """Return a new FieldSchema containing only the named fields.

        The result preserves this schema's existing field order; it does
        not reorder fields to match the order of `names`.

        Parameters
        ----------
        names : Iterable[str]
            The field names to keep.

        Returns
        -------
        FieldSchema
            A new schema containing only the requested fields.

        Raises
        ------
        SchemaError
            If any name in `names` is not a field in this schema.

        Examples
        --------
        >>> from call_report.core import FieldAttributes, FieldSchema, PeriodRange
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> uninum = FieldAttributes(
        ...     name="UNINUM", dtype="Numeric", definition="", periods=(span,)
        ... )
        >>> rssd = FieldAttributes(
        ...     name="RSSD", dtype="Numeric", definition="", periods=(span,)
        ... )
        >>> schema = FieldSchema(fields=[uninum, rssd])
        >>> schema.subset(names=["UNINUM"]).names
        ('UNINUM',)
        """
        requested = tuple(names)
        missing = sorted(name for name in requested if name not in self._by_name)
        if missing:
            raise SchemaError(f"unknown field name(s): {missing!r}.")
        keep = set(requested)
        return FieldSchema(
            fields=(field for field in self._ordered_fields if field.name in keep)
        )

    def add_fields(
        self,
        *,
        fields: FieldAttributes | Iterable[FieldAttributes],
        index: int | None = None,
    ) -> FieldSchema:
        """Return a new FieldSchema with one or more fields inserted.

        This schema is left unmodified; the caller receives a new instance.

        Parameters
        ----------
        fields : FieldAttributes or Iterable[FieldAttributes]
            The field(s) to add.
        index : int, optional
            The position to insert at, following `list.insert` position
            semantics. If omitted, the field(s) are appended at the end.

        Returns
        -------
        FieldSchema
            A new schema with `fields` inserted at `index`.

        Raises
        ------
        SchemaError
            If `fields` is empty, `index` is out of range, or the result
            would contain a duplicate field name.

        Examples
        --------
        >>> from call_report.core import FieldAttributes, FieldSchema, PeriodRange
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> uninum = FieldAttributes(
        ...     name="UNINUM", dtype="Numeric", definition="", periods=(span,)
        ... )
        >>> rssd = FieldAttributes(
        ...     name="RSSD", dtype="Numeric", definition="", periods=(span,)
        ... )
        >>> FieldSchema(fields=[uninum]).add_fields(fields=rssd, index=0).names
        ('RSSD', 'UNINUM')
        """
        new_fields = (fields,) if isinstance(fields, FieldAttributes) else tuple(fields)
        if not new_fields:
            raise SchemaError("must supply at least one field to add.")
        existing = list(self._ordered_fields)
        position = len(existing) if index is None else index
        if not 0 <= position <= len(existing):
            raise SchemaError(
                f"index {index} is out of range for {len(existing)} existing field(s)."
            )
        return FieldSchema(
            fields=[*existing[:position], *new_fields, *existing[position:]]
        )

    def as_of(self, *, period: str | date | ReportingPeriod) -> FieldSchema:
        """Return a new FieldSchema of only the fields present as of `period`.

        Each surviving field's `periods` is narrowed to the single quarter
        `period`, since this is a point-in-time snapshot rather than a
        history.

        Parameters
        ----------
        period : str, datetime.date, or ReportingPeriod
            The quarter-end to take the snapshot at.

        Returns
        -------
        FieldSchema
            A new schema containing only fields present at `period`.

        Examples
        --------
        >>> from call_report.core import FieldAttributes, FieldSchema, PeriodRange
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM", dtype="Numeric", definition="", periods=(span,)
        ... )
        >>> FieldSchema(fields=[field]).as_of(period="2010-03-31").names
        ('UNINUM',)
        """
        resolved = _coerce_period(period)
        snapshot = (PeriodRange(start=resolved, end=resolved),)
        return FieldSchema(
            fields=(
                FieldAttributes(
                    name=field.name,
                    dtype=field.dtype,
                    definition=field.definition,
                    periods=snapshot,
                )
                for field in self._ordered_fields
                if any(resolved in span for span in field.periods)
            )
        )

    def to_dataframe(
        self,
        *,
        backend: DataFrameBackend | None = None,
        dataframe_type: DataFrameType | None = None,
    ) -> Any:
        """Return this schema as a native dataframe, one row per field span.

        A field present across more than one span (i.e. dropped and later
        reintroduced) contributes one row per span, all sharing the same
        `dtype` and `definition`.

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
            get back whatever `backend` produced; set it when the next step
            in your own code needs a specific type -- e.g. this package is
            configured to use polars, but the code after this call expects
            a pandas DataFrame. Converted via `call_report.core._backend`'s
            narwhals-backed `convert_dataframe_type`, which is zero-copy
            when the requested type already matches.

        Returns
        -------
        Any
            A native dataframe with columns ``field_name``, ``dtype``,
            ``definition``, ``period_start``, and ``period_end`` (the last
            two as ISO ``YYYY-MM-DD`` strings).

        Examples
        --------
        >>> from call_report.core import FieldAttributes, FieldSchema, PeriodRange
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM", dtype="Numeric", definition="", periods=(span,)
        ... )
        >>> frame = FieldSchema(fields=[field]).to_dataframe()
        >>> list(frame.columns)
        ['field_name', 'dtype', 'definition', 'period_start', 'period_end']
        """
        columns: dict[str, list[Any]] = {column: [] for column in _FIELD_COLUMNS}
        for field in self._ordered_fields:
            for span in field.periods:
                columns["field_name"].append(field.name)
                columns["dtype"].append(field.dtype)
                columns["definition"].append(field.definition)
                columns["period_start"].append(span[0].period_end.isoformat())
                columns["period_end"].append(span[-1].period_end.isoformat())
        with _backend_context(backend):
            native = finalize(frame=build_frame(data=columns))
        return convert_dataframe_type(data=native, dataframe_type=dataframe_type)

    @classmethod
    def from_dataframe(cls, *, data: Any) -> FieldSchema:
        """Reconstruct a FieldSchema from a dataframe built by `to_dataframe`.

        Rows are grouped by ``field_name``, so a field is reconstructed
        with all of its spans even if `data` has one row per span.

        Parameters
        ----------
        data : Any
            A native dataframe with the columns `to_dataframe` produces.

        Returns
        -------
        FieldSchema
            The reconstructed schema.

        Examples
        --------
        >>> from call_report.core import FieldAttributes, FieldSchema, PeriodRange
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM", dtype="Numeric", definition="", periods=(span,)
        ... )
        >>> frame = FieldSchema(fields=[field]).to_dataframe()
        >>> FieldSchema.from_dataframe(data=frame).names
        ('UNINUM',)
        """
        order: list[str] = []
        dtypes: dict[str, str] = {}
        definitions: dict[str, str] = {}
        spans: dict[str, list[PeriodRange]] = {}
        for row in _rows(data):
            name = row["field_name"]
            if name not in spans:
                order.append(name)
                spans[name] = []
                dtypes[name] = row["dtype"]
                definitions[name] = row["definition"]
            spans[name].append(
                PeriodRange(start=row["period_start"], end=row["period_end"])
            )
        return cls(
            fields=[
                FieldAttributes(
                    name=name,
                    dtype=dtypes[name],
                    definition=definitions[name],
                    periods=tuple(spans[name]),
                )
                for name in order
            ]
        )

    def __getitem__(self, name: str) -> FieldAttributes:
        """Return the field with the given name.

        Implements ``schema[name]`` lookup for `FieldSchema`.

        Parameters
        ----------
        name : str
            The field name to look up.

        Returns
        -------
        FieldAttributes
            The metadata for `name`.

        Raises
        ------
        KeyError
            If `name` is not a field in this schema.

        Examples
        --------
        >>> from call_report.core import FieldAttributes, FieldSchema, PeriodRange
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM", dtype="Numeric", definition="", periods=(span,)
        ... )
        >>> FieldSchema(fields=[field])["UNINUM"] is field
        True
        """
        return self._by_name[name]

    def __iter__(self) -> Iterator[str]:
        """Iterate over field names in schema order.

        Implements the iteration protocol so a `FieldSchema` can be looped
        over, or passed anywhere an iterable of names is expected.

        Returns
        -------
        Iterator[str]
            An iterator over field names, in definition order.

        Examples
        --------
        >>> from call_report.core import FieldAttributes, FieldSchema, PeriodRange
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM", dtype="Numeric", definition="", periods=(span,)
        ... )
        >>> list(FieldSchema(fields=[field]))
        ['UNINUM']
        """
        return iter(self._order)

    def __len__(self) -> int:
        """Return the number of fields in this schema.

        Implements the `len` builtin for `FieldSchema`.

        Returns
        -------
        int
            The field count.

        Examples
        --------
        >>> from call_report.core import FieldAttributes, FieldSchema, PeriodRange
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM", dtype="Numeric", definition="", periods=(span,)
        ... )
        >>> len(FieldSchema(fields=[field]))
        1
        """
        return len(self._order)

    def __repr__(self) -> str:
        """Return a repr showing this schema's field names.

        Implements `repr` for `FieldSchema`.

        Returns
        -------
        str
            A string of the form ``FieldSchema(names=(...))``.

        Examples
        --------
        >>> from call_report.core import FieldAttributes, FieldSchema, PeriodRange
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM", dtype="Numeric", definition="", periods=(span,)
        ... )
        >>> FieldSchema(fields=[field])
        FieldSchema(names=('UNINUM',))
        """
        return f"FieldSchema(names={self._order!r})"


@dataclass(frozen=True, kw_only=True)
class FileMetadata:
    """Cross-time metadata for a single call report file (schedule).

    Unlike a per-period layout, this describes a file across its whole
    known history, including whether its column schema ever changed.

    Attributes
    ----------
    name : str
        The file's identifier, in the source's own vocabulary (e.g. FCA's
        schedule root such as ``"RCB"``).
    periods : tuple[PeriodRange, ...]
        One or more chronologically ordered, non-overlapping, non-adjacent
        spans describing when this file was published. More than one span
        means the file was retired and later reintroduced.
    schema : FieldSchema
        The file's fields, keyed by name.

    Raises
    ------
    SchemaError
        If `periods` is empty, or its spans are out of order, overlapping,
        or adjacent.

    Examples
    --------
    >>> from call_report.core import (
    ...     FieldAttributes,
    ...     FieldSchema,
    ...     FileMetadata,
    ...     PeriodRange,
    ... )
    >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
    >>> uninum = FieldAttributes(
    ...     name="UNINUM", dtype="Numeric", definition="", periods=(span,)
    ... )
    >>> metadata = FileMetadata(
    ...     name="RCB", periods=(span,), schema=FieldSchema(fields=[uninum])
    ... )
    >>> metadata.changed
    False
    """  # numpydoc ignore=PR01

    name: str
    periods: tuple[PeriodRange, ...]
    schema: FieldSchema

    def __post_init__(self) -> None:
        """Validate that `periods` forms well-ordered, non-overlapping spans.

        Runs automatically after construction, since this dataclass is
        frozen and cannot be validated any other way.

        Raises
        ------
        SchemaError
            If `periods` is empty, or any two spans are out of order,
            overlapping, or adjacent.
        """
        _validate_period_spans(self.periods, f"file {self.name!r}")

    @property
    def first_period(self) -> ReportingPeriod:
        """Return the earliest period this file was published in.

        This is the start of the earliest of this file's `periods` spans.

        Returns
        -------
        ReportingPeriod
            The first period of this file's earliest span.

        Examples
        --------
        >>> from call_report.core import FieldSchema, FileMetadata, PeriodRange
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> metadata = FileMetadata(
        ...     name="RCB", periods=(span,), schema=FieldSchema(fields=[])
        ... )
        >>> metadata.first_period.label
        '2000Q1'
        """
        return self.periods[0][0]

    @property
    def last_period(self) -> ReportingPeriod:
        """Return the latest period this file was published in.

        This is the end of the latest of this file's `periods` spans.

        Returns
        -------
        ReportingPeriod
            The last period of this file's latest span.

        Examples
        --------
        >>> from call_report.core import FieldSchema, FileMetadata, PeriodRange
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> metadata = FileMetadata(
        ...     name="RCB", periods=(span,), schema=FieldSchema(fields=[])
        ... )
        >>> metadata.last_period.label
        '2026Q1'
        """
        return self.periods[-1][-1]

    @property
    def changed(self) -> bool:
        """Return whether any field's presence differs from this file's own.

        A field whose `periods` do not exactly match this file's `periods`
        was added after the file's first period, dropped before its last,
        or has a gap the file itself does not have -- in every case, the
        file's column schema was not identical across its whole history.

        Returns
        -------
        bool
            ``True`` if any field's presence differs from the file's own.

        Examples
        --------
        >>> from call_report.core import (
        ...     FieldAttributes,
        ...     FieldSchema,
        ...     FileMetadata,
        ...     PeriodRange,
        ... )
        >>> full = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> partial = PeriodRange(start="2010-03-31", end="2026-03-31")
        >>> added_later = FieldAttributes(
        ...     name="RSSD", dtype="Numeric", definition="", periods=(partial,)
        ... )
        >>> schema = FieldSchema(fields=[added_later])
        >>> FileMetadata(name="RCB", periods=(full,), schema=schema).changed
        True
        """
        return any(field.periods != self.periods for field in self.schema.values())

    def as_of(self, *, period: str | date | ReportingPeriod) -> FileMetadata:
        """Return a new FileMetadata snapshot as of `period`.

        Delegates field selection to `FieldSchema.as_of`; the result's own
        `periods` is likewise narrowed to the single quarter `period`.

        Parameters
        ----------
        period : str, datetime.date, or ReportingPeriod
            The quarter-end to take the snapshot at.

        Returns
        -------
        FileMetadata
            A new instance describing only `period`.

        Raises
        ------
        PeriodNotAvailableError
            If this file was not published as of `period`.

        Examples
        --------
        >>> from call_report.core import (
        ...     FieldAttributes,
        ...     FieldSchema,
        ...     FileMetadata,
        ...     PeriodRange,
        ... )
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM", dtype="Numeric", definition="", periods=(span,)
        ... )
        >>> metadata = FileMetadata(
        ...     name="RCB", periods=(span,), schema=FieldSchema(fields=[field])
        ... )
        >>> metadata.as_of(period="2010-03-31").schema.names
        ('UNINUM',)
        """
        resolved = _coerce_period(period)
        if not any(resolved in span for span in self.periods):
            raise PeriodNotAvailableError(
                f"file {self.name!r} was not published as of {resolved.label}; "
                f"known periods span {self.first_period.label} to "
                f"{self.last_period.label}."
            )
        return FileMetadata(
            name=self.name,
            periods=(PeriodRange(start=resolved, end=resolved),),
            schema=self.schema.as_of(period=resolved),
        )

    def to_dataframe(
        self,
        *,
        backend: DataFrameBackend | None = None,
        dataframe_type: DataFrameType | None = None,
    ) -> Any:
        """Return this file's metadata as a native dataframe.

        Built from `FieldSchema.to_dataframe`, with a ``file_name`` column
        added and one extra row per file-level period span (identifiable by
        an empty ``field_name``), so this file's own `periods` round-trip
        through `from_dataframe` alongside its fields.

        Parameters
        ----------
        backend : {"pandas", "polars", "pyarrow"}, optional
            The dataframe library used to build the frame (passed through
            to `FieldSchema.to_dataframe`). If omitted, uses whatever
            backend is currently configured via
            `call_report.config.get_config`. Most users can leave this at
            its default.
        dataframe_type : {"pandas", "pyarrow_table", "polars_lazyframe", \
"polars_dataframe"}, optional
            The dataframe type to convert the result to as a final step,
            regardless of `backend`. Leave this ``None`` (the default) to
            get back whatever `backend` produced; set it when the next step
            in your own code needs a specific type -- e.g. this package is
            configured to use polars, but the code after this call expects
            a pandas DataFrame. Converted via `call_report.core._backend`'s
            narwhals-backed `convert_dataframe_type`, which is zero-copy
            when the requested type already matches.

        Returns
        -------
        Any
            A native dataframe with columns ``file_name``, ``field_name``,
            ``dtype``, ``definition``, ``period_start``, and ``period_end``.

        Examples
        --------
        >>> from call_report.core import (
        ...     FieldAttributes,
        ...     FieldSchema,
        ...     FileMetadata,
        ...     PeriodRange,
        ... )
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM", dtype="Numeric", definition="", periods=(span,)
        ... )
        >>> metadata = FileMetadata(
        ...     name="RCB", periods=(span,), schema=FieldSchema(fields=[field])
        ... )
        >>> frame = metadata.to_dataframe()
        >>> list(frame.columns)
        ['file_name', 'field_name', 'dtype', 'definition', 'period_start', 'period_end']
        """
        fields_frame = nw.from_native(self.schema.to_dataframe(backend=backend))
        if isinstance(fields_frame, nw.LazyFrame):
            fields_frame = fields_frame.collect()
        fields_frame = fields_frame.with_columns(
            nw.lit(self.name).alias("file_name")
        ).select(*_FILE_COLUMNS)

        file_rows: dict[str, list[Any]] = {
            "file_name": [self.name] * len(self.periods),
            "field_name": [""] * len(self.periods),
            "dtype": [""] * len(self.periods),
            "definition": [""] * len(self.periods),
            "period_start": [span[0].period_end.isoformat() for span in self.periods],
            "period_end": [span[-1].period_end.isoformat() for span in self.periods],
        }
        with _backend_context(backend):
            file_frame = build_frame(data=file_rows)
            combined = concat(frames=[file_frame, fields_frame], how="strict")
            native = finalize(frame=combined)
        return convert_dataframe_type(data=native, dataframe_type=dataframe_type)

    @classmethod
    def from_dataframe(cls, *, data: Any) -> FileMetadata:
        """Reconstruct a FileMetadata from a dataframe built by `to_dataframe`.

        Rows with an empty ``field_name`` are this file's own period spans;
        every other row is delegated to `FieldSchema.from_dataframe`.

        Parameters
        ----------
        data : Any
            A native dataframe with the columns `to_dataframe` produces.

        Returns
        -------
        FileMetadata
            The reconstructed file metadata.

        Raises
        ------
        SchemaError
            If `data` has no file-level period rows, or rows naming more
            than one distinct ``file_name``.

        Examples
        --------
        >>> from call_report.core import (
        ...     FieldAttributes,
        ...     FieldSchema,
        ...     FileMetadata,
        ...     PeriodRange,
        ... )
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM", dtype="Numeric", definition="", periods=(span,)
        ... )
        >>> metadata = FileMetadata(
        ...     name="RCB", periods=(span,), schema=FieldSchema(fields=[field])
        ... )
        >>> frame = metadata.to_dataframe()
        >>> FileMetadata.from_dataframe(data=frame).name
        'RCB'
        """
        frame = nw.from_native(data)
        if isinstance(frame, nw.LazyFrame):
            frame = frame.collect()

        file_rows = frame.filter(nw.col("field_name") == "").rows(named=True)
        if not file_rows:
            raise SchemaError(
                "`data` has no file-level period rows (an empty `field_name`)."
            )
        file_names = {row["file_name"] for row in file_rows}
        if len(file_names) != 1:
            raise SchemaError(
                f"expected exactly one `file_name`, found {sorted(file_names)!r}."
            )
        periods = tuple(
            PeriodRange(start=row["period_start"], end=row["period_end"])
            for row in file_rows
        )

        field_frame = (
            frame.filter(nw.col("field_name") != "").select(*_FIELD_COLUMNS).to_native()
        )
        return cls(
            name=next(iter(file_names)),
            periods=periods,
            schema=FieldSchema.from_dataframe(data=field_frame),
        )
