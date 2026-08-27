.. _getting_started:

===========
Get Started
===========

This page gets you from installation to loading your first schedule.

Installation
============

``call-report`` supports Python 3.11 through 3.14 on Linux, macOS, and Windows.

.. code-block:: bash

   pip install call-report

Reading FCA Call Report data additionally requires a dataframe library
(``pandas``, ``polars``, or ``pyarrow``). Install one directly, or via an
extra:

.. code-block:: bash

   pip install "call-report[pandas]"

Key Concepts
============

``call-report`` follows a `scikit-learn`_-style, object-oriented interface:
each regulatory source has its own *estimator-like* entry point (for
example :class:`~call_report.fca.FCACallReport`). Constructing one only
stores its parameters; a ``fetch``/``load``-style method performs the actual
work and populates trailing-underscore attributes (e.g. ``periods_``).

Two pieces of shared vocabulary are used across every source:

- :class:`~call_report.core.ReportingPeriod` and
  :class:`~call_report.core.PeriodRange` -- the validated, calendar-quarter
  vocabulary every request and result is keyed by.
- :func:`~call_report.config.get_config` / :func:`~call_report.config.set_config`
  -- package-level configuration controlling which dataframe library every
  reader returns native frames of, and whether they are returned eager or
  lazy (see :ref:`api_ref` for the full configuration API).

Every entry point also requires an explicit ``transport=`` -- an injectable
:class:`~call_report.fca.transport.FCATransport` describing how to resolve
each requested period's files. There is no implicit default, so it is
always clear which files a given instance will read.

Quickstart
==========

Load a single FCA schedule for a range of quarters from a directory of
already-downloaded, already-extracted release folders (one subdirectory per
quarter, named after each release's FCA zip file), using
:class:`~call_report.fca.transport.LocalDirectoryTransport`:

.. code-block:: python

   from call_report.fca import FCACallReport
   from call_report.fca.transport import LocalDirectoryTransport

   report = FCACallReport(
       start="2024-03-31",
       end="2025-12-31",
       transport=LocalDirectoryTransport(data_dir="fca_data"),
   )
   rcb = report.load(schedule="RCB")

Alternatively, load historical quarters straight from the release zips
checked into this repository's own ``data/fca-call-report/`` directory,
with no download or manual unzipping needed, using
:class:`~call_report.fca.transport.PackagedArchiveTransport`:

.. code-block:: python

   from call_report.fca.transport import PackagedArchiveTransport

   report = FCACallReport(
       start="2024-03-31", end="2025-12-31", transport=PackagedArchiveTransport()
   )
   rcb = report.load(schedule="RCB")

That archive is checked into the repository but is not included in the
published wheel, so ``PackagedArchiveTransport`` only works from a source
checkout (e.g. an editable install for local development); a ``pip``-installed
package should use ``LocalDirectoryTransport`` instead.

``load`` stacks the schedule across every requested period that has it,
returning a native dataframe (of whichever backend is configured) with a
``period`` column identifying which quarter each row came from.

Load every schedule found across the requested range at once:

.. code-block:: python

   schedules = report.load_all()  # dict[FCASchedule, native dataframe]

Load the institution roster:

.. code-block:: python

   institutions = report.load_institutions()

Schedule metadata
==================

Alongside the data itself, ``call-report`` ships canonical, cross-time
metadata for every FCA schedule: field names, narwhals dtypes,
human-readable definitions, and the exact periods each field has actually
been present for, generated from FCA's own published archives rather than
hand-maintained.

:meth:`~call_report.fca.FCACallReport.get_file_metadata` gives a schedule's
whole history, :meth:`~call_report.fca.FCACallReport.get_schema` a single
quarter's snapshot, and
:meth:`~call_report.fca.layout.FCALayout.to_field_schema` what a release
actually declared. Because all three speak the same vocabulary, checking a
release against what the package expects is three lines:

.. doctest::

   >>> from call_report.fca import FCACallReport
   >>> from call_report.fca.transport import PackagedArchiveTransport
   >>> report = FCACallReport(
   ...     start="2015-03-31", end="2015-03-31", transport=PackagedArchiveTransport()
   ... )
   >>> layout = report.get_layout(schedule="RI", period="2015-03-31")
   >>> canonical = report.get_schema(schedule="RI", period="2015-03-31")
   >>> layout.to_field_schema(period="2015-03-31").compare(other=canonical).is_empty
   True

See :doc:`user_guide/schema_and_metadata` for the full treatment, including
reading a diff and tracking a schedule's drift across quarters.

Next steps
==========

- The :ref:`user_guide` works through each capability in depth:
  :doc:`user_guide/schema_and_metadata`, :doc:`user_guide/reshaping`, and
  :doc:`user_guide/dataframe_backends`.
- Browse the full :ref:`api_ref` for every public class and function.
- See :ref:`contrib_guide` to set up a development environment and
  contribute.

.. _scikit-learn: https://scikit-learn.org/stable/index.html
