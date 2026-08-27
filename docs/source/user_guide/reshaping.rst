.. _user_guide_reshaping:

==========================
Reshaping Across Schedules
==========================

:meth:`~call_report.fca.FCACallReport.load` returns one schedule at a time,
one row per institution per period. Analysis usually wants every schedule
together, in one of two shapes:

- **Wide**: one row per ``(UNINUM, period)`` and one column per variable.
  The shape most modelling and spreadsheet work expects.
- **Long**: one row per institution, period, schedule, and variable. The
  *tidy* shape, better for filtering, grouping, and plotting.

Both stack every requested schedule, and you can convert between them
without going back to the source files.

The examples below use the default ``pandas`` backend, so the indexing is
pandas'. The reshaping methods themselves work on every configured backend,
including ``pyarrow``, which has no native pivot and falls back to an
equivalent filter-and-join reshape automatically. See
:doc:`dataframe_backends`.

.. doctest::

   >>> from call_report.fca import FCACallReport
   >>> from call_report.fca.transport import PackagedArchiveTransport
   >>> report = FCACallReport(
   ...     start="2025-03-31", end="2025-03-31", transport=PackagedArchiveTransport()
   ... )

Wide format
===========

:meth:`~call_report.fca.FCACallReport.to_wide_format` stacks every schedule
into a single frame with one row per institution and period:

.. doctest::

   >>> wide = report.to_wide_format(schedules=["RC", "RCB"])
   >>> wide.shape
   (64, 194)

A plain (non-code) field is named ``{schedule}__{variable}``:

.. doctest::

   >>> row = wide[wide["UNINUM"] == 620000].iloc[0]
   >>> float(row["RC__ASSETS"])
   47138132.0

A field a schedule reports once per code becomes
``{schedule}__{code_column}_{code_value}__{variable}``, one column per code
actually reported. RCB reports its balances once per investment code:

.. doctest::

   >>> float(row["RCB__INV_CODE_81__BKVAL"])
   9579.0

Leave ``schedules`` unset to include every schedule discovered across the
requested range.

Long format
===========

:meth:`~call_report.fca.FCACallReport.to_long_format` stacks the same
schedules without pivoting, one value per row:

.. doctest::

   >>> long = report.to_long_format(schedules=["RC", "RCB"])
   >>> long.shape
   (12288, 8)
   >>> list(long.columns)  # doctest: +NORMALIZE_WHITESPACE
   ['UNINUM', 'period', 'schedule', 'code_column', 'code_value',
    'variable_name', 'value', 'is_multiple']

That column order is part of the contract, and both routes to a long-format
frame produce it, so a positional read of one matches the other.

``value`` is always ``Float64``, the most generic type that represents every
schedule's measures. A plain field has ``is_multiple`` ``False`` and null
``code_column`` and ``code_value``:

.. doctest::

   >>> plain = long[
   ...     (long["UNINUM"] == 620000)
   ...     & (long["schedule"] == "RC")
   ...     & (long["variable_name"] == "ASSETS")
   ... ].iloc[0]
   >>> float(plain["value"]), bool(plain["is_multiple"])
   (47138132.0, False)

A field reported once per code has ``is_multiple`` ``True``, with
``code_column`` and ``code_value`` naming which code the row belongs to.
That matches :class:`~call_report.fca.layout.FCALayout`'s own
"single"/"multiple" vocabulary:

.. doctest::

   >>> coded = long[
   ...     (long["UNINUM"] == 620000)
   ...     & (long["schedule"] == "RCB")
   ...     & (long["code_value"] == 81.0)
   ... ].iloc[0]
   >>> coded["code_column"], float(coded["value"])
   ('INV_CODE', 9579.0)

Converting between the two
==========================

:func:`~call_report.fca.convert_wide_format_to_long_format` and
:func:`~call_report.fca.convert_long_format_to_wide_format` move between the
shapes without a fresh :class:`~call_report.fca.FCACallReport` call:

.. doctest::

   >>> from call_report.fca import convert_long_format_to_wide_format
   >>> convert_long_format_to_wide_format(long=long).shape
   (64, 194)

Going wide first and then long can produce a few extra, structurally null
rows compared with building long format directly. Pivoting fills in every
institution and column combination, including ones no institution actually
reported, as an explicit null. A directly built long-format frame only ever
has a row for a combination that genuinely appeared in the source.

Next steps
==========

- :doc:`dataframe_backends` covers choosing a backend and the dtypes each
  one produces.
- :doc:`schema_and_metadata` covers what the columns in these frames mean.
