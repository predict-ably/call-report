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

import ast
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, Literal, NoReturn, overload

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

if TYPE_CHECKING:
    import pandas as pd
    import polars as pl
    import pyarrow as pa

    from call_report.core._backend import NativeDataFrame

_FIELD_COLUMNS = ("field_name", "dtype", "definition", "period_start", "period_end")
_FILE_COLUMNS = ("file_name", *_FIELD_COLUMNS)


def _resolve_dtype_node(node: ast.expr) -> Any:
    """Resolve one AST node from a narwhals dtype repr into a Python value.

    Recursive helper for `_dtype_from_repr`. Handles exactly the node
    shapes narwhals dtype reprs use: bare dtype names, literals, dict/
    list/tuple literals for nested dtype arguments (e.g. `Struct`'s field
    mapping, `Array`'s shape), and constructor calls. Every name is
    resolved only against narwhals' own dtype classes, not the wider
    `narwhals` namespace, so no other narwhals API is reachable regardless
    of the input.

    Parameters
    ----------
    node : ast.expr
        One node from the parsed repr's expression tree.

    Returns
    -------
    Any
        The resolved value: a narwhals dtype class or instance, a literal
        scalar, or a container of either.

    Raises
    ------
    TypeError
        If `node` is a name that is not a narwhals dtype class, or any
        node kind not used by narwhals dtype reprs.
    """
    if isinstance(node, ast.Name):
        candidate = getattr(nw, node.id, None)
        if not (isinstance(candidate, type) and issubclass(candidate, nw.dtypes.DType)):
            raise TypeError(f"{node.id!r} is not a narwhals dtype.")
        return candidate
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Dict):
        resolved_items: dict[Any, Any] = {}
        for key, value in zip(node.keys, node.values, strict=True):
            if key is None:
                raise TypeError("dict unpacking is not a valid dtype repr.")
            resolved_items[_resolve_dtype_node(key)] = _resolve_dtype_node(value)
        return resolved_items
    if isinstance(node, ast.List):
        return [_resolve_dtype_node(element) for element in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_resolve_dtype_node(element) for element in node.elts)
    if isinstance(node, ast.Call):
        func = _resolve_dtype_node(node.func)
        args = [_resolve_dtype_node(arg) for arg in node.args]
        kwargs = {
            kw.arg: _resolve_dtype_node(kw.value)
            for kw in node.keywords
            if kw.arg is not None
        }
        return func(*args, **kwargs)
    raise TypeError(f"unsupported dtype repr syntax: {ast.dump(node)}")


