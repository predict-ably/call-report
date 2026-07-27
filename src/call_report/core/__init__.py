"""Shared, source-agnostic building blocks for working with call report data.

This package collects the small vocabulary every call report source shares:
the calendar-quarter enum and reporting-period objects
(:class:`ReportingPeriod`, :class:`PeriodRange`), the cross-source
:class:`Source` and :class:`FileType` enums, and :class:`BaseCallReport`, the
abstract interface every source-specific entry point implements. Import them
directly from ``call_report.core`` (e.g.
``from call_report.core import ReportingPeriod``); the concrete definitions
live in private sub-modules and are re-exported here.
"""

from __future__ import annotations

from call_report.core._base import BaseCallReport
from call_report.core._enums import FileType, Quarter, Source
from call_report.core._periods import PeriodRange, ReportingPeriod

__all__ = [
    "BaseCallReport",
    "FileType",
    "PeriodRange",
    "Quarter",
    "ReportingPeriod",
    "Source",
]
