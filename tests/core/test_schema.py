"""Tests for the cross-time schema vocabulary (call_report.core)."""

from __future__ import annotations

import dataclasses
from typing import Any

import narwhals as nw
import pandas as pd
import polars as pl
import pyarrow as pa
import pytest

from call_report.config import DataFrameBackend, config_context
from call_report.core import (
    FieldAttributes,
    FieldChange,
    FieldSchema,
    FieldSchemaDiff,
    FieldVersion,
    FileMetadata,
    FileMetadataDiff,
    PeriodRange,
    ReportingPeriod,
)
from call_report.core._backend import DataFrameType
from call_report.exceptions import PeriodNotAvailableError, SchemaError

FULL_SPAN = PeriodRange(start="2000-03-31", end="2026-03-31")
EARLY_SPAN = PeriodRange(start="2000-03-31", end="2005-12-31")
LATE_SPAN = PeriodRange(start="2010-03-31", end="2026-03-31")
ADJACENT_SPAN = PeriodRange(start="2006-03-31", end="2009-12-31")
_DEFAULT_DTYPE = nw.Int64()


def _field(
    name: str = "UNINUM",
    *,
    dtype: nw.dtypes.DType = _DEFAULT_DTYPE,
    definition: str = "",
    periods: PeriodRange | tuple[PeriodRange, ...] = FULL_SPAN,
) -> FieldAttributes:
    """Build a FieldAttributes whose version(s) all share `dtype`/`definition`.

    `periods` may be a single `PeriodRange` (the common case, one version)
    or a tuple of spans (one version per span, e.g. to test a field with a
    real presence gap) -- every version built this way shares the same
    `dtype`/`definition`. Tests that need versions with *different*
    content per span construct `FieldAttributes`/`FieldVersion` directly.
    """
    spans = periods if isinstance(periods, tuple) else (periods,)
    return FieldAttributes(
        name=name,
        versions=tuple(
            FieldVersion(dtype=dtype, definition=definition, periods=span)
            for span in spans
        ),
    )


# ---------------------------------------------------------------------------
# _validate_period_spans (exercised through FieldAttributes and FileMetadata)
# ---------------------------------------------------------------------------


def test_field_attributes_rejects_empty_versions() -> None:
    """An empty `versions` tuple is rejected."""
    with pytest.raises(SchemaError, match="at least one version"):
        _field(periods=())


def test_field_attributes_accepts_multiple_gapped_spans() -> None:
    """Two spans with a real gap between them are accepted."""
    field = _field(periods=(EARLY_SPAN, LATE_SPAN))
    assert field.first_period.label == "2000Q1"
    assert field.last_period.label == "2026Q1"


@pytest.mark.parametrize(
    "second_span",
    [
        PeriodRange(start="2003-03-31", end="2009-12-31"),  # overlaps EARLY_SPAN
        PeriodRange(start="1999-03-31", end="1999-12-31"),  # out of order
    ],
    ids=["overlapping", "out-of-order"],
)
def test_field_attributes_rejects_overlapping_or_unordered_versions(
    second_span: PeriodRange,
) -> None:
    """Overlapping or out-of-order versions are rejected."""
    with pytest.raises(SchemaError, match="chronologically ordered"):
        _field(periods=(EARLY_SPAN, second_span))


def test_field_attributes_rejects_adjacent_versions_with_identical_content() -> None:
    """Adjacent versions sharing the same dtype/definition should be one version.

    `_field()` gives every span the same dtype/definition, so two adjacent
    (gap-free) spans built through it are exactly this case.
    """
    with pytest.raises(SchemaError, match="identical dtype/definition"):
        _field(periods=(EARLY_SPAN, ADJACENT_SPAN))


def test_field_attributes_accepts_adjacent_versions_with_different_definition() -> None:
    """Adjacent versions ARE allowed when their definition differs.

    This is the real-world case this whole model exists for: a field
    redefined in place (e.g. RCB's INV_CODE growing its embedded code
    list) with no presence gap.
    """
    field = FieldAttributes(
        name="INV_CODE",
        versions=(
            FieldVersion(dtype=nw.Int64(), definition="old", periods=EARLY_SPAN),
            FieldVersion(dtype=nw.Int64(), definition="new", periods=ADJACENT_SPAN),
        ),
    )
    assert len(field.versions) == 2
    assert field.first_period.label == "2000Q1"
    assert field.last_period.label == "2009Q4"


def test_field_attributes_accepts_adjacent_versions_with_different_dtype() -> None:
    """Adjacent versions are also allowed when only the dtype differs."""
    field = FieldAttributes(
        name="X",
        versions=(
            FieldVersion(dtype=nw.Int64(), definition="", periods=EARLY_SPAN),
            FieldVersion(dtype=nw.Float64(), definition="", periods=ADJACENT_SPAN),
        ),
    )
    assert len(field.versions) == 2


def test_field_attributes_is_frozen() -> None:
    """FieldAttributes instances cannot be mutated after construction."""
    field = _field()
    with pytest.raises(dataclasses.FrozenInstanceError):
        field.name = "OTHER"  # type: ignore[misc]


def test_field_attributes_is_keyword_only() -> None:
    """The FieldAttributes constructor takes no positional args."""
    version = FieldVersion(dtype=nw.Int64(), definition="", periods=FULL_SPAN)
    with pytest.raises(TypeError):
        FieldAttributes("UNINUM", (version,))  # type: ignore[call-arg]


def test_field_version_is_frozen() -> None:
    """FieldVersion instances cannot be mutated after construction."""
    version = FieldVersion(dtype=nw.Int64(), definition="", periods=FULL_SPAN)
    with pytest.raises(dataclasses.FrozenInstanceError):
        version.definition = "OTHER"  # type: ignore[misc]


def test_field_version_is_keyword_only() -> None:
    """The FieldVersion constructor takes no positional args."""
    with pytest.raises(TypeError):
        FieldVersion(nw.Int64(), "", FULL_SPAN)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# FieldSchema
# ---------------------------------------------------------------------------


def test_field_schema_preserves_definition_order() -> None:
    """Iteration order matches the order fields were supplied in."""
    uninum = _field("UNINUM")
    rssd = _field("RSSD")
    schema = FieldSchema(fields=[uninum, rssd])
    assert schema.names == ("UNINUM", "RSSD")
    assert list(schema) == ["UNINUM", "RSSD"]
    assert len(schema) == 2


def test_field_schema_rejects_duplicate_names() -> None:
    """Two fields with the same name cannot share a FieldSchema."""
    with pytest.raises(SchemaError, match=r"duplicate field name\(s\).*UNINUM"):
        FieldSchema(fields=[_field("UNINUM"), _field("UNINUM")])


