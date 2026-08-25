"""Property-based tests for the wide-format column naming in fca._reshape.

`_with_column_key` encodes four pieces of information into one string, and
`_parse_wide_column_key` decodes them back out. They are an inverse pair,
which is the shape property testing is best at.

The encoding is not trivially reversible. `code_column` may itself contain
an underscore (``INV_CODE``), so the decoder splits the middle segment from
the *right* on a single ``"_"`` rather than the left, and relies on
``code_value`` being purely digits. The existing example tests cover the
``INV_CODE`` case specifically. These let hypothesis look for a combination
of schedule, code column, code value, and variable name that the convention
cannot survive.

Names are generated without a literal ``"__"``, which is the assumption
`_parse_wide_column_key` documents and no real FCA schedule or field name
violates.
"""

from __future__ import annotations

import string
from typing import Any

import narwhals as nw
import pytest
from hypothesis import given
from hypothesis import strategies as st

from call_report.core._backend import build_frame
from call_report.exceptions import ReshapeError
from call_report.fca._reshape import _parse_wide_column_key, _with_column_key

# Real FCA schedule, code column, and field names are ASCII uppercase,
# digits, and underscores.
_ALPHABET = string.ascii_uppercase + string.digits + "_"


def _is_safe_name(value: str) -> bool:
    """Return whether a name survives the ``__``-delimited key convention."""
    return "__" not in value and not value.startswith("_") and not value.endswith("_")


names = st.text(alphabet=_ALPHABET, min_size=1, max_size=10).filter(_is_safe_name)
code_values = st.integers(min_value=0, max_value=9999)


def _key_of(frame: nw.DataFrame[Any]) -> str:
    """Return the single `column_key` value `_with_column_key` computed."""
    keyed = _with_column_key(frame)
    assert isinstance(keyed, nw.DataFrame)
    return str(keyed["column_key"].to_list()[0])


@given(schedule=names, variable=names)
def test_plain_column_key_round_trips(schedule: str, variable: str) -> None:
    """A field with no code column encodes and decodes back to itself."""
    frame = build_frame(
        data={
            "UNINUM": [1],
            "period": ["2026-03-31"],
            "schedule": [schedule],
            "variable_name": [variable],
            "value": [1.0],
        }
    )
    parsed = _parse_wide_column_key(_key_of(frame))
    assert parsed == (schedule, None, None, False, variable)


@given(schedule=names, code_column=names, code_value=code_values, variable=names)
def test_coded_column_key_round_trips(
    schedule: str, code_column: str, code_value: int, variable: str
) -> None:
    """A coded field encodes and decodes back to its four components."""
    frame = build_frame(
        data={
            "UNINUM": [1],
            "period": ["2026-03-31"],
            "schedule": [schedule],
            "code_column": [code_column],
            "code_value": [float(code_value)],
            "variable_name": [variable],
            "value": [1.0],
        }
    )
    parsed = _parse_wide_column_key(_key_of(frame))
    assert parsed == (schedule, code_column, float(code_value), True, variable)


@given(schedule=names, code_column=names, code_value=code_values, variable=names)
def test_coded_and_plain_keys_never_collide(
    schedule: str, code_column: str, code_value: int, variable: str
) -> None:
    """A coded key is never mistaken for a plain one, or the reverse.

    Both encodings share the same ``__`` separator, so the only thing
    keeping them apart is the segment count. This checks that holds for
    any component names, not just the ones examples happen to use.
    """
    coded = f"{schedule}__{code_column}_{code_value}__{variable}"
    plain = f"{schedule}__{variable}"
    assert _parse_wide_column_key(coded)[3] is True
    assert _parse_wide_column_key(plain)[3] is False


@given(schedule=names, variable=names)
def test_a_key_with_no_separator_is_rejected(schedule: str, variable: str) -> None:
    """A name missing the ``__`` separator raises rather than mis-parsing."""
    with pytest.raises(ReshapeError):
        _parse_wide_column_key(f"{schedule}_{variable}")


@given(schedule=names, code_column=names, variable=names)
def test_a_coded_key_with_a_non_numeric_code_is_rejected(
    schedule: str, code_column: str, variable: str
) -> None:
    """The middle segment must end in digits, or the key is malformed.

    Without the digit check, any three-segment name would be read as
    coded, silently inventing a code column and value.
    """
    with pytest.raises(ReshapeError):
        _parse_wide_column_key(f"{schedule}__{code_column}_NOTDIGITS__{variable}")
