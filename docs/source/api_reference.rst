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

Core
====

Shared, source-agnostic building blocks: the quarter-end reporting-period
vocabulary, the cross-time schema vocabulary, the cross-source enums, and
the abstract base every source-specific entry point implements.

.. autosummary::
   :toctree: generated
   :nosignatures:

   call_report.core.ReportingPeriod
   call_report.core.PeriodRange
   call_report.core.Quarter
   call_report.core.Source
   call_report.core.FileType
   call_report.core.BaseCallReport
   call_report.core.FieldVersion
   call_report.core.FieldAttributes
   call_report.core.FieldSchema
   call_report.core.FieldChange
   call_report.core.FieldSchemaDiff
   call_report.core.FileMetadata
   call_report.core.FileMetadataDiff

Exceptions
==========

The exception hierarchy shared across sources.

.. autosummary::
   :toctree: generated
   :nosignatures:

   call_report.exceptions.CallReportError
   call_report.exceptions.InvalidPeriodError
   call_report.exceptions.PeriodNotAvailableError
   call_report.exceptions.ScheduleNotFoundError
   call_report.exceptions.LayoutParseError
   call_report.exceptions.DownloadError
   call_report.exceptions.ReshapeError

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
   call_report.fca.convert_wide_format_to_long_format
   call_report.fca.convert_long_format_to_wide_format
   call_report.fca.get_fca_file_metadata
   call_report.fca.get_institutions_file_metadata
   call_report.fca.all_fca_file_metadata
   call_report.fca.catalog.construct_fca_download_url
   call_report.fca.layout.parse_layout
   call_report.fca.layout.FCALayout
   call_report.fca.layout.infer_field_dtype
   call_report.fca.reader.read_schedule_file
   call_report.fca.institutions.read_institutions
   call_report.fca.transport.FCATransport
   call_report.fca.transport.LocalDirectoryTransport
   call_report.fca.transport.PackagedArchiveTransport
