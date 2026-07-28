"""Tests for the cross-time schema vocabulary (call_report.core)."""

from __future__ import annotations

import dataclasses

import pandas as pd
import polars as pl
import pyarrow as pa
import pytest

from call_report.config import config_context
from call_report.core import (
    FieldAttributes,
    FieldSchema,
    FileMetadata,
    PeriodRange,
    ReportingPeriod,
)
from call_report.exceptions import PeriodNotAvailableError, SchemaError

FULL_SPAN = PeriodRange(start="2000-03-31", end="2026-03-31")
EARLY_SPAN = PeriodRange(start="2000-03-31", end="2005-12-31")
LATE_SPAN = PeriodRange(start="2010-03-31", end="2026-03-31")


def _field(
    name: str = "UNINUM",
    *,
    dtype: str = "Numeric",
    definition: str = "",
    periods: tuple[PeriodRange, ...] = (FULL_SPAN,),
) -> FieldAttributes:
    return FieldAttributes(
        name=name, dtype=dtype, definition=definition, periods=periods
    )


# ---------------------------------------------------------------------------
# _validate_period_spans (exercised through FieldAttributes and FileMetadata)
# ---------------------------------------------------------------------------


def test_field_attributes_rejects_empty_periods() -> None:
    """An empty `periods` tuple is rejected."""
    with pytest.raises(SchemaError, match="at least one period span"):
        _field(periods=())


def test_field_attributes_accepts_multiple_gapped_spans() -> None:
    """Two spans with a real gap between them are accepted."""
    field = _field(periods=(EARLY_SPAN, LATE_SPAN))
    assert field.first_period.label == "2000Q1"
    assert field.last_period.label == "2026Q1"


@pytest.mark.parametrize(
    "second_span",
    [
        PeriodRange(start="2006-03-31", end="2009-12-31"),  # adjacent, no gap
        PeriodRange(start="2003-03-31", end="2009-12-31"),  # overlaps EARLY_SPAN
        PeriodRange(start="1999-03-31", end="1999-12-31"),  # out of order
    ],
)
def test_field_attributes_rejects_touching_or_unordered_spans(
    second_span: PeriodRange,
) -> None:
    """Adjacent, overlapping, or out-of-order spans are all rejected."""
    with pytest.raises(SchemaError, match="chronologically ordered"):
        _field(periods=(EARLY_SPAN, second_span))


def test_field_attributes_is_frozen() -> None:
    """FieldAttributes instances cannot be mutated after construction."""
    field = _field()
    with pytest.raises(dataclasses.FrozenInstanceError):
        field.name = "OTHER"  # type: ignore[misc]


def test_field_attributes_is_keyword_only() -> None:
    """The FieldAttributes constructor takes no positional args."""
    with pytest.raises(TypeError):
        FieldAttributes("UNINUM", "Numeric", "", (FULL_SPAN,))  # type: ignore[call-arg]


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
# FileMetadata
# ---------------------------------------------------------------------------


def test_file_metadata_first_and_last_period() -> None:
    """first_period/last_period reflect the outermost span bounds."""
    metadata = FileMetadata(
        name="RCB", periods=(EARLY_SPAN, LATE_SPAN), schema=FieldSchema(fields=[])
    )
    assert metadata.first_period.label == "2000Q1"
    assert metadata.last_period.label == "2026Q1"


def test_file_metadata_rejects_invalid_periods() -> None:
    """FileMetadata reuses the same span validation as FieldAttributes."""
    with pytest.raises(SchemaError, match=r"file 'RCB'.*at least one period span"):
        FileMetadata(name="RCB", periods=(), schema=FieldSchema(fields=[]))


def test_file_metadata_not_changed_when_fields_match_file_span() -> None:
    """Changed is False when every field covers the file's full lifetime."""
    schema = FieldSchema(fields=[_field("UNINUM", periods=(FULL_SPAN,))])
    metadata = FileMetadata(name="RCB", periods=(FULL_SPAN,), schema=schema)
    assert metadata.changed is False


def test_file_metadata_changed_when_a_field_span_differs() -> None:
    """Changed is True when a field's presence differs from the file's own."""
    schema = FieldSchema(fields=[_field("RSSD", periods=(LATE_SPAN,))])
    metadata = FileMetadata(name="RCB", periods=(FULL_SPAN,), schema=schema)
    assert metadata.changed is True


