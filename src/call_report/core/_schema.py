"""Source-agnostic, cross-time schema vocabulary: FileMetadata and FieldSchema.

A call report file (schedule) and the fields within it can both change shape
over time: fields get added, dropped, or occasionally retired and later
reintroduced as a source's forms evolve. A field can also be redefined in
place, meaning its dtype or definition changes while the field never
actually disappears (e.g. a field whose embedded code list grows over
time). :class:`FieldVersion` captures one span of a field's history where
its dtype and definition held constant, and :class:`FieldAttributes` is one
or more of them. :class:`FieldAttributes` and :class:`FileMetadata` capture
this history
uniformly across sources, in terms of the periods each field or file is
actually present for, so a single object can answer "what did this look like
across every period" rather than one period at a time.
"""

from __future__ import annotations

import ast
import json
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
    FrameOrLazy,
    build_frame,
    concat,
    convert_dataframe_type,
    finalize,
)
from call_report.core._periods import PeriodRange, ReportingPeriod
from call_report.exceptions import (
    InvalidPeriodError,
    PeriodNotAvailableError,
    SchemaError,
)

if TYPE_CHECKING:
    import pandas
    import polars
    import pyarrow

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
    Parses `text` as an AST rather than calling `eval`, so neither
    arbitrary code nor an arbitrary narwhals API call is reachable,
    whatever `text` contains.

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
class FieldVersion:
    """One span of a field's history where its dtype and definition held constant.

    A `FieldAttributes`'s `versions` is one or more of these: most fields
    have exactly one, meaning their dtype and definition never changed
    across their whole known history. A field whose definition was revised
    in place, without ever disappearing, has one version per revision, with
    adjacent (non-gapped) `periods`.

    Attributes
    ----------
    dtype : narwhals.dtypes.DType
        The field's data type during this version, as an instantiated
        narwhals dtype (e.g. `narwhals.Int64()`, `narwhals.String()`).
        Translating a source's own vocabulary (e.g. FCA's
        ``"Numeric"``/``"Alphanum."``) into a narwhals dtype is that
        source's own responsibility. This class is shared across every
        source, so it only ever deals in narwhals' vocabulary.
    definition : str
        The field's human-readable definition during this version, taken
        from the source's layout file.
    periods : PeriodRange
        The contiguous span this version covers.

    Examples
    --------
    >>> import narwhals as nw
    >>> from call_report.core import FieldVersion, PeriodRange
    >>> version = FieldVersion(
    ...     dtype=nw.Int64(),
    ...     definition="Investment Code: 10 U.S. Treasury securities...",
    ...     periods=PeriodRange(start="2000-03-31", end="2014-12-31"),
    ... )
    >>> version.periods[0].label
    '2000Q1'
    """  # numpydoc ignore=PR01

    dtype: nw.dtypes.DType
    definition: str
    periods: PeriodRange


def _validate_field_versions(versions: tuple[FieldVersion, ...], label: str) -> None:
    """Validate that field versions are non-empty, ordered, and non-overlapping.

    Unlike `_validate_period_spans`, adjacent versions are allowed when
    their `dtype`/`definition` differ, which represents an in-place
    redefinition (e.g. a field's embedded code list growing over time with
    no presence gap). Adjacent versions with identical `dtype`/`definition`
    are still rejected, since those should have been expressed as a single
    version.

    Parameters
    ----------
    versions : tuple[FieldVersion, ...]
        The versions to validate, in the order they were supplied.
    label : str
        A short description of the field being validated, used to make
        the error message actionable.

    Raises
    ------
    SchemaError
        If `versions` is empty, or any two versions are out of order,
        overlapping, or adjacent with identical `dtype`/`definition`.
    """
    if not versions:
        raise SchemaError(f"{label} must have at least one version.")
    previous = versions[0]
    for version in versions[1:]:
        next_expected = previous.periods[-1].next()
        start = version.periods[0]
        if start < next_expected:
            raise SchemaError(
                f"{label} versions must be chronologically ordered and "
                f"non-overlapping; the version starting {start.label} is "
                f"not far enough after the version ending "
                f"{previous.periods[-1].label}."
            )
        if start == next_expected and (version.dtype, version.definition) == (
            previous.dtype,
            previous.definition,
        ):
            raise SchemaError(
                f"{label} has adjacent versions with identical dtype/definition "
                f"spanning {previous.periods[0].label} to {version.periods[-1].label}; "
                "these should be merged into a single version."
            )
        previous = version


def _coalesced_presence(versions: tuple[FieldVersion, ...]) -> tuple[PeriodRange, ...]:
    """Merge a field's versions into presence spans, ignoring content changes.

    Adjacent versions (regardless of whether their `dtype`/`definition`
    differ) are merged into one span. A span only breaks where a real gap
    exists. Used by `FileMetadata.changed` to compare a field's overall
    presence against the file's own `periods`, independent of any
    in-place redefinitions the field underwent while present.

    Parameters
    ----------
    versions : tuple[FieldVersion, ...]
        A field's versions, chronologically ordered (as `FieldAttributes`
        already guarantees).

    Returns
    -------
    tuple[PeriodRange, ...]
        The field's presence spans, merged across content changes.
    """
    merged: list[PeriodRange] = [versions[0].periods]
    for version in versions[1:]:
        if version.periods[0] == merged[-1][-1].next():
            merged[-1] = PeriodRange(start=merged[-1][0], end=version.periods[-1])
        else:
            merged.append(version.periods)
    return tuple(merged)