def test_field_schema_getitem() -> None:
    """Looking up a field by name returns the matching FieldAttributes."""
    uninum = _field("UNINUM")
    schema = FieldSchema(fields=[uninum])
    assert schema["UNINUM"] is uninum


def test_field_schema_getitem_missing_raises_key_error() -> None:
    """Looking up an absent field name raises KeyError."""
    schema = FieldSchema(fields=[_field("UNINUM")])
    with pytest.raises(KeyError):
        schema["RSSD"]


def test_field_schema_contains() -> None:
    """The `in` operator reflects field membership."""
    schema = FieldSchema(fields=[_field("UNINUM")])
    assert "UNINUM" in schema
    assert "RSSD" not in schema


def test_field_schema_repr() -> None:
    """repr() shows the schema's field names."""
    schema = FieldSchema(fields=[_field("UNINUM")])
    assert repr(schema) == "FieldSchema(names=('UNINUM',))"


def test_field_schema_subset_preserves_schema_order() -> None:
    """subset() keeps the schema's own order, not the order of `names`."""
    uninum = _field("UNINUM")
    rssd = _field("RSSD")
    schema = FieldSchema(fields=[uninum, rssd])
    assert schema.subset(names=["RSSD", "UNINUM"]).names == ("UNINUM", "RSSD")


def test_field_schema_subset_rejects_unknown_name() -> None:
    """subset() rejects any name that isn't a field in the schema."""
    schema = FieldSchema(fields=[_field("UNINUM")])
    with pytest.raises(SchemaError, match=r"unknown field name\(s\).*RSSD"):
        schema.subset(names=["RSSD"])


def test_field_schema_add_fields_appends_by_default() -> None:
    """Without an index, add_fields appends at the end."""
    schema = FieldSchema(fields=[_field("UNINUM")])
    updated = schema.add_fields(fields=_field("RSSD"))
    assert updated.names == ("UNINUM", "RSSD")


def test_field_schema_add_fields_accepts_an_iterable() -> None:
    """add_fields accepts either a single field or an iterable of fields."""
    schema = FieldSchema(fields=[_field("UNINUM")])
    updated = schema.add_fields(fields=[_field("RSSD"), _field("FDIC_CERT")])
    assert updated.names == ("UNINUM", "RSSD", "FDIC_CERT")


def test_field_schema_add_fields_inserts_at_index() -> None:
    """A supplied index inserts the new field(s) at that position."""
    schema = FieldSchema(fields=[_field("UNINUM"), _field("RSSD")])
    updated = schema.add_fields(fields=_field("FDIC_CERT"), index=1)
    assert updated.names == ("UNINUM", "FDIC_CERT", "RSSD")


def test_field_schema_add_fields_at_index_zero() -> None:
    """Index 0 inserts before every existing field."""
    schema = FieldSchema(fields=[_field("UNINUM")])
    updated = schema.add_fields(fields=_field("RSSD"), index=0)
    assert updated.names == ("RSSD", "UNINUM")


def test_field_schema_add_fields_does_not_mutate_original() -> None:
    """add_fields returns a new FieldSchema; the original is untouched."""
    schema = FieldSchema(fields=[_field("UNINUM")])
    schema.add_fields(fields=_field("RSSD"))
    assert schema.names == ("UNINUM",)


def test_field_schema_add_fields_rejects_empty_input() -> None:
    """Supplying no fields to add is rejected."""
    schema = FieldSchema(fields=[_field("UNINUM")])
    with pytest.raises(SchemaError, match="at least one field"):
        schema.add_fields(fields=[])


@pytest.mark.parametrize("index", [-1, 2])
def test_field_schema_add_fields_rejects_out_of_range_index(index: int) -> None:
    """An index outside [0, len(schema)] is rejected."""
    schema = FieldSchema(fields=[_field("UNINUM")])
    with pytest.raises(SchemaError, match="out of range"):
        schema.add_fields(fields=_field("RSSD"), index=index)


def test_field_schema_add_fields_rejects_name_collision() -> None:
    """Adding a field whose name already exists is rejected."""
    schema = FieldSchema(fields=[_field("UNINUM")])
    with pytest.raises(SchemaError, match=r"duplicate field name\(s\)"):
        schema.add_fields(fields=_field("UNINUM"))


# ---------------------------------------------------------------------------
# FieldSchema.schema
# ---------------------------------------------------------------------------


def test_field_schema_schema_maps_names_to_dtypes() -> None:
    """Schema is an ordered name-to-dtype mapping matching the fields."""
    uninum = _field("UNINUM", dtype=nw.Int64())
    name_field = _field("SHORTNAME", dtype=nw.String())
    schema = FieldSchema(fields=[uninum, name_field])
    assert schema.schema == nw.Schema({"UNINUM": nw.Int64(), "SHORTNAME": nw.String()})
    assert list(schema.schema.names()) == ["UNINUM", "SHORTNAME"]


def test_field_schema_schema_reflects_latest_version_dtype() -> None:
    """Schema uses a field's *latest* version dtype when it changed over time."""
    field = FieldAttributes(
        name="X",
        versions=(
            FieldVersion(dtype=nw.Int64(), definition="", periods=EARLY_SPAN),
            FieldVersion(dtype=nw.Float64(), definition="", periods=ADJACENT_SPAN),
        ),
    )
    schema = FieldSchema(fields=[field])
    assert schema.schema == nw.Schema({"X": nw.Float64()})


def test_field_schema_schema_is_a_narwhals_schema() -> None:
    """Schema is a genuine narwhals.Schema, not just duck-typed."""
    schema = FieldSchema(fields=[_field("UNINUM")])
    assert isinstance(schema.schema, nw.Schema)


def test_field_schema_schema_repr_hides_the_internal_wrapper_class() -> None:
    """repr() looks like plain narwhals.Schema, not the private subclass."""
    schema = FieldSchema(fields=[_field("UNINUM")])
    assert repr(schema.schema) == "Schema({'UNINUM': Int64})"


def test_field_schema_schema_is_the_same_object_each_access() -> None:
    """Schema returns the same stored instance, not a fresh copy."""
    schema = FieldSchema(fields=[_field("UNINUM")])
    assert schema.schema is schema.schema


def test_field_schema_schema_cannot_be_reassigned() -> None:
    """The schema property has no setter."""
    schema = FieldSchema(fields=[_field("UNINUM")])
    with pytest.raises(AttributeError):
        schema.schema = nw.Schema()  # type: ignore[misc]


