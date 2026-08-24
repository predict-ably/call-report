"""Shared, source-agnostic building blocks for working with call report data.

This package collects the small vocabulary every call report source shares:

- Reporting periods: :class:`Quarter`, :class:`ReportingPeriod`, and
  :class:`PeriodRange`.
- Cross-time schema metadata: :class:`FieldVersion`,
  :class:`FieldAttributes`, :class:`FieldSchema`, and
  :class:`FileMetadata`, plus their comparison results
  :class:`FieldChange`, :class:`FieldSchemaDiff`, and
  :class:`FileMetadataDiff`.
- Cross-source enums: :class:`Source` and :class:`FileType`.
- :class:`BaseCallReport`, the abstract interface every source-specific
  entry point implements.

The concrete definitions live in private sub-modules and are re-exported
here, so import them directly from ``call_report.core``, e.g.
``from call_report.core import ReportingPeriod``.
"""

from __future__ import annotations

from call_report.core._base import BaseCallReport
from call_report.core._enums import FileType, Quarter, Source
from call_report.core._periods import PeriodRange, ReportingPeriod
from call_report.core._schema import (
    FieldAttributes,
    FieldChange,
    FieldSchema,
    FieldSchemaDiff,
    FieldVersion,
    FileMetadata,
    FileMetadataDiff,
)

__all__ = [
    "BaseCallReport",
    "FieldAttributes",
    "FieldChange",
    "FieldSchema",
    "FieldSchemaDiff",
    "FieldVersion",
    "FileMetadata",
    "FileMetadataDiff",
    "FileType",
    "PeriodRange",
    "Quarter",
    "ReportingPeriod",
    "Source",
]
