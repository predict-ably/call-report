"""Tools for working with regulatory call report data.

Source-specific functionality lives in its own sub-module (e.g.
``call_report.fca``). Shared building blocks used across every source
(configuration, the period vocabulary, enums, exceptions, the base
interface) live in their own top-level modules -- import them from there
(e.g. ``from call_report.config import get_config``,
``from call_report.types import ReportingPeriod``) rather than from this
package root.
"""

from __future__ import annotations

__version__ = "0.1.0"