@pytest.mark.parametrize(
    ("method_name", "args"),
    [
        ("__setitem__", ("X", nw.Boolean())),
        ("__delitem__", ("UNINUM",)),
        ("clear", ()),
        ("pop", ("UNINUM",)),
        ("popitem", ()),
        ("setdefault", ("X", nw.Boolean())),
        ("update", ({"X": nw.Boolean()},)),
        ("move_to_end", ("UNINUM",)),
        ("__ior__", ({"X": nw.Boolean()},)),
    ],
)
def test_field_schema_schema_is_read_only(
    method_name: str, args: tuple[Any, ...]
) -> None:
    """Every dict/OrderedDict mutation entry point on schema raises."""
    schema = FieldSchema(fields=[_field("UNINUM")])
    method = getattr(schema.schema, method_name)
    with pytest.raises(TypeError, match="read-only"):
        method(*args)


def test_field_schema_schema_comes_free_through_subset() -> None:
    """subset()'s result has a matching, independently-correct schema."""
    uninum = _field("UNINUM", dtype=nw.Int64())
    rssd = _field("RSSD", dtype=nw.String())
    schema = FieldSchema(fields=[uninum, rssd]).subset(names=["RSSD"])
    assert schema.schema == nw.Schema({"RSSD": nw.String()})


def test_field_schema_schema_comes_free_through_add_fields() -> None:
    """add_fields()'s result has a matching, independently-correct schema."""
    schema = FieldSchema(fields=[_field("UNINUM", dtype=nw.Int64())]).add_fields(
        fields=_field("SHORTNAME", dtype=nw.String())
    )
    assert schema.schema == nw.Schema({"UNINUM": nw.Int64(), "SHORTNAME": nw.String()})


def test_field_schema_schema_comes_free_through_as_of() -> None:
    """as_of()'s result has a matching, independently-correct schema."""
    schema = FieldSchema(fields=[_field("UNINUM", dtype=nw.Int64())]).as_of(
        period="2010-03-31"
    )
    assert schema.schema == nw.Schema({"UNINUM": nw.Int64()})


def test_field_schema_schema_comes_free_through_from_dataframe() -> None:
    """from_dataframe()'s result has a matching, independently-correct schema."""
    original = FieldSchema(fields=[_field("UNINUM", dtype=nw.Int64())])
    restored = FieldSchema.from_dataframe(data=original.to_dataframe())
    assert restored.schema == nw.Schema({"UNINUM": nw.Int64()})


# ---------------------------------------------------------------------------
# FileMetadata
# ---------------------------------------------------------------------------


def test_file_metadata_first_and_last_period() -> None:
    """first_period/last_period reflect the outermost span bounds."""
    metadata = FileMetadata(
        name="RCB", periods=(EARLY_SPAN, LATE_SPAN), file_schema=FieldSchema(fields=[])
    )
    assert metadata.first_period.label == "2000Q1"
    assert metadata.last_period.label == "2026Q1"


def test_file_metadata_rejects_invalid_periods() -> None:
    """FileMetadata reuses the same span validation as FieldAttributes."""
    with pytest.raises(SchemaError, match=r"file 'RCB'.*at least one period span"):
        FileMetadata(name="RCB", periods=(), file_schema=FieldSchema(fields=[]))


def test_file_metadata_rejects_adjacent_periods() -> None:
    """Unlike field versions, a file's own periods still forbid adjacency.

    `FileMetadata.periods` describes when the *file* was published, not a
    field's dtype/definition history -- it keeps the strict
    `_validate_period_spans` rule (no gap-free adjacency at all), unlike
    the relaxed `_validate_field_versions` rule fields now get.
    """
    with pytest.raises(SchemaError, match="chronologically ordered"):
        FileMetadata(
            name="RCB",
            periods=(EARLY_SPAN, ADJACENT_SPAN),
            file_schema=FieldSchema(fields=[]),
        )


def test_file_metadata_not_changed_when_fields_match_file_span() -> None:
    """Changed is False when every field covers the file's full lifetime."""
    schema = FieldSchema(fields=[_field("UNINUM", periods=(FULL_SPAN,))])
    metadata = FileMetadata(name="RCB", periods=(FULL_SPAN,), file_schema=schema)
    assert metadata.changed is False


def test_file_metadata_changed_when_a_field_span_differs() -> None:
    """Changed is True when a field's presence differs from the file's own."""
    schema = FieldSchema(fields=[_field("RSSD", periods=(LATE_SPAN,))])
    metadata = FileMetadata(name="RCB", periods=(FULL_SPAN,), file_schema=schema)
    assert metadata.changed is True


def test_file_metadata_not_changed_by_in_place_redefinition() -> None:
    """A field redefined in place (no presence gap) does not count as changed.

    `changed` is about presence, not content: `_coalesced_presence` merges
    the field's two adjacent versions back into one continuous span before
    comparing against the file's own periods, so a pure redefinition
    (dtype/definition changed, field never absent) doesn't trip it.
    """
    field = FieldAttributes(
        name="INV_CODE",
        versions=(
            FieldVersion(dtype=nw.Int64(), definition="old", periods=EARLY_SPAN),
            FieldVersion(
                dtype=nw.Int64(),
                definition="new",
                periods=PeriodRange(start="2006-03-31", end="2026-03-31"),
            ),
        ),
    )
    schema = FieldSchema(fields=[field])
    metadata = FileMetadata(name="RCB", periods=(FULL_SPAN,), file_schema=schema)
    assert metadata.changed is False


def test_file_metadata_changed_true_for_field_with_a_real_gap() -> None:
    """A field with a genuine presence gap (not just redefinition) is changed.

    Distinguishes `_coalesced_presence`'s two branches: an in-place
    redefinition (adjacent versions) merges into one span, but a real gap
    between versions stays two separate spans -- which then differs from
    the file's own single, unbroken span.
    """
    schema = FieldSchema(fields=[_field("RSSD", periods=(EARLY_SPAN, LATE_SPAN))])
    metadata = FileMetadata(name="RCB", periods=(FULL_SPAN,), file_schema=schema)
    assert metadata.changed is True


