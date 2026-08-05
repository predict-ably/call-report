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
human-readable definitions, and the exact periods each field has
actually been present for -- generated from FCA's own published
archives, not hand-maintained. Look a schedule's metadata up with
:func:`~call_report.fca.get_fca_file_metadata`:

.. code-block:: python

   from call_report.fca import FCASchedule, get_fca_file_metadata

   metadata = get_fca_file_metadata(schedule=FCASchedule.RCF1)
   metadata.file_schema.names
   # ('SYSTEM', 'DIST', 'ASSOC', 'MONTH', 'YEAR', 'UNINUM', 'LOANSTATUS',
   #  'ACCR', 'ACCRPDUE', 'FRMREST', 'NONCSH', 'NONOTH', 'TOTPERF')

These are exactly the columns you get back from ``report.load(schedule=...)``
-- the loader adds one column of its own, ``period``, identifying which
quarter each row came from; it isn't part of the schedule's own field
metadata:

.. code-block:: python

   report = FCACallReport(
       start="2025-03-31", end="2025-03-31", transport=PackagedArchiveTransport()
   )
   rcf1 = report.load(schedule="RCF1")
   list(rcf1.columns)
   # ['SYSTEM', 'DIST', 'ASSOC', 'MONTH', 'YEAR', 'UNINUM', 'LOANSTATUS',
   #  'ACCR', 'ACCRPDUE', 'FRMREST', 'NONCSH', 'NONOTH', 'TOTPERF', 'period']

A field's metadata is more than just its name. ``LOANSTATUS`` holds a
numeric code identifying each row's loan-performance category (its actual
values in ``rcf1`` are plain integers, e.g. ``100``, ``105``, ``155``);
the metadata's `definition` documents what those codes mean, and its
`dtype` matches the column's real, loaded type:

.. code-block:: python

   loanstatus = metadata.file_schema["LOANSTATUS"]
   loanstatus.versions[-1].dtype
   # Int64
   rcf1["LOANSTATUS"].dtype
   # dtype('int64')

A field's metadata can also carry more than one version: FCA revised
``LOANSTATUS``'s own code list in 2015 (splitting code ``155`` into a new
``152``/``155`` pair), with no gap in the field's presence -- `as_of`
recovers whichever definition applied at a given quarter:

.. code-block:: python

   len(loanstatus.versions)
   # 2
   metadata.file_schema.as_of(period="2010-03-31")["LOANSTATUS"].versions[0].definition
   # "...150 Discounted loans to OFIs 155 Other loans 160 Total"
   metadata.file_schema.as_of(period="2020-03-31")["LOANSTATUS"].versions[0].definition
   # "...150 Discounted loans to OFIs 152 Other loans 155 Total"

Reshaping to wide format
==========================

``report.load(schedule=...)`` returns one row per institution per period,
per *schedule*. :meth:`~call_report.fca.FCACallReport.to_wide_format`
goes a step further, stacking every schedule together into a single
frame with one row per ``(UNINUM, period)`` and one column per variable:

.. code-block:: python

   report = FCACallReport(
       start="2025-03-31", end="2025-03-31", transport=PackagedArchiveTransport()
   )
   wide = report.to_wide_format(schedules=["RC", "RCB"])
   wide.shape
   # (64, 194)

A plain (non-code) field is named ``{schedule}__{variable}``:

.. code-block:: python

   row = wide[wide["UNINUM"] == 620000].iloc[0]
   row["RC__ASSETS"]
   # 47138132.0

A field that RCB reports once per investment code instead becomes
``{schedule}__{code_column}_{code_value}__{variable}`` -- one column per
code actually reported:

.. code-block:: python

   row["RCB__INV_CODE_81__BKVAL"]
   # 9579.0

Leave ``schedules`` unset to include every schedule discovered across the
requested range. This works the same way on every configured dataframe
backend, including ``pyarrow`` -- which has no native pivot operation, so
``to_wide_format`` falls back to an equivalent filter-and-join reshape for
it automatically.

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