def _dtype_from_repr(text: str) -> nw.dtypes.DType:
    """Reconstruct a narwhals dtype instance from its repr, safely.

    The inverse of ``repr(dtype)`` for any narwhals `DType` (e.g.
    ``"Int64"``, ``"Datetime(time_unit='us', time_zone='UTC')"``, or a
    deeply nested ``"Array(List(Struct({'a': Int64})), shape=(2,))"``).
    Parses `text` as an AST rather than calling `eval`, so no arbitrary
    code -- or even an arbitrary narwhals API call -- is reachable
    regardless of what `text` contains.

    Parameters
    ----------
    text : str
        A narwhals dtype's `repr`, as stored by `FieldSchema.to_dataframe`.

    Returns
    -------
    narwhals.dtypes.DType
        The reconstructed dtype instance.

    Raises
    ------
    SchemaError
        If `text` is not a valid narwhals dtype repr.
    """
    try:
        resolved = _resolve_dtype_node(ast.parse(text, mode="eval").body)
        if isinstance(resolved, type):
            resolved = resolved()
    except (
        SyntaxError,
        TypeError,
        AttributeError,
        ValueError,
        RecursionError,
    ) as error:
        raise SchemaError(f"{text!r} is not a valid narwhals dtype repr.") from error
    if not isinstance(resolved, nw.dtypes.DType):
        raise SchemaError(f"{text!r} is not a valid narwhals dtype repr.")
    return resolved


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
    dtype : narwhals.dtypes.DType
        The field's data type, as an instantiated narwhals dtype (e.g.
        `narwhals.Int64()`, `narwhals.String()`). Translating a source's
        own vocabulary (e.g. FCA's ``"Numeric"``/``"Alphanum."``) into a
        narwhals dtype is that source's own responsibility -- this class
        is shared across every source, so it only ever deals in narwhals'
        vocabulary.
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
    >>> import narwhals as nw
    >>> from call_report.core import FieldAttributes, PeriodRange
    >>> uninum = FieldAttributes(
    ...     name="UNINUM",
    ...     dtype=nw.Int64(),
    ...     definition="System, District, and Association codes concatenated.",
    ...     periods=(PeriodRange(start="2000-03-31", end="2026-03-31"),),
    ... )
    >>> uninum.first_period.label
    '2000Q1'
    """  # numpydoc ignore=PR01

    name: str
    dtype: nw.dtypes.DType
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
        >>> import narwhals as nw
        >>> from call_report.core import FieldAttributes, PeriodRange
        >>> field = FieldAttributes(
        ...     name="UNINUM",
        ...     dtype=nw.Int64(),
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
        >>> import narwhals as nw
        >>> from call_report.core import FieldAttributes, PeriodRange
        >>> field = FieldAttributes(
        ...     name="UNINUM",
        ...     dtype=nw.Int64(),
        ...     definition="",
        ...     periods=(PeriodRange(start="2000-03-31", end="2005-12-31"),),
        ... )
        >>> field.last_period.label
        '2005Q4'
        """
        return self.periods[-1][-1]


class _ReadOnlySchema(nw.Schema):
    """A narwhals Schema that rejects in-place mutation after construction.

    Backs `FieldSchema.schema`: a plain `@property` only stops the
    *attribute* from being reassigned, but `narwhals.Schema` is itself a
    mutable `OrderedDict` subclass, so without this a caller could still
    mutate the stored schema in place (e.g. ``field_schema.schema["X"] =
    ...``). Every dict/`OrderedDict` mutation entry point is overridden to
    raise, so the returned instance is genuinely read-only -- and since
    that's enforced by the object's own class rather than by copying on
    each access, `FieldSchema.schema` can return the same stored instance
    every time.

    Parameters
    ----------
    schema : Mapping[str, narwhals.dtypes.DType] or Iterable[tuple[str, \
narwhals.dtypes.DType]], optional
        The column name to dtype mapping to populate, as accepted by
        `narwhals.Schema`.
    """

    def __init__(
        self,
        schema: Mapping[str, nw.dtypes.DType]
        | Iterable[tuple[str, nw.dtypes.DType]]
        | None = None,
    ) -> None:
        super().__init__(schema)
        self._frozen = True

    def __repr__(self) -> str:
        """Return a repr matching plain `narwhals.Schema`'s own format.

        `OrderedDict.__repr__` reflects the actual runtime class name, which
        would otherwise print this private wrapper class's name instead of
        looking like the `narwhals.Schema` it actually is.

        Returns
        -------
        str
            A string of the form ``Schema({'name': DType, ...})``.
        """
        return f"Schema({dict(self)!r})"

    def __setitem__(self, key: str, value: nw.dtypes.DType) -> None:
        """Raise, once frozen; populate normally during construction otherwise.

        `OrderedDict.__init__` populates entries by calling this method, so
        it cannot unconditionally raise -- `_frozen` is only set once
        `__init__` finishes, distinguishing initial population from any
        later mutation attempt.

        Parameters
        ----------
        key : str
            The column name.
        value : narwhals.dtypes.DType
            The dtype to associate with `key`.

        Raises
        ------
        TypeError
            If this schema has already finished construction.
        """
        if getattr(self, "_frozen", False):
            raise TypeError("FieldSchema.schema is read-only.")
        super().__setitem__(key, value)

    def __delitem__(self, key: str) -> None:
        """Raise; deleting a column would mutate a read-only schema.

        Implements the ``del schema[key]`` statement.

        Parameters
        ----------
        key : str
            The column name that would have been removed.

        Raises
        ------
        TypeError
            Always.
        """
        raise TypeError("FieldSchema.schema is read-only.")

    def clear(self) -> None:
        """Raise; clearing would mutate a read-only schema.

        Implements the ``dict.clear`` method.

        Raises
        ------
        TypeError
            Always.
        """
        raise TypeError("FieldSchema.schema is read-only.")

    def pop(self, *args: Any, **kwargs: Any) -> Any:
        """Raise; popping a column would mutate a read-only schema.

        Implements the ``dict.pop`` method.

        Parameters
        ----------
        *args : Any
            Ignored.
        **kwargs : Any
            Ignored.

        Returns
        -------
        Any
            Never returns.

        Raises
        ------
        TypeError
            Always.
        """
        raise TypeError("FieldSchema.schema is read-only.")

    def popitem(self, *args: Any, **kwargs: Any) -> Any:
        """Raise; popping a column would mutate a read-only schema.

        Implements the ``dict.popitem`` method.

        Parameters
        ----------
        *args : Any
            Ignored.
        **kwargs : Any
            Ignored.

        Returns
        -------
        Any
            Never returns.

        Raises
        ------
        TypeError
            Always.
        """
        raise TypeError("FieldSchema.schema is read-only.")

    def setdefault(self, *args: Any, **kwargs: Any) -> Any:
        """Raise; inserting a default would mutate a read-only schema.

        Implements the ``dict.setdefault`` method.

        Parameters
        ----------
        *args : Any
            Ignored.
        **kwargs : Any
            Ignored.

        Returns
        -------
        Any
            Never returns.

        Raises
        ------
        TypeError
            Always.
        """
        raise TypeError("FieldSchema.schema is read-only.")

    def update(self, *args: Any, **kwargs: Any) -> None:
        """Raise; updating would mutate a read-only schema.

        Implements the ``dict.update`` method.

        Parameters
        ----------
        *args : Any
            Ignored.
        **kwargs : Any
            Ignored.

        Raises
        ------
        TypeError
            Always.
        """
        raise TypeError("FieldSchema.schema is read-only.")

    def move_to_end(self, *args: Any, **kwargs: Any) -> None:
        """Raise; reordering would mutate a read-only schema.

        Implements the ``OrderedDict.move_to_end`` method.

        Parameters
        ----------
        *args : Any
            Ignored.
        **kwargs : Any
            Ignored.

        Raises
        ------
        TypeError
            Always.
        """
        raise TypeError("FieldSchema.schema is read-only.")

    # __or__ is inherited, not redefined here, which trips a known mypy
    # false positive comparing it against __ior__: python/mypy#11941.
    def __ior__(self, other: Any) -> NoReturn:  # type: ignore[misc]
        """Raise; merging in place would mutate a read-only schema.

        Implements the ``|=`` operator.

        Parameters
        ----------
        other : Any
            Ignored.

        Raises
        ------
        TypeError
            Always.
        """
        raise TypeError("FieldSchema.schema is read-only.")


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
    >>> import narwhals as nw
    >>> from call_report.core import FieldAttributes, FieldSchema, PeriodRange
    >>> uninum = FieldAttributes(
    ...     name="UNINUM",
    ...     dtype=nw.Int64(),
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
        self._schema = _ReadOnlySchema((field.name, field.dtype) for field in ordered)

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
        >>> import narwhals as nw
        >>> from call_report.core import FieldAttributes, FieldSchema, PeriodRange
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM", dtype=nw.Int64(), definition="", periods=(span,)
        ... )
        >>> FieldSchema(fields=[field]).names
        ('UNINUM',)
        """
        return self._order

    @property
    def schema(self) -> nw.Schema:
        """Return this schema's fields as a narwhals Schema.

        Built once at construction from each field's `name` and `dtype`,
        so `subset`, `add_fields`, `as_of`, and `from_dataframe` all get an
        up to date narwhals Schema for free -- they construct their result
        via `FieldSchema(fields=...)`, which runs this same construction
        logic. Read-only: the returned object rejects in-place mutation
        (e.g. item assignment), so it cannot be used to change this
        schema's fields.

        Returns
        -------
        narwhals.Schema
            An ordered mapping of field name to narwhals dtype.

        Examples
        --------
        >>> import narwhals as nw
        >>> from call_report.core import FieldAttributes, FieldSchema, PeriodRange
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM", dtype=nw.Int64(), definition="", periods=(span,)
        ... )
        >>> FieldSchema(fields=[field]).schema
        Schema({'UNINUM': Int64})
        """
        return self._schema

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
        >>> import narwhals as nw
        >>> from call_report.core import FieldAttributes, FieldSchema, PeriodRange
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> uninum = FieldAttributes(
        ...     name="UNINUM", dtype=nw.Int64(), definition="", periods=(span,)
        ... )
        >>> rssd = FieldAttributes(
        ...     name="RSSD", dtype=nw.Int64(), definition="", periods=(span,)
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
        >>> import narwhals as nw
        >>> from call_report.core import FieldAttributes, FieldSchema, PeriodRange
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> uninum = FieldAttributes(
        ...     name="UNINUM", dtype=nw.Int64(), definition="", periods=(span,)
        ... )
        >>> rssd = FieldAttributes(
        ...     name="RSSD", dtype=nw.Int64(), definition="", periods=(span,)
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
        >>> import narwhals as nw
        >>> from call_report.core import FieldAttributes, FieldSchema, PeriodRange
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM", dtype=nw.Int64(), definition="", periods=(span,)
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
    ) -> pd.DataFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_dataframe(
        self,
        *,
        backend: DataFrameBackend | None = None,
        dataframe_type: Literal["pyarrow_table"],
    ) -> pa.Table:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_dataframe(
        self,
        *,
        backend: DataFrameBackend | None = None,
        dataframe_type: Literal["polars_dataframe"],
    ) -> pl.DataFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_dataframe(
        self,
        *,
        backend: DataFrameBackend | None = None,
        dataframe_type: Literal["polars_lazyframe"],
    ) -> pl.LazyFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    def to_dataframe(
        self,
        *,
        backend: DataFrameBackend | None = None,
        dataframe_type: DataFrameType | None = None,
    ) -> NativeDataFrame:
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
        NativeDataFrame
            A native dataframe with columns ``field_name``, ``dtype``,
            ``definition``, ``period_start``, and ``period_end`` (the last
            two as ISO ``YYYY-MM-DD`` strings). ``dtype`` holds each
            field's narwhals dtype as its `repr` (e.g. ``"Int64"``,
            ``"Datetime(time_unit='us', time_zone='UTC')"``) -- a plain
            string, so it round-trips through every backend, including
            pyarrow, which cannot hold arbitrary Python objects as column
            values.

        Examples
        --------
        >>> import narwhals as nw
        >>> from call_report.core import FieldAttributes, FieldSchema, PeriodRange
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM", dtype=nw.Int64(), definition="", periods=(span,)
        ... )
        >>> frame = FieldSchema(fields=[field]).to_dataframe()
        >>> list(frame.columns)
        ['field_name', 'dtype', 'definition', 'period_start', 'period_end']
        """
        columns: dict[str, list[Any]] = {column: [] for column in _FIELD_COLUMNS}
        for field in self._ordered_fields:
            for span in field.periods:
                columns["field_name"].append(field.name)
                columns["dtype"].append(repr(field.dtype))
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
        >>> import narwhals as nw
        >>> from call_report.core import FieldAttributes, FieldSchema, PeriodRange
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM", dtype=nw.Int64(), definition="", periods=(span,)
        ... )
        >>> frame = FieldSchema(fields=[field]).to_dataframe()
        >>> FieldSchema.from_dataframe(data=frame).names
        ('UNINUM',)
        """
        order: list[str] = []
        dtypes: dict[str, nw.dtypes.DType] = {}
        definitions: dict[str, str] = {}
        spans: dict[str, list[PeriodRange]] = {}
        for row in _rows(data):
            name = row["field_name"]
            if name not in spans:
                order.append(name)
                spans[name] = []
                dtypes[name] = _dtype_from_repr(row["dtype"])
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
        >>> import narwhals as nw
        >>> from call_report.core import FieldAttributes, FieldSchema, PeriodRange
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM", dtype=nw.Int64(), definition="", periods=(span,)
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
        >>> import narwhals as nw
        >>> from call_report.core import FieldAttributes, FieldSchema, PeriodRange
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM", dtype=nw.Int64(), definition="", periods=(span,)
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
        >>> import narwhals as nw
        >>> from call_report.core import FieldAttributes, FieldSchema, PeriodRange
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM", dtype=nw.Int64(), definition="", periods=(span,)
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
        >>> import narwhals as nw
        >>> from call_report.core import FieldAttributes, FieldSchema, PeriodRange
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM", dtype=nw.Int64(), definition="", periods=(span,)
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
    file_schema : FieldSchema
        The file's fields, keyed by name.

    Raises
    ------
    SchemaError
        If `periods` is empty, or its spans are out of order, overlapping,
        or adjacent.

    Examples
    --------
    >>> import narwhals as nw
    >>> from call_report.core import (
    ...     FieldAttributes,
    ...     FieldSchema,
    ...     FileMetadata,
    ...     PeriodRange,
    ... )
    >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
    >>> uninum = FieldAttributes(
    ...     name="UNINUM", dtype=nw.Int64(), definition="", periods=(span,)
    ... )
    >>> metadata = FileMetadata(
    ...     name="RCB", periods=(span,), file_schema=FieldSchema(fields=[uninum])
    ... )
    >>> metadata.changed
    False
    """  # numpydoc ignore=PR01

    name: str
    periods: tuple[PeriodRange, ...]
    file_schema: FieldSchema

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
        ...     name="RCB", periods=(span,), file_schema=FieldSchema(fields=[])
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
        ...     name="RCB", periods=(span,), file_schema=FieldSchema(fields=[])
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
        >>> import narwhals as nw
        >>> from call_report.core import (
        ...     FieldAttributes,
        ...     FieldSchema,
        ...     FileMetadata,
        ...     PeriodRange,
        ... )
        >>> full = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> partial = PeriodRange(start="2010-03-31", end="2026-03-31")
        >>> added_later = FieldAttributes(
        ...     name="RSSD", dtype=nw.Int64(), definition="", periods=(partial,)
        ... )
        >>> schema = FieldSchema(fields=[added_later])
        >>> FileMetadata(name="RCB", periods=(full,), file_schema=schema).changed
        True
        """
        return any(field.periods != self.periods for field in self.file_schema.values())

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
        >>> import narwhals as nw
        >>> from call_report.core import (
        ...     FieldAttributes,
        ...     FieldSchema,
        ...     FileMetadata,
        ...     PeriodRange,
        ... )
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM", dtype=nw.Int64(), definition="", periods=(span,)
        ... )
        >>> metadata = FileMetadata(
        ...     name="RCB", periods=(span,), file_schema=FieldSchema(fields=[field])
        ... )
        >>> metadata.as_of(period="2010-03-31").file_schema.names
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
            file_schema=self.file_schema.as_of(period=resolved),
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
    ) -> pd.DataFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_dataframe(
        self,
        *,
        backend: DataFrameBackend | None = None,
        dataframe_type: Literal["pyarrow_table"],
    ) -> pa.Table:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_dataframe(
        self,
        *,
        backend: DataFrameBackend | None = None,
        dataframe_type: Literal["polars_dataframe"],
    ) -> pl.DataFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    @overload
    def to_dataframe(
        self,
        *,
        backend: DataFrameBackend | None = None,
        dataframe_type: Literal["polars_lazyframe"],
    ) -> pl.LazyFrame:  # numpydoc ignore=GL08
        ...  # pragma: no cover
    def to_dataframe(
        self,
        *,
        backend: DataFrameBackend | None = None,
        dataframe_type: DataFrameType | None = None,
    ) -> NativeDataFrame:
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
        NativeDataFrame
            A native dataframe with columns ``file_name``, ``field_name``,
            ``dtype``, ``definition``, ``period_start``, and ``period_end``.

        Examples
        --------
        >>> import narwhals as nw
        >>> from call_report.core import (
        ...     FieldAttributes,
        ...     FieldSchema,
        ...     FileMetadata,
        ...     PeriodRange,
        ... )
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM", dtype=nw.Int64(), definition="", periods=(span,)
        ... )
        >>> metadata = FileMetadata(
        ...     name="RCB", periods=(span,), file_schema=FieldSchema(fields=[field])
        ... )
        >>> frame = metadata.to_dataframe()
        >>> list(frame.columns)
        ['file_name', 'field_name', 'dtype', 'definition', 'period_start', 'period_end']
        """
        fields_frame = nw.from_native(self.file_schema.to_dataframe(backend=backend))
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
        >>> import narwhals as nw
        >>> from call_report.core import (
        ...     FieldAttributes,
        ...     FieldSchema,
        ...     FileMetadata,
        ...     PeriodRange,
        ... )
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM", dtype=nw.Int64(), definition="", periods=(span,)
        ... )
        >>> metadata = FileMetadata(
        ...     name="RCB", periods=(span,), file_schema=FieldSchema(fields=[field])
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
            file_schema=FieldSchema.from_dataframe(data=field_frame),
        )
