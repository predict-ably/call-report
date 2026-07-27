.. _changelog:

=========
Changelog
=========

All notable changes to ``call-report`` are documented on this page, newest
release first. See the :ref:`release_process` for how this page fits into
cutting a release.

.. _changelog_0.1.0:

0.1.0 (2026-07-26)
====================

Initial release.

- **Package configuration** (:mod:`call_report.config`): choose the
  dataframe backend a reader returns native frames of (``pandas``,
  ``polars``, or ``pyarrow``), and whether frames are returned eager or
  lazy, via :func:`~call_report.config.get_config`,
  :func:`~call_report.config.set_config`, and
  :func:`~call_report.config.config_context`.
- **Shared, source-agnostic core vocabulary** (:mod:`call_report.core`):
  the calendar-quarter reporting-period objects
  :class:`~call_report.core.ReportingPeriod` and
  :class:`~call_report.core.PeriodRange`, the cross-source
  :class:`~call_report.core.Quarter`, :class:`~call_report.core.Source`,
  and :class:`~call_report.core.FileType` enums, and
  :class:`~call_report.core.BaseCallReport`, the abstract, scikit-learn-style
  interface every source-specific entry point implements.
- A shared exception hierarchy (:mod:`call_report.exceptions`).
- **The first regulatory source**, :mod:`call_report.fca`:
  :class:`~call_report.fca.FCACallReport` reads already-extracted Farm
  Credit Administration Call Report releases from a local directory, with
  lower-level building blocks (layout parsing, schedule and institution
  data reading, and pluggable transports) available on their own. Live
  downloading directly from FCA is not yet supported; this repository
  ships every quarterly release since 2000 as a packaged archive, usable
  immediately via
  :class:`~call_report.fca.transport.PackagedArchiveTransport`.
- Supports Python 3.11 through 3.14 on Linux, macOS, and Windows.

Live downloading from FCA and the FFIEC, FDIC, and NCUA sources are
planned for later releases -- see the project's ``CLAUDE.md`` for the
current roadmap.
