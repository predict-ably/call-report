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

A fourth shape, the :ref:`curated domain dataset <user_guide_domain_datasets>`,
curates its columns based on subject matter expertise, often combining
information across multiple Call Report schedules.

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

.. _user_guide_domain_datasets:

Curated domain datasets
=======================

The three shapes above name every column after the schedule it came from.
That is faithful, but it means the caller has to know which schedules
compose the view they want, and it splits a series in two whenever FCA
renames a schedule.

:meth:`~call_report.fca.FCACallReport.to_domain_dataset` is the curated
alternative. Which schedules compose the view, which code each row is keyed
by, and what every column is called are all chosen by this package:

.. doctest::

   >>> loans = report.to_domain_dataset(domain_dataset="loan_portfolio")
   >>> loans.shape
   (768, 16)
   >>> list(loans.columns)[:8]  # doctest: +NORMALIZE_WHITESPACE
   ['UNINUM', 'period', 'code_column', 'code_value', 'accruing',
    'accruing_past_due_90', 'allowance', 'charge_off']

Rows are keyed by portfolio, and each column is named for what it measures
with no schedule prefix. Code 110 is agribusiness:

.. doctest::

   >>> agribusiness = loans[(loans["UNINUM"] == 620000) & (loans["code_value"] == 110.0)].iloc[
   ...     0
   ... ]
   >>> float(agribusiness["accruing"]), float(agribusiness["allowance"])
   (3067844.0, 11547.0)

Use :func:`~call_report.fca.get_domain_dataset_codes` to turn the codes into
names:

.. doctest::

   >>> from call_report.fca import get_domain_dataset_codes
   >>> codes = get_domain_dataset_codes(domain_dataset="loan_portfolio")
   >>> codes[codes["code"] == 110]["label"].iloc[0]
   'Agribusiness'

What the curation buys you
--------------------------

**A series that survives a schedule split.** FCA renamed RI-E to RI-E.2 in
2023 while keeping every field name. The dataset draws ``charge_off`` from
whichever of the two covers each period and lands both in one column, so a
range spanning 2023 has no gap. Naming columns after schedules would give
``RIE__charge_off`` up to 2022Q4 and ``RIE2__charge_off`` after it.

**Two measures that must not be added together, kept apart.** RI-E reports
charge-offs gross for most portfolios but net of recoveries for direct loans
to associations (145) and discounted loans to OFIs (150), which have no
recovery figure at all. Those two land in ``net_charge_off``, never in
``charge_off``, and ``net_charge_off`` is computed for every other portfolio
so the column is complete either way.

**Derived columns you would otherwise write yourself.** ``non_performing``
sums accruing loans 90 or more days past due and the two nonaccrual columns.
``non_performing_with_restructured`` adds formally restructured accruing
loans. Neither name asserts that it matches FCA's own definition of a
nonperforming loan; each states its components plainly.

Reported subtotals
------------------

Code 155 is a total RC-F.1 reports itself, not a portfolio. It is excluded
by default, so an aggregation over every returned row does not double count.
Pass ``include_totals=True`` to get it back, for example to compare it
against the sum of the portfolios it totals:

.. doctest::

   >>> report.to_domain_dataset(domain_dataset="loan_portfolio", include_totals=True).shape
   (832, 16)

Filers round their own submissions, so the portfolio rows foot to the total
within a dollar or two rather than exactly.

What the numbers mean over time
-------------------------------

Three boundaries matter when reading a long series, and all three report
null rather than zero where a figure was not collected:

- **2005Q1.** RC-F.1's measures and RI-E's by-portfolio columns begin here.
  Earlier quarters carry rows and codes with no values behind them.
- **2007Q1.** The "Other loans" detail (code 152) begins.
- **2023Q1.** RI-E becomes RI-E.2, and the allowance changes measurement
  basis from incurred loss to current expected credit loss. The
  ``allowance`` column is continuous across that quarter, but the two sides
  of it are not measured the same way.

Wide
----

Pass ``wide=True`` to key rows by ``(UNINUM, period)`` alone, with one
column per ``{code_value}__{measure}`` combination, rather than one row per
portfolio:

.. doctest::

   >>> wide = report.to_domain_dataset(domain_dataset="loan_portfolio", wide=True)
   >>> "110__accruing" in wide.columns
   True
   >>> row = wide[wide["UNINUM"] == 620000].iloc[0]
   >>> float(row["110__accruing"])
   3067844.0

``code_column`` and ``code_value`` are dropped rather than folded into the
name, since a domain dataset only ever declares one ``code_column`` and it
therefore disambiguates nothing once every column already names its own
measure.

Loan performance
-----------------

``loan_performance`` curates RC-F, the whole-book counterpart to
``loan_portfolio``. Rows are keyed by performance status rather than
portfolio:

.. doctest::

   >>> performance = report.to_domain_dataset(domain_dataset="loan_performance")
   >>> list(performance.columns)
   ['UNINUM', 'period', 'code_column', 'code_value', 'not_past_due', 'past_due_30', 'past_due_90', 'total_past_due']

Code 10 is loans currently accruing, and code 54 is nonaccrual loans on a
cash basis:

.. doctest::

   >>> from call_report.fca import get_domain_dataset_codes
   >>> codes = get_domain_dataset_codes(domain_dataset="loan_performance")
   >>> codes[codes["code"].isin([10, 54])][["code", "label"]]
      code                   label
   0    10                Accruing
   2    54  Nonaccrual: Cash basis

This bundle draws on RC-F alone. A later revision may extend it to more
schedules.

RC-F's own reported total (code 60) is the only subtotal here, excluded by
default the same way ``loan_portfolio``'s code 155 is:

