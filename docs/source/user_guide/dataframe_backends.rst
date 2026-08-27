.. _user_guide_dataframe_backends:

==================
Dataframe Backends
==================

``call-report`` does not have its own dataframe type. Every reader returns a
native frame of whichever library you have configured, reached through
`narwhals <https://narwhals-dev.github.io/narwhals/>`_ so the parsing code
is written once rather than three times.

Three backends are supported: ``pandas`` (the default), ``polars``, and
``pyarrow``. They are optional dependencies, so install the one you want:

.. code-block:: bash

   pip install "call-report[polars]"

.. testcleanup::

   # This page's examples call set_config, which is process-global, and
   # Sphinx runs every page's doctests in one process. Restoring the
   # defaults here means a failure part-way through this page cannot leave
   # another page's examples running on the wrong backend. It mirrors the
   # autouse reset_config fixture the test suite uses for the same reason.
   from call_report.config import set_config

   set_config(dataframe_backend="pandas", lazy=False)

Choosing a backend
==================

:func:`~call_report.config.get_config` reports the current settings:

.. doctest::

   >>> from call_report.config import config_context, get_config, set_config
   >>> get_config()
   {'dataframe_backend': 'pandas', 'lazy': False}

:func:`~call_report.config.config_context` changes them for the duration of
a ``with`` block and restores them on exit, which is the safer choice
whenever the change is meant to be temporary:

.. doctest::

   >>> from call_report.fca import FCACallReport
   >>> from call_report.fca.transport import PackagedArchiveTransport
   >>> report = FCACallReport(
   ...     start="2025-03-31", end="2025-03-31", transport=PackagedArchiveTransport()
   ... )
   >>> with config_context(dataframe_backend="pyarrow"):
   ...     type(report.load(schedule="RCF1")).__name__
   ...
   'Table'

:func:`~call_report.config.set_config` changes them for the rest of the
process:

.. doctest::

   >>> set_config(dataframe_backend="polars")
   >>> type(report.load(schedule="RCF1")).__name__
   'DataFrame'
   >>> set_config(dataframe_backend="pandas")

Lazy frames
===========

The ``lazy`` setting asks for a frame that defers its work until collected.
Only ``polars`` supports it, and asking for it with another backend raises
rather than silently returning an eager frame:

.. doctest::

   >>> with config_context(dataframe_backend="polars", lazy=True):
   ...     frame = report.load(schedule="RCF1")
   ...
   >>> type(frame).__name__
   'LazyFrame'
   >>> frame.collect().shape
   (767, 14)

Asking for one type at a time
=============================

Every reader also takes a ``dataframe_type`` argument, converting its result
as a final step. Use it when the code consuming the frame needs a specific
type while the package stays configured for another:

.. doctest::

   >>> type(report.load(schedule="RCF1", dataframe_type="pyarrow_table")).__name__
   'Table'

The accepted values are ``"pandas"``, ``"pyarrow_table"``,
``"polars_dataframe"``, and ``"polars_lazyframe"``.

What dtypes you get
===================

Column types come from the schedule's layout, not from whatever a
particular quarter's values happen to look like. A field that is empty one
quarter and populated the next therefore keeps one type across both.

Under pandas that means the nullable extension dtypes rather than the
numpy-backed defaults, since a numpy ``int64`` column cannot hold a missing
value:

.. doctest::

   >>> rcf1 = report.load(schedule="RCF1")
   >>> rcf1["LOANSTATUS"].dtype
   Int64Dtype()

That matches what the schedule's canonical metadata says the field is, so
:doc:`schema_and_metadata` can be trusted as a description of the frames you
actually get:

.. doctest::

   >>> report.get_file_metadata(schedule="RCF1").file_schema.schema["LOANSTATUS"]
   Int64

The ``period`` column
---------------------

The ``period`` column every reader adds names a calendar quarter end.
polars and pyarrow hold it as a date:

.. doctest::

   >>> with config_context(dataframe_backend="polars"):
   ...     report.load(schedule="RCF1").schema["period"]
   ...
   Date

pandas has no date dtype, so it holds the same value as a datetime whose
time component is always midnight. That keeps the ``.dt`` accessor and a
parquet round trip working:

.. doctest::

   >>> rcf1["period"].dtype
   dtype('<M8[us]')

A ``dataframe_type`` override follows the same rule, so the dtype depends on
the type you ask for rather than on the backend that built the frame.

Next steps
==========

- :doc:`reshaping` stacks every schedule into one wide or long frame.
- :doc:`schema_and_metadata` covers what each column means and how a
  schedule's fields have changed over time.
