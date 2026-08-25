"""Property-based tests for the schema vocabulary in call_report.core.

``tests/core/test_schema.py`` already round-trips a parametrized dtype, a
nested one, an Enum, and an Array through `FieldSchema.to_dataframe`. Those
examples cover each *shape* once. What they cannot cover is the shapes
composed together: an Array of List of Struct of Enum, a Struct whose
fields are themselves Structs, a Datetime carried three levels down inside
a List.

Dtypes are stored in a dataframe as their `repr` and rebuilt by
`_dtype_from_repr`, which parses the repr as an AST. That parser has to
handle every nesting narwhals can produce, so an arbitrary-depth generator
is a better fit than any list of examples someone writes by hand.
"""

from __future__ import annotations

import narwhals as nw
from hypothesis import given
from hypothesis import strategies as st

from call_report.core import (
    FieldAttributes,
    FieldSchema,
    FieldVersion,
    FileMetadata,
    PeriodRange,
)
from call_report.core._schema import _dtype_from_repr

# Names safe to embed in a dtype repr and in JSON: no quotes, no braces, no
# characters that would need escaping to survive the round trip.
names = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Nd"), whitelist_characters="_"),
    min_size=1,
    max_size=8,
).filter(lambda value: not value.startswith("_"))

_SIMPLE_DTYPES = [
    nw.Int8(),
    nw.Int16(),
    nw.Int32(),
    nw.Int64(),
    nw.UInt8(),
    nw.UInt16(),
    nw.UInt32(),
    nw.UInt64(),
    nw.Float32(),
    nw.Float64(),
    nw.String(),
    nw.Boolean(),
    nw.Date(),
    nw.Categorical(),
]

_TIME_UNITS = ["s", "ms", "us", "ns"]
_TIME_ZONES = [None, "UTC", "America/New_York"]

_leaf_dtypes = st.one_of(
    st.sampled_from(_SIMPLE_DTYPES),
    st.builds(
        nw.Datetime,
        time_unit=st.sampled_from(_TIME_UNITS),
        time_zone=st.sampled_from(_TIME_ZONES),
    ),
    st.builds(nw.Duration, time_unit=st.sampled_from(_TIME_UNITS)),
    st.lists(names, min_size=1, max_size=4, unique=True).map(nw.Enum),
)


def _nested(
    children: st.SearchStrategy[nw.dtypes.DType],
) -> st.SearchStrategy[nw.dtypes.DType]:
    """Wrap a dtype strategy in the three container dtypes narwhals has."""
    return st.one_of(
        children.map(nw.List),
        st.builds(nw.Array, children, st.integers(min_value=1, max_value=4)),
        st.dictionaries(names, children, min_size=1, max_size=3).map(nw.Struct),
    )


dtypes = st.recursive(_leaf_dtypes, _nested, max_leaves=4)

SPAN = PeriodRange(start="2000-03-31", end="2026-03-31")


@given(dtype=dtypes)
def test_dtype_repr_round_trips_for_any_nesting(dtype: nw.dtypes.DType) -> None:
    """Rebuilding a dtype from its own repr reproduces an equal dtype."""
    assert _dtype_from_repr(text=repr(dtype)) == dtype


@given(dtype=dtypes)
def test_dtype_repr_round_trip_is_stable_under_repetition(
    dtype: nw.dtypes.DType,
) -> None:
    """Rebuilding twice does not drift: repr of the rebuilt dtype is unchanged."""
    once = _dtype_from_repr(text=repr(dtype))
    assert repr(_dtype_from_repr(text=repr(once))) == repr(dtype)


@given(
    field_names=st.lists(names, min_size=1, max_size=4, unique=True),
    dtype=dtypes,
    definition=st.text(max_size=40),
)
def test_field_schema_json_round_trips(
    field_names: list[str], dtype: nw.dtypes.DType, definition: str
) -> None:
    """A schema rebuilt from its own JSON equals the original."""
    schema = FieldSchema(
        fields=[
            FieldAttributes(
                name=name,
                versions=(
                    FieldVersion(dtype=dtype, definition=definition, periods=SPAN),
                ),
            )
            for name in field_names
        ]
    )
    assert FieldSchema.from_json(text=schema.to_json()) == schema


@given(
    field_names=st.lists(names, min_size=1, max_size=4, unique=True),
    dtype=dtypes,
    definition=st.text(max_size=40),
)
def test_field_schema_json_preserves_field_order(
    field_names: list[str], dtype: nw.dtypes.DType, definition: str
) -> None:
    """Field order survives the JSON round trip, not just field identity."""
    schema = FieldSchema(
        fields=[
            FieldAttributes(
                name=name,
                versions=(
                    FieldVersion(dtype=dtype, definition=definition, periods=SPAN),
                ),
            )
            for name in field_names
        ]
    )
    assert FieldSchema.from_json(text=schema.to_json()).names == tuple(field_names)


@given(
    file_name=names,
    field_names=st.lists(names, min_size=0, max_size=3, unique=True),
    dtype=dtypes,
)
def test_file_metadata_json_round_trips(
    file_name: str, field_names: list[str], dtype: nw.dtypes.DType
) -> None:
    """File metadata rebuilt from its own JSON equals the original."""
    metadata = FileMetadata(
        name=file_name,
        periods=(SPAN,),
        file_schema=FieldSchema(
            fields=[
                FieldAttributes(
                    name=name,
                    versions=(FieldVersion(dtype=dtype, definition="", periods=SPAN),),
                )
                for name in field_names
            ]
        ),
    )
    assert FileMetadata.from_json(text=metadata.to_json()) == metadata


@given(field_names=st.lists(names, min_size=1, max_size=5, unique=True))
def test_subset_preserves_original_order_regardless_of_argument_order(
    field_names: list[str],
) -> None:
    """`subset` keeps the schema's own field order, not the order of `names`."""
    schema = FieldSchema(
        fields=[
            FieldAttributes(
                name=name,
                versions=(FieldVersion(dtype=nw.Int64(), definition="", periods=SPAN),),
            )
            for name in field_names
        ]
    )
    assert schema.subset(names=list(reversed(field_names))).names == tuple(field_names)