def test_file_metadata_is_frozen() -> None:
    """FileMetadata instances cannot be mutated after construction."""
    metadata = FileMetadata(
        name="RCB", periods=(FULL_SPAN,), file_schema=FieldSchema(fields=[])
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        metadata.name = "OTHER"  # type: ignore[misc]


def test_file_metadata_is_keyword_only() -> None:
    """The FileMetadata constructor takes no positional args."""
    with pytest.raises(TypeError):
        FileMetadata("RCB", (FULL_SPAN,), FieldSchema(fields=[]))  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# FieldSchema.as_of
# ---------------------------------------------------------------------------


def test_field_schema_as_of_keeps_fields_present_at_the_period() -> None:
    """A field present at `period` survives, narrowed to that one quarter."""
    schema = FieldSchema(fields=[_field("UNINUM")])
    snapshot = schema.as_of(period="2010-03-31")
    assert snapshot.names == ("UNINUM",)
    assert snapshot["UNINUM"].versions == (
        FieldVersion(
            dtype=_DEFAULT_DTYPE,
            definition="",
            periods=PeriodRange(start="2010-03-31", end="2010-03-31"),
        ),
    )


def test_field_schema_as_of_uses_the_version_active_at_that_period() -> None:
    """as_of surfaces the version actually active then, not always the latest.

    This is the point-in-time accuracy the versioned model exists for: a
    field whose definition was later revised should still show the old
    definition for a period before the revision.
    """
    field = FieldAttributes(
        name="INV_CODE",
        versions=(
            FieldVersion(dtype=nw.Int64(), definition="old text", periods=EARLY_SPAN),
            FieldVersion(
                dtype=nw.Int64(), definition="new text", periods=ADJACENT_SPAN
            ),
        ),
    )
    schema = FieldSchema(fields=[field])
    before = schema.as_of(period="2001-03-31")["INV_CODE"]
    after = schema.as_of(period="2007-03-31")["INV_CODE"]
    assert before.versions[0].definition == "old text"
    assert after.versions[0].definition == "new text"


def test_field_schema_as_of_drops_fields_absent_in_a_gap() -> None:
    """A field with a gap is dropped for periods that fall in that gap."""
    schema = FieldSchema(
        fields=[_field("UNINUM"), _field("RSSD", periods=(EARLY_SPAN, LATE_SPAN))]
    )
    # 2007Q1 falls between EARLY_SPAN and LATE_SPAN -- RSSD is absent there.
    snapshot = schema.as_of(period="2007-03-31")
    assert snapshot.names == ("UNINUM",)


def test_field_schema_as_of_accepts_a_reporting_period_directly() -> None:
    """as_of accepts an already-built ReportingPeriod, not just a string."""
    schema = FieldSchema(fields=[_field("UNINUM")])
    period = ReportingPeriod.from_period_end(value="2010-03-31")
    assert schema.as_of(period=period).names == ("UNINUM",)


def test_field_schema_as_of_can_return_an_empty_schema() -> None:
    """A period no field covers yields an empty FieldSchema."""
    schema = FieldSchema(fields=[_field("UNINUM", periods=(EARLY_SPAN,))])
    snapshot = schema.as_of(period="2020-03-31")
    assert len(snapshot) == 0


# ---------------------------------------------------------------------------
# FieldSchema.to_dataframe / from_dataframe
# ---------------------------------------------------------------------------


def test_field_schema_to_dataframe_columns() -> None:
    """to_dataframe produces the documented column set, in order."""
    schema = FieldSchema(fields=[_field("UNINUM")])
    frame = schema.to_dataframe()
    assert list(frame.columns) == [
        "field_name",
        "dtype",
        "definition",
        "period_start",
        "period_end",
    ]


def test_field_schema_to_dataframe_one_row_per_span() -> None:
    """A field with multiple spans contributes one row per span."""
    schema = FieldSchema(fields=[_field("RSSD", periods=(EARLY_SPAN, LATE_SPAN))])
    frame = schema.to_dataframe(dataframe_type="pandas")
    assert len(frame) == 2
    assert list(frame["field_name"]) == ["RSSD", "RSSD"]


def test_field_schema_dataframe_round_trip() -> None:
    """from_dataframe(to_dataframe(schema)) reconstructs an equal schema."""
    schema = FieldSchema(
        fields=[
            _field("UNINUM", periods=(FULL_SPAN,)),
            _field(
                "RSSD",
                dtype=nw.Int64(),
                definition="Fed ID.",
                periods=(EARLY_SPAN, LATE_SPAN),
            ),
        ]
    )
    restored = FieldSchema.from_dataframe(data=schema.to_dataframe())
    assert restored == schema
    assert restored.names == schema.names


def test_field_schema_dataframe_round_trip_when_empty() -> None:
    """An empty FieldSchema round-trips through a zero-row dataframe."""
    schema = FieldSchema(fields=[])
    restored = FieldSchema.from_dataframe(data=schema.to_dataframe())
    assert restored == schema
    assert len(restored) == 0


def test_field_schema_dtype_round_trip_preserves_parametrized_dtype() -> None:
    """A parametrized dtype (e.g. Datetime) round-trips with its exact params."""
    schema = FieldSchema(fields=[_field("ASOFDATE", dtype=nw.Datetime("us", "UTC"))])
    restored = FieldSchema.from_dataframe(data=schema.to_dataframe())
    assert restored["ASOFDATE"].versions[0].dtype == nw.Datetime("us", "UTC")


def test_field_schema_dtype_round_trip_preserves_nested_dtype() -> None:
    """A deeply nested dtype (Struct of List of Int64) round-trips exactly."""
    nested = nw.Struct({"codes": nw.List(nw.Int64())})
    schema = FieldSchema(fields=[_field("PAYLOAD", dtype=nested)])
    restored = FieldSchema.from_dataframe(data=schema.to_dataframe())
    assert restored["PAYLOAD"].versions[0].dtype == nested


def test_field_schema_dtype_round_trip_preserves_enum_dtype() -> None:
    """Enum's list-literal categories round-trip exactly."""
    enum = nw.Enum(("a", "b", "c"))
    schema = FieldSchema(fields=[_field("STATUS", dtype=enum)])
    restored = FieldSchema.from_dataframe(data=schema.to_dataframe())
    assert restored["STATUS"].versions[0].dtype == enum


def test_field_schema_dtype_round_trip_preserves_array_dtype() -> None:
    """Array's tuple-literal shape round-trips exactly."""
    array = nw.Array(nw.Int64(), 3)
    schema = FieldSchema(fields=[_field("COORDS", dtype=array)])
    restored = FieldSchema.from_dataframe(data=schema.to_dataframe())
    assert restored["COORDS"].versions[0].dtype == array


def test_field_schema_to_dataframe_one_row_per_version_with_different_content() -> None:
    """A field redefined in place contributes one row per version, each distinct."""
    field = FieldAttributes(
        name="INV_CODE",
        versions=(
            FieldVersion(dtype=nw.Int64(), definition="old", periods=EARLY_SPAN),
            FieldVersion(dtype=nw.Int64(), definition="new", periods=ADJACENT_SPAN),
        ),
    )
    frame = FieldSchema(fields=[field]).to_dataframe(dataframe_type="pandas")
    assert len(frame) == 2
    assert list(frame["definition"]) == ["old", "new"]


def _dataframe_with_dtype_text(dtype_text: str) -> pd.DataFrame:
    """Build a minimal FieldSchema-shaped dataframe with a given dtype cell."""
    return pd.DataFrame(
        {
            "field_name": ["X"],
            "dtype": [dtype_text],
            "definition": [""],
            "period_start": ["2000-03-31"],
            "period_end": ["2026-03-31"],
        }
    )


@pytest.mark.parametrize(
    "dtype_text",
    [
        "(",
        "1 + 1",
        "from_dict",
        "123",
        "Struct({**{'a': Int64}})",
    ],
    ids=[
        "syntax-error",
        "unsupported-node-kind",
        "narwhals-name-not-a-dtype",
        "valid-syntax-not-a-dtype",
        "dict-unpacking-rejected",
    ],
)
def test_field_schema_from_dataframe_rejects_invalid_dtype_text(
    dtype_text: str,
) -> None:
    """A dtype column that isn't a valid narwhals dtype repr is rejected.

    Also guards against arbitrary-code-execution: `"from_dict"` is a real
    narwhals function, not a dtype, and must be rejected rather than called.
    """
    with pytest.raises(SchemaError, match="not a valid narwhals dtype repr"):
        FieldSchema.from_dataframe(data=_dataframe_with_dtype_text(dtype_text))


_BACKEND_NATIVE_TYPES: dict[str, type] = {
    "pandas": pd.DataFrame,
    "polars": pl.DataFrame,
    "pyarrow": pa.Table,
}
_DATAFRAME_TYPE_NATIVE_TYPES: dict[str, type] = {
    "pandas": pd.DataFrame,
    "pyarrow_table": pa.Table,
    "polars_dataframe": pl.DataFrame,
    "polars_lazyframe": pl.LazyFrame,
}


@pytest.mark.parametrize("backend", ["pandas", "polars", "pyarrow"])
@pytest.mark.parametrize(
    "dataframe_type",
    [None, "pandas", "pyarrow_table", "polars_dataframe", "polars_lazyframe"],
)
def test_field_schema_to_dataframe_backend_and_dataframe_type_matrix(
    backend: DataFrameBackend, dataframe_type: DataFrameType | None
) -> None:
    """Every (backend, dataframe_type) combination returns the right output."""
    schema = FieldSchema(fields=[_field("UNINUM")])
    frame = schema.to_dataframe(backend=backend, dataframe_type=dataframe_type)
    expected_type = (
        _BACKEND_NATIVE_TYPES[backend]
        if dataframe_type is None
        else _DATAFRAME_TYPE_NATIVE_TYPES[dataframe_type]
    )
    assert isinstance(frame, expected_type)
    if isinstance(frame, pl.LazyFrame):
        frame = frame.collect()
    assert FieldSchema.from_dataframe(data=frame) == schema


def test_field_schema_dataframe_round_trip_when_lazy() -> None:
    """A lazy polars frame round-trips (exercises the lazy-collect branch)."""
    schema = FieldSchema(fields=[_field("UNINUM")])
    with config_context(dataframe_backend="polars", lazy=True):
        frame = schema.to_dataframe()
        assert isinstance(frame, pl.LazyFrame)
        restored = FieldSchema.from_dataframe(data=frame)
    assert restored == schema


# ---------------------------------------------------------------------------
# FileMetadata.as_of
# ---------------------------------------------------------------------------


def test_file_metadata_as_of_narrows_periods_and_delegates_to_schema() -> None:
    """as_of narrows both the file's own periods and its schema's fields."""
    schema = FieldSchema(
        fields=[_field("UNINUM"), _field("RSSD", periods=(EARLY_SPAN, LATE_SPAN))]
    )
    metadata = FileMetadata(name="RCB", periods=(FULL_SPAN,), file_schema=schema)
    snapshot = metadata.as_of(period="2007-03-31")
    assert snapshot.name == "RCB"
    assert snapshot.periods == (PeriodRange(start="2007-03-31", end="2007-03-31"),)
    # RSSD has a gap over 2007Q1, so it should have been dropped by FieldSchema.as_of.
    assert snapshot.file_schema.names == ("UNINUM",)


def test_file_metadata_as_of_rejects_unpublished_period() -> None:
    """as_of raises if the file was not published as of `period`."""
    metadata = FileMetadata(
        name="RCB", periods=(FULL_SPAN,), file_schema=FieldSchema(fields=[])
    )
    with pytest.raises(PeriodNotAvailableError, match="RCB"):
        metadata.as_of(period="1990-03-31")


# ---------------------------------------------------------------------------
# FileMetadata.to_dataframe / from_dataframe
# ---------------------------------------------------------------------------


def test_file_metadata_to_dataframe_columns() -> None:
    """to_dataframe produces the documented column set, in order."""
    metadata = FileMetadata(
        name="RCB",
        periods=(FULL_SPAN,),
        file_schema=FieldSchema(fields=[_field("UNINUM")]),
    )
    frame = metadata.to_dataframe()
    assert list(frame.columns) == [
        "file_name",
        "field_name",
        "dtype",
        "definition",
        "period_start",
        "period_end",
    ]


def test_file_metadata_dataframe_round_trip() -> None:
    """from_dataframe(to_dataframe(metadata)) reconstructs an equal instance."""
    schema = FieldSchema(
        fields=[
            _field("UNINUM", periods=(FULL_SPAN,)),
            _field("RSSD", periods=(EARLY_SPAN, LATE_SPAN)),
        ]
    )
    metadata = FileMetadata(
        name="RCB", periods=(EARLY_SPAN, LATE_SPAN), file_schema=schema
    )
    restored = FileMetadata.from_dataframe(data=metadata.to_dataframe())
    assert restored == metadata


def test_file_metadata_dataframe_round_trip_when_schema_empty() -> None:
    """A file with no known fields still round-trips its own periods."""
    metadata = FileMetadata(
        name="RCB", periods=(FULL_SPAN,), file_schema=FieldSchema(fields=[])
    )
    restored = FileMetadata.from_dataframe(data=metadata.to_dataframe())
    assert restored == metadata


@pytest.mark.parametrize("backend", ["pandas", "polars", "pyarrow"])
@pytest.mark.parametrize(
    "dataframe_type",
    [None, "pandas", "pyarrow_table", "polars_dataframe", "polars_lazyframe"],
)
def test_file_metadata_to_dataframe_backend_and_dataframe_type_matrix(
    backend: DataFrameBackend, dataframe_type: DataFrameType | None
) -> None:
    """Every (backend, dataframe_type) combination returns the right output."""
    metadata = FileMetadata(
        name="RCB",
        periods=(FULL_SPAN,),
        file_schema=FieldSchema(fields=[_field("UNINUM")]),
    )
    frame = metadata.to_dataframe(backend=backend, dataframe_type=dataframe_type)
    expected_type = (
        _BACKEND_NATIVE_TYPES[backend]
        if dataframe_type is None
        else _DATAFRAME_TYPE_NATIVE_TYPES[dataframe_type]
    )
    assert isinstance(frame, expected_type)
    if isinstance(frame, pl.LazyFrame):
        frame = frame.collect()
    assert FileMetadata.from_dataframe(data=frame) == metadata


def test_file_metadata_dataframe_round_trip_when_lazy() -> None:
    """A lazy polars frame round-trips (exercises the lazy-collect branches)."""
    metadata = FileMetadata(
        name="RCB",
        periods=(FULL_SPAN,),
        file_schema=FieldSchema(fields=[_field("UNINUM")]),
    )
    with config_context(dataframe_backend="polars", lazy=True):
        frame = metadata.to_dataframe()
        assert isinstance(frame, pl.LazyFrame)
        restored = FileMetadata.from_dataframe(data=frame)
    assert restored == metadata


def test_file_metadata_to_dataframe_lazy_pipeline_does_not_collect_early() -> None:
    """Under lazy=True, `fields_frame` and `file_frame` reach concat still lazy.

    `FileMetadata.to_dataframe` used to collect `fields_frame` immediately
    after building it, before combining it with a fresh `file_frame`. It
    no longer does -- `file_frame` is instead matched to `fields_frame`'s
    laziness via `.lazy()`. Confirmed here by patching `concat` and
    checking both frames it receives are still `narwhals.LazyFrame`, not
    already collected.
    """
    from unittest.mock import patch

    from call_report.core import _schema

    metadata = FileMetadata(
        name="RCB",
        periods=(FULL_SPAN,),
        file_schema=FieldSchema(fields=[_field("UNINUM")]),
    )

    captured: list[object] = []
    original = _schema.concat

    def spy(*, frames: object, how: object) -> object:
        captured.extend(frames)  # type: ignore[arg-type]
        return original(frames=frames, how=how)  # type: ignore[call-overload]

    with (
        config_context(dataframe_backend="polars", lazy=True),
        patch.object(_schema, "concat", spy),
    ):
        frame = metadata.to_dataframe()

    assert isinstance(frame, pl.LazyFrame)
    assert len(captured) == 2
    assert all(isinstance(item, nw.LazyFrame) for item in captured)


def test_file_metadata_from_dataframe_rejects_missing_file_rows() -> None:
    """A dataframe with no file-level period rows is rejected."""
    schema = FieldSchema(fields=[_field("UNINUM")])
    with pytest.raises(SchemaError, match="no file-level period rows"):
        FileMetadata.from_dataframe(data=schema.to_dataframe())


def test_file_metadata_from_dataframe_rejects_multiple_file_names() -> None:
    """A dataframe mixing rows from more than one file is rejected."""
    schema = FieldSchema(fields=[_field("UNINUM")])
    frame_a = FileMetadata(
        name="RCB", periods=(FULL_SPAN,), file_schema=schema
    ).to_dataframe(backend="pandas", dataframe_type="pandas")
    frame_b = FileMetadata(
        name="RCB2", periods=(FULL_SPAN,), file_schema=schema
    ).to_dataframe(backend="pandas", dataframe_type="pandas")
    combined = pd.concat([frame_a, frame_b], ignore_index=True)
    with pytest.raises(SchemaError, match="exactly one"):
        FileMetadata.from_dataframe(data=combined)


# ---------------------------------------------------------------------------
# FieldSchema.is_equal / compare
# ---------------------------------------------------------------------------


def test_field_schema_is_equal_true_regardless_of_order_by_default() -> None:
    """Two schemas with the same fields in a different order are equal."""
    uninum = _field("UNINUM")
    rssd = _field("RSSD")
    forward = FieldSchema(fields=[uninum, rssd])
    reordered = FieldSchema(fields=[rssd, uninum])
    assert forward.is_equal(other=reordered) is True


def test_field_schema_is_equal_false_for_different_content() -> None:
    """Schemas with a different field are not equal."""
    a = FieldSchema(fields=[_field("UNINUM")])
    b = FieldSchema(fields=[_field("RSSD")])
    assert a.is_equal(other=b) is False


def test_field_schema_is_equal_check_order_detects_reordering() -> None:
    """check_order=True makes field order significant."""
    uninum = _field("UNINUM")
    rssd = _field("RSSD")
    forward = FieldSchema(fields=[uninum, rssd])
    reordered = FieldSchema(fields=[rssd, uninum])
    assert forward.is_equal(other=reordered, check_order=True) is False
    assert forward.is_equal(other=forward, check_order=True) is True


def test_field_schema_compare_detects_added_and_removed() -> None:
    """Pure adds/removes show up in added/removed, not changed."""
    before = FieldSchema(fields=[_field("UNINUM"), _field("RSSD")])
    after = FieldSchema(fields=[_field("UNINUM"), _field("ASSOC")])
    diff = before.compare(other=after)
    assert diff.added == ("ASSOC",)
    assert diff.removed == ("RSSD",)
    assert diff.changed == ()
    assert diff.is_empty is False


def test_field_schema_compare_detects_changed_field() -> None:
    """A field present in both schemas with different content shows up in changed."""
    before = FieldSchema(fields=[_field("UNINUM", definition="old")])
    after = FieldSchema(fields=[_field("UNINUM", definition="new")])
    diff = before.compare(other=after)
    assert diff.added == ()
    assert diff.removed == ()
    assert diff.changed == (
        FieldChange(name="UNINUM", before=before["UNINUM"], after=after["UNINUM"]),
    )


def test_field_schema_compare_identical_schemas_is_empty() -> None:
    """Comparing a schema against itself produces an empty diff."""
    schema = FieldSchema(fields=[_field("UNINUM")])
    assert schema.compare(other=schema).is_empty is True


def test_field_schema_compare_order_changed_only_when_requested() -> None:
    """order_changed stays False unless check_order=True is passed."""
    uninum = _field("UNINUM")
    rssd = _field("RSSD")
    forward = FieldSchema(fields=[uninum, rssd])
    reordered = FieldSchema(fields=[rssd, uninum])
    assert forward.compare(other=reordered).order_changed is False
    assert forward.compare(other=reordered, check_order=True).order_changed is True


def test_field_schema_compare_order_changed_ignores_added_removed_shift() -> None:
    """An add/remove alone doesn't spuriously flag surviving fields as reordered."""
    before = FieldSchema(fields=[_field("UNINUM"), _field("RSSD")])
    after = FieldSchema(fields=[_field("UNINUM"), _field("RSSD"), _field("ASSOC")])
    diff = before.compare(other=after, check_order=True)
    assert diff.added == ("ASSOC",)
    assert diff.order_changed is False


def test_field_schema_diff_is_empty_true_when_nothing_differs() -> None:
    """is_empty is True only when added/removed/changed are empty and order matches."""
    assert FieldSchemaDiff(added=(), removed=(), changed=()).is_empty is True


def test_field_schema_diff_is_empty_false_when_order_changed() -> None:
    """order_changed alone is enough to make is_empty False."""
    diff = FieldSchemaDiff(added=(), removed=(), changed=(), order_changed=True)
    assert diff.is_empty is False


# ---------------------------------------------------------------------------
# FieldChange.dtype_changed / definition_changed / periods_changed / content_changed
# ---------------------------------------------------------------------------


def test_field_change_dtype_changed_true_when_dtype_differs() -> None:
    """dtype_changed is True, and the other two axes False, when only dtype differs."""
    change = FieldChange(
        name="UNINUM",
        before=_field(dtype=nw.Int64()),
        after=_field(dtype=nw.Float64()),
    )
    assert change.dtype_changed is True
    assert change.definition_changed is False
    assert change.periods_changed is False


def test_field_change_definition_changed_true_when_definition_differs() -> None:
    """definition_changed is True, the other two False, when only definition differs."""
    change = FieldChange(
        name="UNINUM", before=_field(definition="old"), after=_field(definition="new")
    )
    assert change.definition_changed is True
    assert change.dtype_changed is False
    assert change.periods_changed is False


def test_field_change_periods_changed_true_when_span_differs() -> None:
    """periods_changed is True, the other two False, when only the span differs."""
    change = FieldChange(
        name="UNINUM",
        before=_field(periods=EARLY_SPAN),
        after=_field(periods=LATE_SPAN),
    )
    assert change.periods_changed is True
    assert change.dtype_changed is False
    assert change.definition_changed is False


def test_field_change_content_changed_true_for_dtype_or_definition() -> None:
    """content_changed is True whether dtype or definition is the one that moved."""
    dtype_only = FieldChange(
        name="UNINUM",
        before=_field(dtype=nw.Int64()),
        after=_field(dtype=nw.Float64()),
    )
    definition_only = FieldChange(
        name="UNINUM", before=_field(definition="old"), after=_field(definition="new")
    )
    assert dtype_only.content_changed is True
    assert definition_only.content_changed is True


def test_field_change_content_changed_false_when_only_periods_differ() -> None:
    """content_changed is False for the issue #68 case: a span-only difference."""
    change = FieldChange(
        name="UNINUM",
        before=_field(periods=EARLY_SPAN),
        after=_field(periods=LATE_SPAN),
    )
    assert change.periods_changed is True
    assert change.content_changed is False


def test_field_change_dtype_changed_true_when_version_count_differs() -> None:
    """A field split into a different number of versions counts as a dtype change."""
    before = _field(periods=FULL_SPAN)
    after = FieldAttributes(
        name="UNINUM",
        versions=(
            FieldVersion(dtype=nw.Int64(), definition="", periods=EARLY_SPAN),
            FieldVersion(dtype=nw.Float64(), definition="", periods=ADJACENT_SPAN),
        ),
    )
    change = FieldChange(name="UNINUM", before=before, after=after)
    assert change.dtype_changed is True
    assert change.content_changed is True


# ---------------------------------------------------------------------------
# FieldSchemaDiff.content_changed
# ---------------------------------------------------------------------------


def test_field_schema_diff_content_changed_filters_period_only_changes() -> None:
    """content_changed excludes fields that only differ by version span.

    Mirrors the cross-quarter `as_of` workflow from issue #68: PROVLNS has
    a real definition change, UNINUM only differs by which quarter its one
    surviving version is stamped with.
    """
    before = FieldSchema(
        fields=[
            _field("PROVLNS", definition="old definition", periods=EARLY_SPAN),
            _field("UNINUM", periods=EARLY_SPAN),
        ]
    )
    after = FieldSchema(
        fields=[
            _field("PROVLNS", definition="new definition", periods=LATE_SPAN),
            _field("UNINUM", periods=LATE_SPAN),
        ]
    )
    diff = before.compare(other=after)
    assert len(diff.changed) == 2
    assert [change.name for change in diff.content_changed] == ["PROVLNS"]


def test_field_schema_diff_content_changed_empty_for_period_only_diff() -> None:
    """content_changed is empty when every changed field differs only by span."""
    before = FieldSchema(fields=[_field("UNINUM", periods=EARLY_SPAN)])
    after = FieldSchema(fields=[_field("UNINUM", periods=LATE_SPAN)])
    diff = before.compare(other=after)
    assert len(diff.changed) == 1
    assert diff.content_changed == ()


# ---------------------------------------------------------------------------
# FieldSchema.to_json / from_json
# ---------------------------------------------------------------------------


def test_field_schema_json_round_trip() -> None:
    """from_json(to_json(schema)) reconstructs an equal schema."""
    schema = FieldSchema(
        fields=[_field("UNINUM"), _field("RSSD", periods=(EARLY_SPAN, LATE_SPAN))]
    )
    assert FieldSchema.from_json(text=schema.to_json()) == schema


def test_field_schema_json_round_trip_preserves_multiple_versions() -> None:
    """A field with multiple, differently-defined versions round-trips exactly."""
    field = FieldAttributes(
        name="INV_CODE",
        versions=(
            FieldVersion(dtype=nw.Int64(), definition="old", periods=EARLY_SPAN),
            FieldVersion(dtype=nw.Int64(), definition="new", periods=ADJACENT_SPAN),
        ),
    )
    schema = FieldSchema(fields=[field])
    restored = FieldSchema.from_json(text=schema.to_json())
    assert restored == schema
    assert restored["INV_CODE"].versions[0].definition == "old"
    assert restored["INV_CODE"].versions[1].definition == "new"


def test_field_schema_json_preserves_field_order() -> None:
    """Field order in the JSON matches the schema's own order."""
    schema = FieldSchema(fields=[_field("RSSD"), _field("UNINUM")])
    restored = FieldSchema.from_json(text=schema.to_json())
    assert restored.names == ("RSSD", "UNINUM")


def test_field_schema_to_json_default_indent_is_human_readable() -> None:
    """The default indent produces multi-line, diffable output."""
    schema = FieldSchema(fields=[_field("UNINUM")])
    assert "\n" in schema.to_json()


def test_field_schema_to_json_indent_none_is_compact() -> None:
    """indent=None produces a single-line, compact JSON string."""
    schema = FieldSchema(fields=[_field("UNINUM")])
    assert "\n" not in schema.to_json(indent=None)


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{"UNINUM": {}}',
        '{"UNINUM": {"versions": [{"dtype": "Int64"}]}}',
        '{"UNINUM": {"versions": [{"dtype": "not_a_dtype", "definition": "", '
        '"period_start": "2000-03-31", "period_end": "2026-03-31"}]}}',
        '{"UNINUM": {"versions": [{"dtype": "Int64", "definition": "", '
        '"period_start": "not-a-date", "period_end": "2026-03-31"}]}}',
    ],
    ids=[
        "invalid-json-syntax",
        "not-a-json-object",
        "missing-versions-key",
        "missing-fields-in-version",
        "invalid-dtype-repr",
        "invalid-period-value",
    ],
)
def test_field_schema_from_json_rejects_malformed_input(text: str) -> None:
    """Malformed JSON, at every level, raises SchemaError."""
    with pytest.raises(SchemaError):
        FieldSchema.from_json(text=text)


