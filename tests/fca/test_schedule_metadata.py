"""Tests for the FCA schedule metadata loader (call_report.fca._schedule_metadata)."""

from __future__ import annotations

import pytest

from call_report.core import FileMetadata
from call_report.fca import (
    FCASchedule,
    all_fca_file_metadata,
    get_fca_file_metadata,
    get_institutions_file_metadata,
)
from call_report.fca.institutions import INSTITUTIONS_ROOT


@pytest.mark.parametrize("schedule", list(FCASchedule))
def test_get_fca_file_metadata_loads_every_schedule(schedule: FCASchedule) -> None:
    """Every real FCASchedule member has shipped, loadable metadata."""
    metadata = get_fca_file_metadata(schedule=schedule)
    assert isinstance(metadata, FileMetadata)
    assert metadata.name == schedule.value


def test_get_fca_file_metadata_is_cached() -> None:
    """The same schedule returns the identical cached object, not a fresh parse."""
    first = get_fca_file_metadata(schedule=FCASchedule.RCB)
    second = get_fca_file_metadata(schedule=FCASchedule.RCB)
    assert first is second


def test_get_fca_file_metadata_is_keyword_only() -> None:
    """get_fca_file_metadata takes no positional arguments.

    Note: mypy doesn't statically catch this -- `functools.cache`'s stub
    doesn't preserve the wrapped function's keyword-only-ness -- but the
    real, wrapped function still enforces it at runtime.
    """
    with pytest.raises(TypeError):
        get_fca_file_metadata(FCASchedule.RCB)


def test_get_institutions_file_metadata_loads_the_roster() -> None:
    """The institution roster loads under its own root name, not via FCASchedule."""
    metadata = get_institutions_file_metadata()
    assert metadata.name == INSTITUTIONS_ROOT


def test_get_institutions_file_metadata_is_cached() -> None:
    """Repeated calls return the identical cached object."""
    assert get_institutions_file_metadata() is get_institutions_file_metadata()


def test_all_fca_file_metadata_covers_every_schedule() -> None:
    """all_fca_file_metadata returns exactly the FCASchedule members, no more/fewer."""
    metadata_by_schedule = all_fca_file_metadata()
    assert set(metadata_by_schedule) == set(FCASchedule)
    for schedule, metadata in metadata_by_schedule.items():
        assert metadata.name == schedule.value


def test_all_fca_file_metadata_reuses_the_cached_get_fca_file_metadata() -> None:
    """Entries come from the same cache get_fca_file_metadata populates."""
    direct = get_fca_file_metadata(schedule=FCASchedule.RCB)
    assert all_fca_file_metadata()[FCASchedule.RCB] is direct


def test_rcb_inv_code_reflects_the_real_2015_code_list_expansion() -> None:
    """Regression check: RCB's INV_CODE field is genuinely versioned, not flattened.

    Confirms the shipped metadata actually carries the real FCA drift this
    whole feature exists for -- the code list embedded in INV_CODE's
    definition grew starting in 2015, with no presence gap.
    """
    schema = get_fca_file_metadata(schedule=FCASchedule.RCB).file_schema
    assert len(schema["INV_CODE"].versions) > 1
    old_definition = (
        schema.as_of(period="2010-03-31")["INV_CODE"].versions[0].definition
    )
    new_definition = (
        schema.as_of(period="2020-03-31")["INV_CODE"].versions[0].definition
    )
    assert old_definition != new_definition
    assert "SBA" in new_definition