.. doctest::

   >>> report.to_domain_dataset(domain_dataset="loan_performance").shape
   (295, 8)
   >>> report.to_domain_dataset(domain_dataset="loan_performance", include_totals=True).shape
   (354, 8)

Code 80 ("Number of loans") shares its column names with the dollar-valued
codes but reports a loan count. That is not a special case for the reshape:
the code value is what tells a reader the unit, exactly as it does for every
other code, and a null appears in the aging-bucket columns that a count has
no version of:

.. doctest::

   >>> counted = report.to_domain_dataset(
   ...     domain_dataset="loan_performance", include_totals=True
   ... )
   >>> row = counted[(counted["UNINUM"] == 620000) & (counted["code_value"] == 80.0)].iloc[0]
   >>> bool(row["not_past_due"] != row["not_past_due"]), float(row["total_past_due"])
   (True, 18520.0)

No derived ``non_performing`` column ships here. Compute it from the wide
shape instead:

.. doctest::

   >>> wide = report.to_domain_dataset(domain_dataset="loan_performance", wide=True)
   >>> row = wide[wide["UNINUM"] == 620000].iloc[0]
   >>> non_performing = (
   ...     row["10__past_due_90"] + row["54__total_past_due"] + row["56__total_past_due"]
   ... )
   >>> float(non_performing)
   64834.0

Allowance for credit losses
----------------------------

``allowance_for_credit_losses`` curates the institution-level allowance
rollforward. Rows are keyed by rollforward stage:

.. doctest::

   >>> allowance = report.to_domain_dataset(domain_dataset="allowance_for_credit_losses")
   >>> list(allowance.columns)
   ['UNINUM', 'period', 'code_column', 'code_value', 'afs_debt_securities', 'htm_debt_securities', 'loans_and_leases']

Code 10 is the beginning balance and code 70 is the ending balance:

.. doctest::

   >>> codes = get_domain_dataset_codes(domain_dataset="allowance_for_credit_losses")
   >>> codes[codes["code"].isin([10, 70])][["code", "label"]]
      code              label
   0    10  Beginning balance
   6    70     Ending balance

This dataset spans the 2023 split between RC-I.E and RC-I.E.1. RC-I.E.1
reports the rollforward as a code (``ACLCode``) with a column per asset
class. RC-I.E reported only ``loans_and_leases``, as separately named
fields rather than a code. The two cannot be grouped into one source the
way ``loan_portfolio`` groups RI-E and RI-E.2, since a source has one
`code_column` setting and RC-I.E.1 has one while RC-I.E does not. RC-I.E's
source declares `continues` instead, keeping ``loans_and_leases``
continuous across the split all the same:

.. doctest::

   >>> row = allowance[
   ...     (allowance["UNINUM"] == 620000) & (allowance["code_value"] == 70.0)
   ... ].iloc[0]
   >>> float(row["loans_and_leases"])
   40396.0

``htm_debt_securities`` and ``afs_debt_securities`` are RC-I.E.1 detail
with no RC-I.E counterpart, so they are null before 2023.
Capital
--------

``capital`` curates RI-D, the quarterly rollforward of an institution's net
worth. Rows are keyed by the change that moved it:

.. doctest::

   >>> capital = report.to_domain_dataset(domain_dataset="capital")
   >>> codes = get_domain_dataset_codes(domain_dataset="capital")
   >>> codes[codes["code"].isin([35, 85])][["code", "label"]]
      code                                      label
   2    35  Net income and other comprehensive income
   6    85                           Equities retired

Code 10 is the beginning balance and code 130 the ending balance. The codes
between them are the movements that separate the two.

RI-D renumbered its codes and renamed, split, or retired most of its columns
at 2017Q1, keeping its own name throughout. This dataset states the crosswalk
across that boundary, so a series runs from 2000 to the present without a
break. One quarter's ending balance equals the next quarter's beginning
balance across it:

.. doctest::

   >>> spanning = FCACallReport(
   ...     start="2016-12-31", end="2017-03-31", transport=PackagedArchiveTransport()
   ... )
   >>> across = spanning.to_domain_dataset(domain_dataset="capital")
   >>> across = across[across["UNINUM"] == 620000]
   >>> ending = across[
   ...     (across["period"] == "2016-12-31") & (across["code_value"] == 130.0)
   ... ].iloc[0]
   >>> beginning = across[
   ...     (across["period"] == "2017-03-31") & (across["code_value"] == 10.0)
   ... ].iloc[0]
   >>> float(ending["capital_stock"]), float(beginning["capital_stock"])
   (351155.0, 351155.0)
   >>> float(ending["total_net_worth"]), float(beginning["total_net_worth"])
   (2225248.0, 2225248.0)

``paid_in_capital``, ``allocated_surplus_qualified``, and ``total_net_worth``
kept their names across the boundary. Four columns did not.
``unallocated_retained_earnings`` and
``accumulated_other_comprehensive_income`` were renamed, and
``capital_stock`` and ``allocated_surplus_nonqualified`` were each split into
parts. From 2017Q1 those two are computed from the parts, which stay
available as columns of their own:

.. doctest::

   >>> parts = [
   ...     "capital_stock_purchased",
   ...     "capital_stock_allocated",
   ...     "preferred_stock_perpetual",
   ...     "preferred_stock_other",
   ... ]
   >>> float(sum(beginning[part] for part in parts))
   351155.0

Before 2017Q1 those columns are null and ``capital_stock`` is RI-D's own
combined figure. ``surplus_reserve`` is the reverse: RI-D reported it through
2016Q4 and no column replaced it, so it is null from 2017Q1 onward and absent
from a frame covering only later periods.

.. doctest::

   >>> "surplus_reserve" in capital.columns
   False

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