# ---------------------------------------------------------------------------
# FileMetadata.is_equal / compare
# ---------------------------------------------------------------------------


def test_file_metadata_is_equal_true_for_matching_metadata() -> None:
    """Two FileMetadata with the same name/periods/fields are equal."""
    schema = FieldSchema(fields=[_field("UNINUM")])
    a = FileMetadata(name="RCB", periods=(FULL_SPAN,), file_schema=schema)
    b = FileMetadata(name="RCB", periods=(FULL_SPAN,), file_schema=schema)
    assert a.is_equal(other=b) is True


def test_file_metadata_is_equal_false_for_different_name() -> None:
    """A different name alone makes two FileMetadata unequal."""
    schema = FieldSchema(fields=[])
    a = FileMetadata(name="RCB", periods=(FULL_SPAN,), file_schema=schema)
    b = FileMetadata(name="RCB2", periods=(FULL_SPAN,), file_schema=schema)
    assert a.is_equal(other=b) is False


def test_file_metadata_is_equal_delegates_check_order_to_field_schema() -> None:
    """check_order is passed through to the underlying field comparison."""
    uninum = _field("UNINUM")
    rssd = _field("RSSD")
    forward = FileMetadata(
        name="RCB",
        periods=(FULL_SPAN,),
        file_schema=FieldSchema(fields=[uninum, rssd]),
    )
    reordered = FileMetadata(
        name="RCB",
        periods=(FULL_SPAN,),
        file_schema=FieldSchema(fields=[rssd, uninum]),
    )
    assert forward.is_equal(other=reordered) is True
    assert forward.is_equal(other=reordered, check_order=True) is False