@dataclass(frozen=True, kw_only=True)
class FieldAttributes:
    """Cross-time metadata for a single field within a call report file.

    Unlike a per-period layout entry, this describes a field across its
    whole known history rather than a single period. A field whose dtype
    or definition changed while it stayed continuously present (e.g. a
    field whose embedded code list grew over time, with no presence gap)
    is represented as multiple `FieldVersion` objects with adjacent
    `periods`, rather than forcing one dtype/definition across the field's
    entire history.

    Attributes
    ----------
    name : str
        The field's name, as it appears in the source's layout files.
    versions : tuple[FieldVersion, ...]
        One or more chronologically ordered, non-overlapping `FieldVersion`
        objects describing this field's dtype/definition over time. More
        than one version means the field's dtype or definition changed at
        some point, was dropped and later reintroduced, or both. Adjacent
        versions always differ in `dtype`/`definition`, since identical
        adjacent content is rejected as something that should have been
        expressed as a single version.

    Raises
    ------
    SchemaError
        If `versions` is empty, or its spans are out of order, overlapping,
        or adjacent with identical `dtype`/`definition`.

    Examples
    --------
    >>> import narwhals as nw
    >>> from call_report.core import FieldAttributes, FieldVersion, PeriodRange
    >>> inv_code = FieldAttributes(
    ...     name="INV_CODE",
    ...     versions=(
    ...         FieldVersion(
    ...             dtype=nw.Int64(),
    ...             definition="Investment Code: 10 U.S. Treasury securities...",
    ...             periods=PeriodRange(start="2000-03-31", end="2014-12-31"),
    ...         ),
    ...         FieldVersion(
    ...             dtype=nw.Int64(),
    ...             definition="Investment Code: 10 U.S. Treasury securities..."
    ...             "15 SBA securities...",
    ...             periods=PeriodRange(start="2015-03-31", end="2026-03-31"),
    ...         ),
    ...     ),
    ... )
    >>> inv_code.first_period.label
    '2000Q1'
    >>> inv_code.last_period.label
    '2026Q1'
    """  # numpydoc ignore=PR01

    name: str
    versions: tuple[FieldVersion, ...]

    def __post_init__(self) -> None:
        """Validate that `versions` forms well-ordered, non-overlapping spans.

        Runs automatically after construction, since this dataclass is
        frozen and cannot be validated any other way.

        Raises
        ------
        SchemaError
            If `versions` is empty, or any two versions are out of order,
            overlapping, or adjacent with identical `dtype`/`definition`.
        """
        _validate_field_versions(self.versions, f"field {self.name!r}")

    @property
    def first_period(self) -> ReportingPeriod:
        """Return the earliest period this field is present in.

        This is the start of this field's earliest version, not
        necessarily the start of the file it belongs to.

        Returns
        -------
        ReportingPeriod
            The first period of this field's earliest version.

        Examples
        --------
        >>> import narwhals as nw
        >>> from call_report.core import FieldAttributes, FieldVersion, PeriodRange
        >>> field = FieldAttributes(
        ...     name="UNINUM",
        ...     versions=(
        ...         FieldVersion(
        ...             dtype=nw.Int64(),
        ...             definition="",
        ...             periods=PeriodRange(start="2000-03-31", end="2005-12-31"),
        ...         ),
        ...     ),
        ... )
        >>> field.first_period.label
        '2000Q1'
        """
        return self.versions[0].periods[0]

    @property
    def last_period(self) -> ReportingPeriod:
        """Return the latest period this field is present in.

        This is the end of this field's latest version, not necessarily
        the end of the file it belongs to.

        Returns
        -------
        ReportingPeriod
            The last period of this field's latest version.

        Examples
        --------
        >>> import narwhals as nw
        >>> from call_report.core import FieldAttributes, FieldVersion, PeriodRange
        >>> field = FieldAttributes(
        ...     name="UNINUM",
        ...     versions=(
        ...         FieldVersion(
        ...             dtype=nw.Int64(),
        ...             definition="",
        ...             periods=PeriodRange(start="2000-03-31", end="2005-12-31"),
        ...         ),
        ...     ),
        ... )
        >>> field.last_period.label
        '2005Q4'
        """
        return self.versions[-1].periods[-1]


