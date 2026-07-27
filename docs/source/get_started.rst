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

- :class:`~call_report.types.ReportingPeriod` and
  :class:`~call_report.types.PeriodRange` -- the validated, calendar-quarter
  vocabulary every request and result is keyed by.
- :func:`~call_report.config.get_config` / :func:`~call_report.config.set_config`
  -- package-level configuration controlling which dataframe library every
  reader returns native frames of, and whether they are returned eager or
  lazy (see :ref:`api_ref` for the full configuration API).

Quickstart
==========

Load a single FCA schedule for a range of quarters from a directory of
already-downloaded, already-extracted release folders (one subdirectory per
quarter, named after each release's FCA zip file):

.. code-block:: python

   from call_report.fca import FCACallReport

   report = FCACallReport(start="2024-03-31", end="2025-12-31", data_dir="fca_data")
   rcb = report.load(schedule="RCB")

``load`` stacks the schedule across every requested period that has it,
returning a native dataframe (of whichever backend is configured) with a
``period`` column identifying which quarter each row came from.

Load every schedule found across the requested range at once:

.. code-block:: python

   schedules = report.load_all()  # dict[FCASchedule, native dataframe]

Load the institution roster:

.. code-block:: python

   institutions = report.load_institutions()

Choosing a dataframe backend
-----------------------------

By default, readers return ``pandas`` frames. Switch backends globally, or
temporarily within a ``with`` block:

.. code-block:: python

   from call_report.config import config_context, set_config

   set_config(dataframe_backend="polars")

   with config_context(dataframe_backend="pyarrow"):
       arrow_table = report.load(schedule="RCB")

Next steps
==========

- Browse the full :ref:`api_ref` for every public class and function.
- See :ref:`contrib_guide` to set up a development environment and
  contribute.

.. _scikit-learn: https://scikit-learn.org/stable/index.html