def test_file_metadata_compare_detects_name_and_periods_changes() -> None:
    """name_changed/periods_changed reflect direct differences."""
    schema = FieldSchema(fields=[])
    a = FileMetadata(name="RCB", periods=(FULL_SPAN,), file_schema=schema)
    b = FileMetadata(name="RCB2", periods=(EARLY_SPAN,), file_schema=schema)
    diff = a.compare(other=b)
    assert diff.name_changed is True
    assert diff.periods_changed is True
    assert diff.is_empty is False


def test_file_metadata_compare_delegates_field_comparison_to_field_schema() -> None:
    """The field-level diff matches what FieldSchema.compare produces directly."""
    before_schema = FieldSchema(fields=[_field("UNINUM", definition="old")])
    after_schema = FieldSchema(fields=[_field("UNINUM", definition="new")])
    before = FileMetadata(name="RCB", periods=(FULL_SPAN,), file_schema=before_schema)
    after = FileMetadata(name="RCB", periods=(FULL_SPAN,), file_schema=after_schema)
    diff = before.compare(other=after)
    assert diff.name_changed is False
    assert diff.periods_changed is False
    assert diff.file_schema_diff == before_schema.compare(other=after_schema)


def test_file_metadata_compare_identical_is_empty() -> None:
    """Comparing a FileMetadata against itself produces an empty diff."""
    metadata = FileMetadata(
        name="RCB",
        periods=(FULL_SPAN,),
        file_schema=FieldSchema(fields=[_field("UNINUM")]),
    )
    assert metadata.compare(other=metadata).is_empty is True