class _ReadOnlySchema(nw.Schema):
    """A narwhals Schema that rejects in-place mutation after construction.

    Backs `FieldSchema.schema`. A plain `@property` only stops the
    *attribute* from being reassigned, but `narwhals.Schema` is itself a
    mutable `OrderedDict` subclass, so without this a caller could still
    mutate the stored schema in place (e.g. ``field_schema.schema["X"] =
    ...``). Every dict and `OrderedDict` mutation entry point is overridden
    to raise, so the returned instance is genuinely read-only. Because that
    is enforced by the object's own class rather than by copying on each
    access, `FieldSchema.schema` can return the same stored instance every
    time.

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
        """Raise once frozen, but populate normally during construction.

        `OrderedDict.__init__` populates entries by calling this method, so
        it cannot unconditionally raise. `_frozen` is only set once
        `__init__` finishes, which distinguishes initial population from any
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
        """Raise, since deleting a column would mutate a read-only schema.

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
        """Raise, since clearing would mutate a read-only schema.

        Implements the ``dict.clear`` method.

        Raises
        ------
        TypeError
            Always.
        """
        raise TypeError("FieldSchema.schema is read-only.")

    def pop(self, *args: Any, **kwargs: Any) -> Any:
        """Raise, since popping a column would mutate a read-only schema.

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
        """Raise, since popping a column would mutate a read-only schema.

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
        """Raise, since inserting a default would mutate a read-only schema.

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
        """Raise, since updating would mutate a read-only schema.

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
        """Raise, since reordering would mutate a read-only schema.

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
        """Raise, since merging in place would mutate a read-only schema.

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


@dataclass(frozen=True, kw_only=True)
class FieldChange:
    """One field's before/after metadata between two FieldSchema snapshots.

    Only appears in `FieldSchemaDiff.changed`. A field present in only one
    of the two compared schemas shows up in `FieldSchemaDiff.added` or
    `FieldSchemaDiff.removed` instead, as a plain name.

    Attributes
    ----------
    name : str
        The field name.
    before : FieldAttributes
        This field's metadata in the left-hand (``self``) schema.
    after : FieldAttributes
        This field's metadata in the right-hand (``other``) schema.

    Examples
    --------
    >>> import narwhals as nw
    >>> from call_report.core import (
    ...     FieldAttributes,
    ...     FieldChange,
    ...     FieldVersion,
    ...     PeriodRange,
    ... )
    >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
    >>> old = FieldAttributes(
    ...     name="UNINUM",
    ...     versions=(FieldVersion(dtype=nw.Int64(), definition="old", periods=span),),
    ... )
    >>> new = FieldAttributes(
    ...     name="UNINUM",
    ...     versions=(FieldVersion(dtype=nw.Int64(), definition="new", periods=span),),
    ... )
    >>> change = FieldChange(name="UNINUM", before=old, after=new)
    >>> change.before.versions[0].definition
    'old'
    """  # numpydoc ignore=PR01

    name: str
    before: FieldAttributes
    after: FieldAttributes

    @property
    def dtype_changed(self) -> bool:
        """Return whether the field's dtype differs between `before` and `after`.

        Compares each version's `dtype`, in order, ignoring `definition`
        and `periods`. A field whose version count differs between
        `before` and `after` counts as changed too, since the two
        sequences of dtypes then have different lengths.

        Returns
        -------
        bool
            ``True`` if the sequence of `dtype` values differs between
            `before` and `after`.

        Examples
        --------
        >>> import narwhals as nw
        >>> from call_report.core import (
        ...     FieldAttributes,
        ...     FieldChange,
        ...     FieldVersion,
        ...     PeriodRange,
        ... )
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> old = FieldAttributes(
        ...     name="PROVLNS",
        ...     versions=(FieldVersion(dtype=nw.Int64(), definition="", periods=span),),
        ... )
        >>> new = FieldAttributes(
        ...     name="PROVLNS",
        ...     versions=(
        ...         FieldVersion(dtype=nw.Float64(), definition="", periods=span),
        ...     ),
        ... )
        >>> FieldChange(name="PROVLNS", before=old, after=new).dtype_changed
        True
        """
        return tuple(version.dtype for version in self.before.versions) != tuple(
            version.dtype for version in self.after.versions
        )

    @property
    def definition_changed(self) -> bool:
        """Return whether the field's definition differs between `before` and `after`.

        Compares each version's `definition`, in order, ignoring `dtype`
        and `periods`. A field whose version count differs between
        `before` and `after` counts as changed too, since the two
        sequences of definitions then have different lengths.

        Returns
        -------
        bool
            ``True`` if the sequence of `definition` values differs
            between `before` and `after`.

        Examples
        --------
        >>> import narwhals as nw
        >>> from call_report.core import (
        ...     FieldAttributes,
        ...     FieldChange,
        ...     FieldVersion,
        ...     PeriodRange,
        ... )
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> old = FieldAttributes(
        ...     name="PROVLNS",
        ...     versions=(
        ...         FieldVersion(dtype=nw.Int64(), definition="old", periods=span),
        ...     ),
        ... )
        >>> new = FieldAttributes(
        ...     name="PROVLNS",
        ...     versions=(
        ...         FieldVersion(dtype=nw.Int64(), definition="new", periods=span),
        ...     ),
        ... )
        >>> FieldChange(name="PROVLNS", before=old, after=new).definition_changed
        True
        """
        return tuple(version.definition for version in self.before.versions) != tuple(
            version.definition for version in self.after.versions
        )

    @property
    def periods_changed(self) -> bool:
        """Return whether the field's version spans differ between `before` and `after`.

        Compares each version's `periods`, in order, ignoring `dtype` and
        `definition`. This is the difference that shows up on every field
        common to two schemas taken from different quarters via
        `FieldSchema.as_of`, since narrowing to a quarter stamps each
        surviving field's one remaining version with that quarter alone.

        Returns
        -------
        bool
            ``True`` if the sequence of `periods` values differs between
            `before` and `after`.

        Examples
        --------
        >>> import narwhals as nw
        >>> from call_report.core import (
        ...     FieldAttributes,
        ...     FieldChange,
        ...     FieldVersion,
        ...     PeriodRange,
        ... )
        >>> q4_2014 = PeriodRange(start="2014-12-31", end="2014-12-31")
        >>> q1_2015 = PeriodRange(start="2015-03-31", end="2015-03-31")
        >>> before = FieldAttributes(
        ...     name="UNINUM",
        ...     versions=(
        ...         FieldVersion(dtype=nw.Int64(), definition="", periods=q4_2014),
        ...     ),
        ... )
        >>> after = FieldAttributes(
        ...     name="UNINUM",
        ...     versions=(
        ...         FieldVersion(dtype=nw.Int64(), definition="", periods=q1_2015),
        ...     ),
        ... )
        >>> FieldChange(name="UNINUM", before=before, after=after).periods_changed
        True
        """
        return tuple(version.periods for version in self.before.versions) != tuple(
            version.periods for version in self.after.versions
        )

    @property
    def content_changed(self) -> bool:
        """Return whether the field's dtype or definition differs, ignoring periods.

        Shorthand for ``dtype_changed or definition_changed``. Use this to
        tell a field whose dtype or definition actually moved apart from
        one whose only difference is `periods_changed`, the case
        `FieldSchemaDiff.content_changed` filters `changed` down to.

        Returns
        -------
        bool
            ``True`` if `dtype_changed` or `definition_changed` is
            ``True``.

        Examples
        --------
        >>> import narwhals as nw
        >>> from call_report.core import (
        ...     FieldAttributes,
        ...     FieldChange,
        ...     FieldVersion,
        ...     PeriodRange,
        ... )
        >>> q4_2014 = PeriodRange(start="2014-12-31", end="2014-12-31")
        >>> q1_2015 = PeriodRange(start="2015-03-31", end="2015-03-31")
        >>> before = FieldAttributes(
        ...     name="UNINUM",
        ...     versions=(
        ...         FieldVersion(dtype=nw.Int64(), definition="", periods=q4_2014),
        ...     ),
        ... )
        >>> after = FieldAttributes(
        ...     name="UNINUM",
        ...     versions=(
        ...         FieldVersion(dtype=nw.Int64(), definition="", periods=q1_2015),
        ...     ),
        ... )
        >>> change = FieldChange(name="UNINUM", before=before, after=after)
        >>> change.periods_changed
        True
        >>> change.content_changed
        False
        """
        return self.dtype_changed or self.definition_changed


@dataclass(frozen=True, kw_only=True)
class FieldSchemaDiff:
    """The result of comparing two FieldSchema snapshots via `FieldSchema.compare`.

    `added`/`removed`/`changed` are mutually exclusive: a field appears in
    exactly one of them (or none, if it's identical in both schemas).

    Attributes
    ----------
    added : tuple[str, ...]
        Field names present in the compared-against schema but not this
        one.
    removed : tuple[str, ...]
        Field names present in this schema but not the compared-against
        one.
    changed : tuple[FieldChange, ...]
        Fields present in both schemas, but with different metadata.
    order_changed : bool
        Whether the fields common to both schemas appear in a different
        relative order. Always ``False`` unless `FieldSchema.compare` was
        called with ``check_order=True``.

    Examples
    --------
    >>> diff = FieldSchemaDiff(added=("ASSOC",), removed=(), changed=())
    >>> diff.added
    ('ASSOC',)
    """  # numpydoc ignore=PR01

    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[FieldChange, ...]
    order_changed: bool = False

    @property
    def is_empty(self) -> bool:
        """Return whether the two schemas being compared were identical.

        A convenience for the common "did anything change" check, without
        inspecting `added`/`removed`/`changed` individually.

        Returns
        -------
        bool
            ``True`` if there is nothing in `added`, `removed`, or
            `changed`, and `order_changed` is ``False``.

        Examples
        --------
        >>> FieldSchemaDiff(added=(), removed=(), changed=()).is_empty
        True
        >>> FieldSchemaDiff(added=("ASSOC",), removed=(), changed=()).is_empty
        False
        """
        return not (self.added or self.removed or self.changed or self.order_changed)

    @property
    def content_changed(self) -> tuple[FieldChange, ...]:
        """Return the entries in `changed` whose dtype or definition differs.

        A convenience filter over `changed` for comparing schemas across
        quarters, where a field is commonly stamped with a different
        `periods` (see `FieldChange.periods_changed`) purely because it
        was narrowed by `FieldSchema.as_of`, and that difference carries
        no signal on its own. `changed` still reports every such field, so
        this property is the way to ask what changed beyond that.

        Returns
        -------
        tuple[FieldChange, ...]
            The subset of `changed` where `FieldChange.content_changed` is
            ``True``.

        Examples
        --------
        >>> import narwhals as nw
        >>> from call_report.core import (
        ...     FieldAttributes,
        ...     FieldSchema,
        ...     FieldVersion,
        ...     PeriodRange,
        ... )
        >>> def _field(
        ...     name: str, definition: str, periods: PeriodRange
        ... ) -> FieldAttributes:
        ...     return FieldAttributes(
        ...         name=name,
        ...         versions=(
        ...             FieldVersion(
        ...                 dtype=nw.Int64(), definition=definition, periods=periods
        ...             ),
        ...         ),
        ...     )
        >>> q4_2014 = PeriodRange(start="2014-12-31", end="2014-12-31")
        >>> q1_2015 = PeriodRange(start="2015-03-31", end="2015-03-31")
        >>> before = FieldSchema(
        ...     fields=[
        ...         _field("PROVLNS", "old definition", q4_2014),
        ...         _field("UNINUM", "", q4_2014),
        ...     ]
        ... )
        >>> after = FieldSchema(
        ...     fields=[
        ...         _field("PROVLNS", "new definition", q1_2015),
        ...         _field("UNINUM", "", q1_2015),
        ...     ]
        ... )
        >>> diff = before.compare(other=after)
        >>> len(diff.changed)
        2
        >>> [change.name for change in diff.content_changed]
        ['PROVLNS']
        """
        return tuple(change for change in self.changed if change.content_changed)


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
    >>> from call_report.core import (
    ...     FieldAttributes,
    ...     FieldSchema,
    ...     FieldVersion,
    ...     PeriodRange,
    ... )
    >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
    >>> uninum = FieldAttributes(
    ...     name="UNINUM",
    ...     versions=(FieldVersion(dtype=nw.Int64(), definition="", periods=span),),
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
        self._schema = _ReadOnlySchema(
            (field.name, field.versions[-1].dtype) for field in ordered
        )

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
        >>> from call_report.core import (
        ...     FieldAttributes,
        ...     FieldSchema,
        ...     FieldVersion,
        ...     PeriodRange,
        ... )
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM",
        ...     versions=(FieldVersion(dtype=nw.Int64(), definition="", periods=span),),
        ... )
        >>> FieldSchema(fields=[field]).names
        ('UNINUM',)
        """
        return self._order

    @property
    def schema(self) -> nw.Schema:
        """Return this schema's fields as a narwhals Schema.

        Built once at construction from each field's `name` and its
        *latest* version's `dtype`. `subset`, `add_fields`, `as_of`, and
        `from_dataframe` construct their result via
        `FieldSchema(fields=...)`, which runs this same logic, so they all
        get an up to date narwhals Schema for free. The returned object is
        read-only: it rejects in-place mutation such as item assignment, so
        it cannot be used to change this schema's fields.

        Returns
        -------
        narwhals.Schema
            An ordered mapping of field name to narwhals dtype.

        Examples
        --------
        >>> import narwhals as nw
        >>> from call_report.core import (
        ...     FieldAttributes,
        ...     FieldSchema,
        ...     FieldVersion,
        ...     PeriodRange,
        ... )
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM",
        ...     versions=(FieldVersion(dtype=nw.Int64(), definition="", periods=span),),
        ... )
        >>> FieldSchema(fields=[field]).schema
        Schema({'UNINUM': Int64})
        """
        return self._schema

    def subset(self, *, names: Iterable[str]) -> FieldSchema:
        """Return a new FieldSchema containing only the named fields.

        The result preserves this schema's existing field order. It does
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
        >>> from call_report.core import (
        ...     FieldAttributes,
        ...     FieldSchema,
        ...     FieldVersion,
        ...     PeriodRange,
        ... )
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> uninum = FieldAttributes(
        ...     name="UNINUM",
        ...     versions=(FieldVersion(dtype=nw.Int64(), definition="", periods=span),),
        ... )
        >>> rssd = FieldAttributes(
        ...     name="RSSD",
        ...     versions=(FieldVersion(dtype=nw.Int64(), definition="", periods=span),),
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

        This schema is left unmodified. The caller receives a new instance.

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
        >>> from call_report.core import (
        ...     FieldAttributes,
        ...     FieldSchema,
        ...     FieldVersion,
        ...     PeriodRange,
        ... )
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> uninum = FieldAttributes(
        ...     name="UNINUM",
        ...     versions=(FieldVersion(dtype=nw.Int64(), definition="", periods=span),),
        ... )
        >>> rssd = FieldAttributes(
        ...     name="RSSD",
        ...     versions=(FieldVersion(dtype=nw.Int64(), definition="", periods=span),),
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

        Each surviving field is narrowed to a single version covering the
        quarter `period`, using whichever of that field's versions was
        actually active at that date rather than its most recent one. That
        is what makes the snapshot historically accurate: a field whose
        definition was later revised still shows the definition that
        applied at `period`, not today's.

        Parameters
        ----------
        period : str, datetime.date, or ReportingPeriod
            The quarter-end to take the snapshot at.

        Returns
        -------
        FieldSchema
            A new schema containing only fields present at `period`, each
            narrowed to a single version.

        Examples
        --------
        >>> import narwhals as nw
        >>> from call_report.core import (
        ...     FieldAttributes,
        ...     FieldSchema,
        ...     FieldVersion,
        ...     PeriodRange,
        ... )
        >>> field = FieldAttributes(
        ...     name="INV_CODE",
        ...     versions=(
        ...         FieldVersion(
        ...             dtype=nw.Int64(),
        ...             definition="old text",
        ...             periods=PeriodRange(start="2000-03-31", end="2014-12-31"),
        ...         ),
        ...         FieldVersion(
        ...             dtype=nw.Int64(),
        ...             definition="new text",
        ...             periods=PeriodRange(start="2015-03-31", end="2026-03-31"),
        ...         ),
        ...     ),
        ... )
        >>> schema = FieldSchema(fields=[field])
        >>> schema.as_of(period="2010-03-31")["INV_CODE"].versions[0].definition
        'old text'
        >>> schema.as_of(period="2020-03-31")["INV_CODE"].versions[0].definition
        'new text'
        """
        resolved = _coerce_period(period)
        snapshot_span = PeriodRange(start=resolved, end=resolved)
        fields: list[FieldAttributes] = []
        for field in self._ordered_fields:
            active = next(
                (version for version in field.versions if resolved in version.periods),
                None,
            )
            if active is None:
                continue
            fields.append(
                FieldAttributes(
                    name=field.name,
                    versions=(
                        FieldVersion(
                            dtype=active.dtype,
                            definition=active.definition,
                            periods=snapshot_span,
                        ),
                    ),
                )
            )
        return FieldSchema(fields=fields)

    def is_equal(self, *, other: FieldSchema, check_order: bool = False) -> bool:
        """Return whether `other` defines the same fields and metadata.

        Content comparison is always order-insensitive (inherited from
        `Mapping.__eq__`): two schemas with the same fields in a different
        order compare equal by default. Pass `check_order=True` to
        additionally require the fields appear in the same sequence.

        Parameters
        ----------
        other : FieldSchema
            The schema to compare against.
        check_order : bool, default False
            If ``True``, also require identical field order.

        Returns
        -------
        bool
            ``True`` if `other` has the same fields (and, if `check_order`
            is ``True``, the same field order).

        Examples
        --------
        >>> import narwhals as nw
        >>> from call_report.core import (
        ...     FieldAttributes,
        ...     FieldSchema,
        ...     FieldVersion,
        ...     PeriodRange,
        ... )
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> uninum = FieldAttributes(
        ...     name="UNINUM",
        ...     versions=(FieldVersion(dtype=nw.Int64(), definition="", periods=span),),
        ... )
        >>> rssd = FieldAttributes(
        ...     name="RSSD",
        ...     versions=(FieldVersion(dtype=nw.Int64(), definition="", periods=span),),
        ... )
        >>> forward = FieldSchema(fields=[uninum, rssd])
        >>> reordered = FieldSchema(fields=[rssd, uninum])
        >>> forward.is_equal(other=reordered)
        True
        >>> forward.is_equal(other=reordered, check_order=True)
        False
        """
        if check_order and self.names != other.names:
            return False
        return self == other

    def compare(
        self, *, other: FieldSchema, check_order: bool = False
    ) -> FieldSchemaDiff:
        """Compare this schema against `other`, field by field.

        Unlike `is_equal`, this returns the actual differences rather than
        a single bool, which is useful for reviewing what changed between
        two point-in-time snapshots or auditing a metadata regeneration
        run.

        Parameters
        ----------
        other : FieldSchema
            The schema to compare against.
        check_order : bool, default False
            If ``True``, also detect whether the fields common to both
            schemas appear in a different relative order.

        Returns
        -------
        FieldSchemaDiff
            The fields added, removed, and changed between the two
            schemas.

        Examples
        --------
        >>> import narwhals as nw
        >>> from call_report.core import (
        ...     FieldAttributes,
        ...     FieldSchema,
        ...     FieldVersion,
        ...     PeriodRange,
        ... )
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> def _field(name: str, definition: str) -> FieldAttributes:
        ...     return FieldAttributes(
        ...         name=name,
        ...         versions=(
        ...             FieldVersion(
        ...                 dtype=nw.Int64(), definition=definition, periods=span
        ...             ),
        ...         ),
        ...     )
        >>> before = FieldSchema(fields=[_field("UNINUM", "old"), _field("RSSD", "")])
        >>> after = FieldSchema(fields=[_field("UNINUM", "new"), _field("ASSOC", "")])
        >>> diff = before.compare(other=after)
        >>> diff.added
        ('ASSOC',)
        >>> diff.removed
        ('RSSD',)
        >>> [change.name for change in diff.changed]
        ['UNINUM']
        >>> diff.is_empty
        False
        """
        added = tuple(name for name in other.names if name not in self._by_name)
        removed = tuple(name for name in self.names if name not in other._by_name)
        changed = tuple(
            FieldChange(name=name, before=self[name], after=other[name])
            for name in self.names
            if name in other._by_name and self[name] != other[name]
        )
        common_here = tuple(name for name in self.names if name in other._by_name)
        common_there = tuple(name for name in other.names if name in self._by_name)
        order_changed = check_order and common_here != common_there
        return FieldSchemaDiff(
            added=added, removed=removed, changed=changed, order_changed=order_changed
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
        """Return this schema as a native dataframe, one row per field version.

        A field with more than one version (redefined in place without a
        presence gap, dropped and later reintroduced, or both) contributes
        one row per version, each with that version's own `dtype` and
        `definition`.

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
            A native dataframe with columns ``field_name``, ``dtype``,
            ``definition``, ``period_start``, and ``period_end`` (the last
            two as ISO ``YYYY-MM-DD`` strings). ``dtype`` holds each
            version's narwhals dtype as its `repr` (e.g. ``"Int64"``,
            ``"Datetime(time_unit='us', time_zone='UTC')"``). Storing it
            as a plain string lets it round-trip through every backend,
            including pyarrow, which cannot hold arbitrary Python objects
            as column values.

        Examples
        --------
        >>> import narwhals as nw
        >>> from call_report.core import (
        ...     FieldAttributes,
        ...     FieldSchema,
        ...     FieldVersion,
        ...     PeriodRange,
        ... )
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM",
        ...     versions=(FieldVersion(dtype=nw.Int64(), definition="", periods=span),),
        ... )
        >>> frame = FieldSchema(fields=[field]).to_dataframe()
        >>> list(frame.columns)
        ['field_name', 'dtype', 'definition', 'period_start', 'period_end']
        """
        columns: dict[str, list[Any]] = {column: [] for column in _FIELD_COLUMNS}
        for field in self._ordered_fields:
            for version in field.versions:
                columns["field_name"].append(field.name)
                columns["dtype"].append(repr(version.dtype))
                columns["definition"].append(version.definition)
                columns["period_start"].append(
                    version.periods[0].period_end.isoformat()
                )
                columns["period_end"].append(version.periods[-1].period_end.isoformat())
        with _backend_context(backend):
            native = finalize(frame=build_frame(data=columns))
        return convert_dataframe_type(data=native, dataframe_type=dataframe_type)

    @classmethod
    def from_dataframe(cls, *, data: Any) -> FieldSchema:
        """Reconstruct a FieldSchema from a dataframe built by `to_dataframe`.

        Rows are grouped by ``field_name``, so a field is reconstructed
        with all of its versions even if `data` has one row per version.

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
        >>> from call_report.core import (
        ...     FieldAttributes,
        ...     FieldSchema,
        ...     FieldVersion,
        ...     PeriodRange,
        ... )
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM",
        ...     versions=(FieldVersion(dtype=nw.Int64(), definition="", periods=span),),
        ... )
        >>> frame = FieldSchema(fields=[field]).to_dataframe()
        >>> FieldSchema.from_dataframe(data=frame).names
        ('UNINUM',)
        """
        order: list[str] = []
        versions: dict[str, list[FieldVersion]] = {}
        for row in _rows(data):
            name = row["field_name"]
            if name not in versions:
                order.append(name)
                versions[name] = []
            versions[name].append(
                FieldVersion(
                    dtype=_dtype_from_repr(row["dtype"]),
                    definition=row["definition"],
                    periods=PeriodRange(
                        start=row["period_start"], end=row["period_end"]
                    ),
                )
            )
        return cls(
            fields=[
                FieldAttributes(name=name, versions=tuple(versions[name]))
                for name in order
            ]
        )

    def to_json(self, *, indent: int | None = 2) -> str:
        """Return this schema as a JSON string.

        The format this package ships canonical schedule metadata in (see
        `call_report.fca.get_fca_file_metadata`). It is used in preference
        to a flat dataframe because a field's versions nest naturally under
        its name.

        Parameters
        ----------
        indent : int, optional
            Passed through to `json.dumps`. The default of ``2`` produces
            human-readable, diffable output, matching the shipped metadata
            files. Pass ``None`` for the most compact representation.

        Returns
        -------
        str
            A JSON object mapping each field name to its ``versions``
            list, field order preserved.

        Examples
        --------
        >>> import narwhals as nw
        >>> from call_report.core import (
        ...     FieldAttributes,
        ...     FieldSchema,
        ...     FieldVersion,
        ...     PeriodRange,
        ... )
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM",
        ...     versions=(FieldVersion(dtype=nw.Int64(), definition="", periods=span),),
        ... )
        >>> schema = FieldSchema(fields=[field])
        >>> FieldSchema.from_json(text=schema.to_json()) == schema
        True
        """
        return json.dumps(_field_schema_to_dict(self), indent=indent)

    @classmethod
    def from_json(cls, *, text: str) -> FieldSchema:
        """Reconstruct a FieldSchema from JSON built by `to_json`.

        The inverse of `to_json`. Round-tripping a schema through both
        reconstructs an equal `FieldSchema`.

        Parameters
        ----------
        text : str
            A JSON string in the shape `to_json` produces.

        Returns
        -------
        FieldSchema
            The reconstructed schema.

        Raises
        ------
        SchemaError
            If `text` is not valid JSON, is valid JSON that isn't a JSON
            object, or doesn't otherwise match the shape `to_json`
            produces (a missing key, a bad dtype repr, or an invalid
            period value).

        Examples
        --------
        >>> import narwhals as nw
        >>> from call_report.core import (
        ...     FieldAttributes,
        ...     FieldSchema,
        ...     FieldVersion,
        ...     PeriodRange,
        ... )
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM",
        ...     versions=(FieldVersion(dtype=nw.Int64(), definition="", periods=span),),
        ... )
        >>> schema = FieldSchema(fields=[field])
        >>> FieldSchema.from_json(text=schema.to_json()).names
        ('UNINUM',)
        """
        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            raise SchemaError(f"{text[:80]!r} is not valid JSON: {error}") from error
        if not isinstance(data, dict):
            raise SchemaError(f"expected a JSON object, got {type(data).__name__}.")
        return _field_schema_from_dict(data)

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
        >>> from call_report.core import (
        ...     FieldAttributes,
        ...     FieldSchema,
        ...     FieldVersion,
        ...     PeriodRange,
        ... )
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM",
        ...     versions=(FieldVersion(dtype=nw.Int64(), definition="", periods=span),),
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
        >>> from call_report.core import (
        ...     FieldAttributes,
        ...     FieldSchema,
        ...     FieldVersion,
        ...     PeriodRange,
        ... )
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM",
        ...     versions=(FieldVersion(dtype=nw.Int64(), definition="", periods=span),),
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
        >>> from call_report.core import (
        ...     FieldAttributes,
        ...     FieldSchema,
        ...     FieldVersion,
        ...     PeriodRange,
        ... )
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM",
        ...     versions=(FieldVersion(dtype=nw.Int64(), definition="", periods=span),),
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
        >>> from call_report.core import (
        ...     FieldAttributes,
        ...     FieldSchema,
        ...     FieldVersion,
        ...     PeriodRange,
        ... )
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM",
        ...     versions=(FieldVersion(dtype=nw.Int64(), definition="", periods=span),),
        ... )
        >>> FieldSchema(fields=[field])
        FieldSchema(names=('UNINUM',))
        """
        return f"FieldSchema(names={self._order!r})"


def _field_schema_to_dict(schema: FieldSchema) -> dict[str, Any]:
    """Convert a FieldSchema into a JSON-serializable nested dict.

    Shared by `FieldSchema.to_json` and `FileMetadata.to_json`, so the
    field-serialization shape is defined exactly once.

    Parameters
    ----------
    schema : FieldSchema
        The schema to convert.

    Returns
    -------
    dict[str, Any]
        A ``{field_name: {"versions": [...]}}`` mapping, field order
        preserved.
    """
    return {
        field.name: {
            "versions": [
                {
                    "dtype": repr(version.dtype),
                    "definition": version.definition,
                    "period_start": version.periods[0].period_end.isoformat(),
                    "period_end": version.periods[-1].period_end.isoformat(),
                }
                for version in field.versions
            ]
        }
        for field in schema.values()
    }


def _field_schema_from_dict(data: Mapping[str, Any]) -> FieldSchema:
    """Reconstruct a FieldSchema from the dict shape `_field_schema_to_dict` produces.

    The inverse of `_field_schema_to_dict`, shared by
    `FieldSchema.from_json` and `FileMetadata.from_json`.

    Parameters
    ----------
    data : Mapping[str, Any]
        A ``{field_name: {"versions": [...]}}`` mapping.

    Returns
    -------
    FieldSchema
        The reconstructed schema.

    Raises
    ------
    SchemaError
        If `data` is malformed: a missing key, a value of the wrong shape,
        a bad dtype repr, or an invalid period value.
    """
    try:
        fields = [
            FieldAttributes(
                name=name,
                versions=tuple(
                    FieldVersion(
                        dtype=_dtype_from_repr(version["dtype"]),
                        definition=version["definition"],
                        periods=PeriodRange(
                            start=version["period_start"], end=version["period_end"]
                        ),
                    )
                    for version in field_data["versions"]
                ),
            )
            for name, field_data in data.items()
        ]
    except (KeyError, TypeError, InvalidPeriodError) as error:
        raise SchemaError(f"malformed field schema JSON: {error}") from error
    return FieldSchema(fields=fields)


@dataclass(frozen=True, kw_only=True)
class FileMetadataDiff:
    """The result of comparing two FileMetadata via `FileMetadata.compare`.

    `name` and `periods` are compared directly. Field-level differences
    are delegated entirely to `FieldSchema.compare` (see
    `file_schema_diff`).

    Attributes
    ----------
    name_changed : bool
        Whether the two files have different `name` values.
    periods_changed : bool
        Whether the two files have different publication `periods`.
    file_schema_diff : FieldSchemaDiff
        The field-level differences, delegated to `FieldSchema.compare`.

    Examples
    --------
    >>> empty = FieldSchemaDiff(added=(), removed=(), changed=())
    >>> diff = FileMetadataDiff(
    ...     name_changed=False, periods_changed=False, file_schema_diff=empty
    ... )
    >>> diff.name_changed
    False
    """  # numpydoc ignore=PR01

    name_changed: bool
    periods_changed: bool
    file_schema_diff: FieldSchemaDiff

    @property
    def is_empty(self) -> bool:
        """Return whether the two files being compared were identical.

        A convenience for the common "did anything change" check, without
        inspecting `name_changed`/`periods_changed`/`file_schema_diff`
        individually.

        Returns
        -------
        bool
            ``True`` if `name`/`periods` matched and `file_schema_diff` is
            empty too.

        Examples
        --------
        >>> empty = FieldSchemaDiff(added=(), removed=(), changed=())
        >>> FileMetadataDiff(
        ...     name_changed=False, periods_changed=False, file_schema_diff=empty
        ... ).is_empty
        True
        >>> FileMetadataDiff(
        ...     name_changed=True, periods_changed=False, file_schema_diff=empty
        ... ).is_empty
        False
        """
        return (
            not self.name_changed
            and not self.periods_changed
            and self.file_schema_diff.is_empty
        )


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
    ...     FieldVersion,
    ...     FileMetadata,
    ...     PeriodRange,
    ... )
    >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
    >>> uninum = FieldAttributes(
    ...     name="UNINUM",
    ...     versions=(FieldVersion(dtype=nw.Int64(), definition="", periods=span),),
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

        A field whose overall presence (its versions' periods, merged
        across any in-place redefinitions) does not exactly match this
        file's `periods` was added after the file's first period, dropped
        before its last, or has a gap the file itself does not have. In
        every one of those cases the file's column schema was not identical
        across its whole history. A field that was purely redefined in
        place, meaning its dtype or definition changed but it was always
        present, does *not* count as `changed` on its own.

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
        ...     FieldVersion,
        ...     FileMetadata,
        ...     PeriodRange,
        ... )
        >>> full = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> partial = PeriodRange(start="2010-03-31", end="2026-03-31")
        >>> added_later = FieldAttributes(
        ...     name="RSSD",
        ...     versions=(
        ...         FieldVersion(dtype=nw.Int64(), definition="", periods=partial),
        ...     ),
        ... )
        >>> schema = FieldSchema(fields=[added_later])
        >>> FileMetadata(name="RCB", periods=(full,), file_schema=schema).changed
        True
        """
        return any(
            _coalesced_presence(field.versions) != self.periods
            for field in self.file_schema.values()
        )

    def as_of(self, *, period: str | date | ReportingPeriod) -> FileMetadata:
        """Return a new FileMetadata snapshot as of `period`.

        Delegates field selection to `FieldSchema.as_of`. The result's own
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
        ...     FieldVersion,
        ...     FileMetadata,
        ...     PeriodRange,
        ... )
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM",
        ...     versions=(FieldVersion(dtype=nw.Int64(), definition="", periods=span),),
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

    def is_equal(self, *, other: FileMetadata, check_order: bool = False) -> bool:
        """Return whether `other` describes the same file: name, periods, and fields.

        Field comparison is delegated entirely to `FieldSchema.is_equal`,
        so this doesn't duplicate that logic.

        Parameters
        ----------
        other : FileMetadata
            The file metadata to compare against.
        check_order : bool, default False
            Passed through to `FieldSchema.is_equal` for the field
            comparison.

        Returns
        -------
        bool
            ``True`` if `name`, `periods`, and `file_schema` (per
            `FieldSchema.is_equal`) all match.

        Examples
        --------
        >>> import narwhals as nw
        >>> from call_report.core import (
        ...     FieldAttributes,
        ...     FieldSchema,
        ...     FieldVersion,
        ...     FileMetadata,
        ...     PeriodRange,
        ... )
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM",
        ...     versions=(FieldVersion(dtype=nw.Int64(), definition="", periods=span),),
        ... )
        >>> a = FileMetadata(
        ...     name="RCB", periods=(span,), file_schema=FieldSchema(fields=[field])
        ... )
        >>> b = FileMetadata(
        ...     name="RCB", periods=(span,), file_schema=FieldSchema(fields=[field])
        ... )
        >>> a.is_equal(other=b)
        True
        """
        return (
            self.name == other.name
            and self.periods == other.periods
            and self.file_schema.is_equal(
                other=other.file_schema, check_order=check_order
            )
        )

    def compare(
        self, *, other: FileMetadata, check_order: bool = False
    ) -> FileMetadataDiff:
        """Compare this file's metadata against `other`.

        `name` and `periods` are compared directly. The field-level
        comparison is delegated entirely to `FieldSchema.compare`.

        Parameters
        ----------
        other : FileMetadata
            The file metadata to compare against.
        check_order : bool, default False
            Passed through to `FieldSchema.compare`.

        Returns
        -------
        FileMetadataDiff
            The name/periods/field-level differences between the two.

        Examples
        --------
        >>> import narwhals as nw
        >>> from call_report.core import (
        ...     FieldAttributes,
        ...     FieldSchema,
        ...     FieldVersion,
        ...     FileMetadata,
        ...     PeriodRange,
        ... )
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> old_field = FieldAttributes(
        ...     name="UNINUM",
        ...     versions=(
        ...         FieldVersion(dtype=nw.Int64(), definition="old", periods=span),
        ...     ),
        ... )
        >>> new_field = FieldAttributes(
        ...     name="UNINUM",
        ...     versions=(
        ...         FieldVersion(dtype=nw.Int64(), definition="new", periods=span),
        ...     ),
        ... )
        >>> before = FileMetadata(
        ...     name="RCB", periods=(span,), file_schema=FieldSchema(fields=[old_field])
        ... )
        >>> after = FileMetadata(
        ...     name="RCB", periods=(span,), file_schema=FieldSchema(fields=[new_field])
        ... )
        >>> diff = before.compare(other=after)
        >>> diff.is_empty
        False
        >>> [change.name for change in diff.file_schema_diff.changed]
        ['UNINUM']
        """
        return FileMetadataDiff(
            name_changed=self.name != other.name,
            periods_changed=self.periods != other.periods,
            file_schema_diff=self.file_schema.compare(
                other=other.file_schema, check_order=check_order
            ),
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
            get back whatever `backend` produced. Set it when the code that
            consumes this result needs a specific type, for example a
            pandas DataFrame while the package is configured to use polars.
            The conversion is zero-copy when the requested type already
            matches.

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
        ...     FieldVersion,
        ...     FileMetadata,
        ...     PeriodRange,
        ... )
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM",
        ...     versions=(FieldVersion(dtype=nw.Int64(), definition="", periods=span),),
        ... )
        >>> metadata = FileMetadata(
        ...     name="RCB", periods=(span,), file_schema=FieldSchema(fields=[field])
        ... )
        >>> frame = metadata.to_dataframe()
        >>> list(frame.columns)
        ['file_name', 'field_name', 'dtype', 'definition', 'period_start', 'period_end']
        """
        fields_frame = nw.from_native(self.file_schema.to_dataframe(backend=backend))
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
            file_frame: FrameOrLazy = build_frame(data=file_rows)
            # fields_frame may already be lazy (lazy=True configured); match it
            # so concat isn't asked to stack an eager frame with a lazy one.
            if isinstance(fields_frame, nw.LazyFrame):
                file_frame = file_frame.lazy()
            combined = concat(frames=[file_frame, fields_frame], how="strict")
            native = finalize(frame=combined)
        return convert_dataframe_type(data=native, dataframe_type=dataframe_type)

    @classmethod
    def from_dataframe(cls, *, data: Any) -> FileMetadata:
        """Reconstruct a FileMetadata from a dataframe built by `to_dataframe`.

        Rows with an empty ``field_name`` are this file's own period
        spans. Every other row is delegated to
        `FieldSchema.from_dataframe`.

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
        ...     FieldVersion,
        ...     FileMetadata,
        ...     PeriodRange,
        ... )
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM",
        ...     versions=(FieldVersion(dtype=nw.Int64(), definition="", periods=span),),
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

    def to_json(self, *, indent: int | None = 2) -> str:
        """Return this file's metadata as a JSON string.

        This is the format shipped, canonical FCA schedule metadata is
        stored in (see `call_report.fca.get_fca_file_metadata`).

        Parameters
        ----------
        indent : int, optional
            Passed through to `json.dumps`. See `FieldSchema.to_json`.

        Returns
        -------
        str
            A JSON object with ``name``, ``periods``, and ``fields``
            keys. ``fields`` uses the same shape `FieldSchema.to_json`
            produces.

        Examples
        --------
        >>> import narwhals as nw
        >>> from call_report.core import (
        ...     FieldAttributes,
        ...     FieldSchema,
        ...     FieldVersion,
        ...     FileMetadata,
        ...     PeriodRange,
        ... )
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM",
        ...     versions=(FieldVersion(dtype=nw.Int64(), definition="", periods=span),),
        ... )
        >>> metadata = FileMetadata(
        ...     name="RCB", periods=(span,), file_schema=FieldSchema(fields=[field])
        ... )
        >>> FileMetadata.from_json(text=metadata.to_json()) == metadata
        True
        """
        payload = {
            "name": self.name,
            "periods": [
                {
                    "start": span[0].period_end.isoformat(),
                    "end": span[-1].period_end.isoformat(),
                }
                for span in self.periods
            ],
            "fields": _field_schema_to_dict(self.file_schema),
        }
        return json.dumps(payload, indent=indent)

    @classmethod
    def from_json(cls, *, text: str) -> FileMetadata:
        """Reconstruct a FileMetadata from JSON built by `to_json`.

        The inverse of `to_json`. Round-tripping metadata through both
        reconstructs an equal `FileMetadata`.

        Parameters
        ----------
        text : str
            A JSON string in the shape `to_json` produces.

        Returns
        -------
        FileMetadata
            The reconstructed file metadata.

        Raises
        ------
        SchemaError
            If `text` is not valid JSON, is valid JSON that isn't a JSON
            object, or doesn't otherwise match the shape `to_json`
            produces.

        Examples
        --------
        >>> import narwhals as nw
        >>> from call_report.core import (
        ...     FieldAttributes,
        ...     FieldSchema,
        ...     FieldVersion,
        ...     FileMetadata,
        ...     PeriodRange,
        ... )
        >>> span = PeriodRange(start="2000-03-31", end="2026-03-31")
        >>> field = FieldAttributes(
        ...     name="UNINUM",
        ...     versions=(FieldVersion(dtype=nw.Int64(), definition="", periods=span),),
        ... )
        >>> metadata = FileMetadata(
        ...     name="RCB", periods=(span,), file_schema=FieldSchema(fields=[field])
        ... )
        >>> FileMetadata.from_json(text=metadata.to_json()).name
        'RCB'
        """
        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            raise SchemaError(f"{text[:80]!r} is not valid JSON: {error}") from error
        if not isinstance(data, dict):
            raise SchemaError(f"expected a JSON object, got {type(data).__name__}.")
        try:
            name = data["name"]
            periods = tuple(
                PeriodRange(start=span["start"], end=span["end"])
                for span in data["periods"]
            )
            fields_data = data["fields"]
        except (KeyError, TypeError, InvalidPeriodError) as error:
            raise SchemaError(f"malformed file metadata JSON: {error}") from error
        return cls(
            name=name,
            periods=periods,
            file_schema=_field_schema_from_dict(fields_data),
        )
