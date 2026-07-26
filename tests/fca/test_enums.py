"""Tests for the FCASchedule enum and case-insensitive coercion."""

from __future__ import annotations

import pytest

from call_report.exceptions import ScheduleNotFoundError
from call_report.fca.enums import FCASchedule, coerce_schedule


def test_fca_schedule_covers_full_observed_history() -> None:
    """The enum spans both retired (pre-2018) and current schedule names."""
    names = {member.value for member in FCASchedule}
    # Retired, pre-2018 combined schedules:
    assert {"RCI", "RIE"} <= names
    # Current, post-2018 split schedules:
    assert {"RCI1", "RCI2A", "RCI2B", "RCI2C", "RCI2D", "RIE1", "RIE2"} <= names
    # Schedules only introduced later (RCB2-5, RCR1-7):
    assert {"RCB2", "RCB3", "RCB4", "RCB5", "RCR1", "RCR7"} <= names
    # INST is handled separately via load_institutions(), not a schedule.
    assert "INST" not in names


@pytest.mark.parametrize("value", ["RCB", "rcb", "Rcb", "rCb"])
def test_coerce_schedule_is_case_insensitive(value: str) -> None:
    """Schedule names are accepted regardless of case."""
    assert coerce_schedule(value=value) is FCASchedule.RCB


def test_coerce_schedule_passes_through_enum_member() -> None:
    """An already-FCASchedule value is returned unchanged."""
    assert coerce_schedule(value=FCASchedule.RC) is FCASchedule.RC


def test_coerce_schedule_rejects_unknown_name() -> None:
    """An unrecognized schedule name raises a clear, actionable error."""
    with pytest.raises(ScheduleNotFoundError, match="NOT_A_SCHEDULE"):
        coerce_schedule(value="NOT_A_SCHEDULE")


def test_coerce_schedule_is_keyword_only() -> None:
    """coerce_schedule takes no positional arguments."""
    with pytest.raises(TypeError):
        coerce_schedule("RCB")  # type: ignore[misc]