def test_file_metadata_is_frozen() -> None:
    """FileMetadata instances cannot be mutated after construction."""
    metadata = FileMetadata(
        name="RCB", periods=(FULL_SPAN,), schema=FieldSchema(fields=[])
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
    assert snapshot["UNINUM"].periods == (
        PeriodRange(start="2010-03-31", end="2010-03-31"),
    )


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
    frame = schema.to_dataframe()
    assert len(frame) == 2
    assert list(frame["field_name"]) == ["RSSD", "RSSD"]


def test_field_schema_dataframe_round_trip() -> None:
    """from_dataframe(to_dataframe(schema)) reconstructs an equal schema."""
    schema = FieldSchema(
        fields=[
            _field("UNINUM", periods=(FULL_SPAN,)),
            _field(
                "RSSD",
                dtype="Numeric",
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
    backend: str, dataframe_type: str | None
) -> None:
    """Every (backend, dataframe_type) combination returns the right output."""
    schema = FieldSchema(fields=[_field("UNINUM")])
    frame = schema.to_dataframe(
        backend=backend,  # type: ignore[arg-type]
        dataframe_type=dataframe_type,  # type: ignore[arg-type]
    )
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
    metadata = FileMetadata(name="RCB", periods=(FULL_SPAN,), schema=schema)
    snapshot = metadata.as_of(period="2007-03-31")
    assert snapshot.name == "RCB"
    assert snapshot.periods == (PeriodRange(start="2007-03-31", end="2007-03-31"),)
    # RSSD has a gap over 2007Q1, so it should have been dropped by FieldSchema.as_of.
    assert snapshot.schema.names == ("UNINUM",)


def test_file_metadata_as_of_rejects_unpublished_period() -> None:
    """as_of raises if the file was not published as of `period`."""
    metadata = FileMetadata(
        name="RCB", periods=(FULL_SPAN,), schema=FieldSchema(fields=[])
    )
    with pytest.raises(PeriodNotAvailableError, match="RCB"):
        metadata.as_of(period="1990-03-31")


# ---------------------------------------------------------------------------
# FileMetadata.to_dataframe / from_dataframe
# ---------------------------------------------------------------------------


def test_file_metadata_to_dataframe_columns() -> None:
    """to_dataframe produces the documented column set, in order."""
    metadata = FileMetadata(
        name="RCB", periods=(FULL_SPAN,), schema=FieldSchema(fields=[_field("UNINUM")])
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
    metadata = FileMetadata(name="RCB", periods=(EARLY_SPAN, LATE_SPAN), schema=schema)
    restored = FileMetadata.from_dataframe(data=metadata.to_dataframe())
    assert restored == metadata


def test_file_metadata_dataframe_round_trip_when_schema_empty() -> None:
    """A file with no known fields still round-trips its own periods."""
    metadata = FileMetadata(
        name="RCB", periods=(FULL_SPAN,), schema=FieldSchema(fields=[])
    )
    restored = FileMetadata.from_dataframe(data=metadata.to_dataframe())
    assert restored == metadata


@pytest.mark.parametrize("backend", ["pandas", "polars", "pyarrow"])
@pytest.mark.parametrize(
    "dataframe_type",
    [None, "pandas", "pyarrow_table", "polars_dataframe", "polars_lazyframe"],
)
def test_file_metadata_to_dataframe_backend_and_dataframe_type_matrix(
    backend: str, dataframe_type: str | None
) -> None:
    """Every (backend, dataframe_type) combination returns the right output."""
    metadata = FileMetadata(
        name="RCB", periods=(FULL_SPAN,), schema=FieldSchema(fields=[_field("UNINUM")])
    )
    frame = metadata.to_dataframe(
        backend=backend,  # type: ignore[arg-type]
        dataframe_type=dataframe_type,  # type: ignore[arg-type]
    )
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
        name="RCB", periods=(FULL_SPAN,), schema=FieldSchema(fields=[_field("UNINUM")])
    )
    with config_context(dataframe_backend="polars", lazy=True):
        frame = metadata.to_dataframe()
        assert isinstance(frame, pl.LazyFrame)
        restored = FileMetadata.from_dataframe(data=frame)
    assert restored == metadata


def test_file_metadata_from_dataframe_rejects_missing_file_rows() -> None:
    """A dataframe with no file-level period rows is rejected."""
    schema = FieldSchema(fields=[_field("UNINUM")])
    with pytest.raises(SchemaError, match="no file-level period rows"):
        FileMetadata.from_dataframe(data=schema.to_dataframe())


def test_file_metadata_from_dataframe_rejects_multiple_file_names() -> None:
    """A dataframe mixing rows from more than one file is rejected."""
    schema = FieldSchema(fields=[_field("UNINUM")])
    frame_a = FileMetadata(
        name="RCB", periods=(FULL_SPAN,), schema=schema
    ).to_dataframe(backend="pandas")
    frame_b = FileMetadata(
        name="RCB2", periods=(FULL_SPAN,), schema=schema
    ).to_dataframe(backend="pandas")
    combined = pd.concat([frame_a, frame_b], ignore_index=True)
    with pytest.raises(SchemaError, match="exactly one"):
        FileMetadata.from_dataframe(data=combined)