def test_file_metadata_diff_is_empty_requires_all_three_clear() -> None:
    """is_empty is True only when name/periods match and the field diff is empty."""
    empty_field_diff = FieldSchemaDiff(added=(), removed=(), changed=())
    assert (
        FileMetadataDiff(
            name_changed=False, periods_changed=False, file_schema_diff=empty_field_diff
        ).is_empty
        is True
    )
    assert (
        FileMetadataDiff(
            name_changed=True, periods_changed=False, file_schema_diff=empty_field_diff
        ).is_empty
        is False
    )


# ---------------------------------------------------------------------------
# FileMetadata.to_json / from_json
# ---------------------------------------------------------------------------


def test_file_metadata_json_round_trip() -> None:
    """from_json(to_json(metadata)) reconstructs an equal instance."""
    schema = FieldSchema(
        fields=[_field("UNINUM"), _field("RSSD", periods=(EARLY_SPAN, LATE_SPAN))]
    )
    metadata = FileMetadata(
        name="RCB", periods=(EARLY_SPAN, LATE_SPAN), file_schema=schema
    )
    assert FileMetadata.from_json(text=metadata.to_json()) == metadata


def test_file_metadata_json_round_trip_when_schema_empty() -> None:
    """A file with no known fields still round-trips its own periods."""
    metadata = FileMetadata(
        name="RCB", periods=(FULL_SPAN,), file_schema=FieldSchema(fields=[])
    )
    assert FileMetadata.from_json(text=metadata.to_json()) == metadata


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{"periods": [], "fields": {}}',
        '{"name": "RCB", "fields": {}}',
        '{"name": "RCB", "periods": [{"start": "bad-date", "end": "2026-03-31"}], '
        '"fields": {}}',
        '{"name": "RCB", "periods": [{"start": "2000-03-31", "end": "2026-03-31"}]}',
    ],
    ids=[
        "invalid-json-syntax",
        "not-a-json-object",
        "missing-name-key",
        "missing-periods-key",
        "invalid-period-value",
        "missing-fields-key",
    ],
)
def test_file_metadata_from_json_rejects_malformed_input(text: str) -> None:
    """Malformed JSON, at every level, raises SchemaError."""
    with pytest.raises(SchemaError):
        FileMetadata.from_json(text=text)
