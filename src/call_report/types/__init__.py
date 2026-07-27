"""Shared, source-agnostic data types for working with call report data.

This package collects the small vocabulary every call report source shares:
the calendar-quarter enum and reporting-period objects
(:class:`ReportingPeriod`, :class:`PeriodRange`), plus the cross-source
:class:`Source` and :class:`FileKind` enums. Import them directly from
``call_report.types`` (e.g. ``from call_report.types import ReportingPeriod``);
the concrete definitions live in private sub-modules and are re-exported here.
"""

from __future__ import annotations

from call_report.types._enums import FileKind, Quarter, Source
from call_report.types._periods import PeriodRange, ReportingPeriod

__all__ = [
    "FileKind",
    "PeriodRange",
    "Quarter",
    "ReportingPeriod",
    "Source",
]
