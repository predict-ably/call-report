.. _api_ref:

===
API
===

This is the API reference for ``call-report``. See :ref:`getting_started` for
a task-oriented introduction to the same functionality.

Package configuration
======================

Controls which dataframe library every reader returns native frames of, and
whether they are returned eager or lazy.

.. autosummary::
   :toctree: generated
   :nosignatures:

   call_report.config.get_config
   call_report.config.set_config
   call_report.config.config_context

Call Report Reporting periods
=============================

The source-agnostic quarter-end vocabulary shared by every regulatory source.

.. autosummary::
   :toctree: generated
   :nosignatures:

   call_report.types.ReportingPeriod
   call_report.types.PeriodRange
   call_report.types.Quarter
   call_report.types.Source
   call_report.types.FileKind

Shared interface
================

The abstract base every source-specific entry point implements, and the
exception hierarchy shared across sources.

.. autosummary::
   :toctree: generated
   :nosignatures:

   call_report.base.BaseCallReport
   call_report.exceptions.CallReportError
   call_report.exceptions.InvalidPeriodError
   call_report.exceptions.PeriodNotAvailableError
   call_report.exceptions.ScheduleNotFoundError
   call_report.exceptions.LayoutParseError
   call_report.exceptions.DownloadError

FCA Call Report
================

``FCACallReport`` is the main entry point; the other members below are the
lower-level building blocks it's composed from, useful on their own for
parsing an individual layout or data file.

.. autosummary::
   :toctree: generated
   :nosignatures:

   call_report.fca.FCACallReport
   call_report.fca.FCASchedule
   call_report.fca.catalog.construct_fca_download_url
   call_report.fca.layout.parse_layout
   call_report.fca.layout.FCALayout
   call_report.fca.reader.read_schedule_file
   call_report.fca.institutions.read_institutions
   call_report.fca.transport.FCATransport
   call_report.fca.transport.LocalDirectoryTransport
