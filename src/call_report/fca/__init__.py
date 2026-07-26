"""Farm Credit Administration (FCA) Call Report source."""

from __future__ import annotations

from call_report.fca.enums import FCASchedule
from call_report.fca.report import FCACallReport

__all__ = ["FCACallReport", "FCASchedule"]
