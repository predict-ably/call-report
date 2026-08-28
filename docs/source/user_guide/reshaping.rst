.. _user_guide_reshaping:

==========================
Reshaping Across Schedules
==========================

:meth:`~call_report.fca.FCACallReport.load` returns one schedule at a time,
one row per institution per period. Analysis usually wants every schedule
together, in one of three shapes:

- **Wide**: one row per ``(UNINUM, period)`` and one column per variable.
  The shape most modelling and spreadsheet work expects.
- **Long**: one row per institution, period, schedule, and variable. The
  *tidy* shape, better for filtering, grouping, and plotting.
- **Code grain**: one row per institution, period, and the *code* a
  schedule reports at, such as an investment code or a loan portfolio.
  The shape for comparing one measure across codes.

All three stack every requested schedule, and you can convert between them
without going back to the source files.

The examples below use the default ``pandas`` backend, so the indexing is
pandas'. The reshaping methods themselves work on every configured backend,
including ``pyarrow``, which has no native pivot and uses an equivalent
single-pass reshape automatically. See :doc:`dataframe_backends`.

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

Code grain
==========

The wide format folds a code into the column name, so comparing one measure
across codes means parsing hundreds of headers. The long format keeps the
code as a row key but leaves every measure stacked in one ``value`` column.
:meth:`~call_report.fca.FCACallReport.to_code_grain_format` is the third
shape: the code stays a row key, and each variable gets its own column.

.. doctest::

   >>> code_grain = report.to_code_grain_format(schedules=["RCB", "RCF1"])
   >>> list(code_grain.columns)[:6]  # doctest: +NORMALIZE_WHITESPACE
   ['UNINUM', 'period', 'code_column', 'code_value', 'RCB__BKVAL',
    'RCB__BKVALFORSALE']

Rows are keyed by ``(UNINUM, period, code_column, code_value)``. Measure
columns are named ``{schedule}__{variable}``, the same way a plain field is
named in the wide format. The schedule stays out of the row key on purpose,
so two schedules reporting at the same code contribute columns to one row
rather than a row each. That is what makes a sub-architecture such as a
loan-portfolio dataset possible.

RC-F.1 reports loan performance once per loan portfolio, so its portfolios
become rows. Code 110 is agribusiness:

.. doctest::

   >>> portfolio = code_grain[
   ...     (code_grain["UNINUM"] == 620000)
   ...     & (code_grain["code_column"] == "LOANSTATUS")
   ...     & (code_grain["code_value"] == 110.0)
   ... ].iloc[0]
   >>> float(portfolio["RCF1__ACCR"]), float(portfolio["RCF1__TOTPERF"])
   (3067844.0, 3068039.0)

Schedules whose code columns differ are stacked, not joined. ``code_column``
is part of the grain, so RC-B's ``INV_CODE`` rows and RC-F.1's
``LOANSTATUS`` rows coexist, each populating only its own schedule's
columns:

.. doctest::

   >>> sorted(code_grain["code_column"].unique())
   ['INV_CODE', 'LOANSTATUS']

Two schedules can even share a code column *name* while using different code
universes. RC-F's ``LOANSTATUS`` is a loan performance status, RC-F.1's is a
loan portfolio. The schedule-prefixed column names keep the two separable.

Only code-bearing schedules take part. A schedule that reports no code at
all, such as RC, has no code grain, and leaving ``schedules`` unset skips
those:

.. doctest::

   >>> everything = report.to_code_grain_format()
   >>> sorted({name.split("__")[0] for name in everything.columns[4:]})
   ... # doctest: +NORMALIZE_WHITESPACE
   ['RCB', 'RCB2', 'RCB3', 'RCF', 'RCF1', 'RCI2B', 'RCI2C', 'RCI2D', 'RCO',
    'RCR3', 'RCR7', 'RID', 'RIE1']

Naming one explicitly is an error instead, since a request for its columns
cannot be honored:

.. doctest::

   >>> report.to_code_grain_format(schedules=["RC", "RCB"])
   Traceback (most recent call last):
   call_report.exceptions.ReshapeError: Schedules ['RC'] report no code, ...

Converting between the shapes
=============================

:func:`~call_report.fca.convert_wide_format_to_long_format` and
:func:`~call_report.fca.convert_long_format_to_wide_format` move between the
shapes without a fresh :class:`~call_report.fca.FCACallReport` call:

.. doctest::

   >>> from call_report.fca import convert_long_format_to_wide_format
   >>> convert_long_format_to_wide_format(long=long).shape
   (64, 194)

:func:`~call_report.fca.convert_long_format_to_code_grain_format` does the
same for the code grain. The long format already carries ``code_column`` and
``code_value``, so the code grain is the pivot that keeps them as row keys:

.. doctest::

   >>> from call_report.fca import convert_long_format_to_code_grain_format
   >>> convert_long_format_to_code_grain_format(long=long).shape
   (2240, 8)

Single-occurrence rows are dropped on the way, since they have no code to
key on. A long frame with no multiple-occurrence rows at all raises
:class:`~call_report.exceptions.ReshapeError` rather than returning a frame
of bare row keys.

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
