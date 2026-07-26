call-report
===========

Tools for working with regulatory call report data filed by regulated U.S.
financial institutions.

Currently implemented: the package-level configuration and shared
interface, and the Farm Credit Administration (FCA) Call Report source,
reading from a local directory of already-extracted release files. Live
downloading, and the FFIEC/FDIC/NCUA sources, are planned for later
releases.

Installation
------------

.. code-block:: bash

   pip install call-report

Quickstart
----------

.. code-block:: python

   from call_report.fca import FCACallReport

   report = FCACallReport(start="2024-03-31", end="2025-12-31", data_dir="fca_data")
   rcb = report.load(schedule="RCB")

Package configuration
----------------------

Controls which dataframe library every reader returns native frames of,
and whether they are returned eager or lazy.

.. autosummary::
   :toctree: generated
   :nosignatures:

   call_report.config.get_config
   call_report.config.set_config
   call_report.config.config_context

Reporting periods
------------------

The source-agnostic quarter-end vocabulary shared by every regulatory
source.

.. autosummary::
   :toctree: generated
   :nosignatures:

   call_report.periods.ReportingPeriod
   call_report.periods.PeriodRange
   call_report.enums.Quarter
   call_report.enums.Source
   call_report.enums.FileKind

Shared interface
-----------------

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
-----------------

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

.. toctree::
   :maxdepth: 2
   :caption: Contents:
