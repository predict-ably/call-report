.. _user_guide_schema_and_metadata:

=============================
Schemas and Schedule Metadata
=============================

``call-report`` holds two separate answers to "what fields does this
schedule have", and keeping them distinct is what makes them useful.

- The **canonical metadata** is what the package believes a schedule looks
  like across its whole published history. It ships with the package,
  generated from FCA's own archives, and is available whether or not you
  have any release files.
- A **layout** is what one release actually declared. It comes from the
  ``D_<ROOT>.TXT`` file inside that quarter's release, so it reflects that
  quarter and nothing else.

Both are reachable from :class:`~call_report.fca.FCACallReport`, and both
speak the same vocabulary (:class:`~call_report.core.FieldSchema`), so they
can be compared directly.

Every example on this page uses
:class:`~call_report.fca.transport.PackagedArchiveTransport`, which reads
the release zips checked into this repository, so nothing here needs a
download:

.. doctest::

   >>> from call_report.fca import FCACallReport
   >>> from call_report.fca.transport import PackagedArchiveTransport
   >>> report = FCACallReport(
   ...     start="2014-12-31", end="2015-03-31", transport=PackagedArchiveTransport()
   ... )

A schedule's whole history
==========================

:meth:`~call_report.fca.FCACallReport.get_file_metadata` returns a
schedule's canonical, cross-time :class:`~call_report.core.FileMetadata`.
It does not depend on ``start``, ``end``, or ``transport``, so it answers
even before any file has been read:

.. doctest::

   >>> metadata = report.get_file_metadata(schedule="RI")
   >>> metadata.name
   'RI'
   >>> metadata.first_period.label, metadata.last_period.label
   ('2000Q1', '2026Q1')
   >>> len(metadata.file_schema)
   46

RI has been published every quarter since 2000, but that does not mean its
columns held still. ``changed`` reports whether any field's presence
differs from the file's own:

.. doctest::

   >>> metadata.changed
   True

Each field carries the periods it was actually present for, so you can see
which ones came and went. ``NONTEMPIMPAIRN`` stopped being reported after
2014:

.. doctest::

   >>> impair = metadata.file_schema["NONTEMPIMPAIRN"]
   >>> impair.first_period.label, impair.last_period.label
   ('2000Q1', '2014Q4')

That is why ``len(metadata.file_schema)`` is 46 while no single quarter of
RI ever had 46 columns. The cross-time schema is the union of every field
the schedule has ever had.

A single quarter
================

:meth:`~call_report.fca.FCACallReport.get_schema` narrows that history to
one quarter, returning a :class:`~call_report.core.FieldSchema` of just the
fields present then:

.. doctest::

   >>> schema = report.get_schema(schedule="RI", period="2014-12-31")
   >>> len(schema)
   44
   >>> schema.names[:6]
   ('SYSTEM', 'DIST', 'ASSOC', 'MONTH', 'YEAR', 'UNINUM')

The snapshot is historically accurate, not merely filtered. Each field is
narrowed to the one version that applied at that date, so a field revised
later still shows the definition in force at the quarter you asked for:

.. doctest::

   >>> provisions = schema["PROVLNS"]
   >>> len(provisions.versions)
   1
   >>> provisions.versions[0].definition
   'Provisions for Losses on Loans,Sales Contracts,Notes,and Leases'
   >>> provisions.versions[0].periods[0].label
   '2014Q4'

``FieldSchema`` also exposes a plain narwhals schema, mapping name to
dtype, for code that only cares about types:

.. doctest::

   >>> schema.schema["PROVLNS"]
   Int64

Every quarter in range
======================

Omit ``period`` to get one schema per fetched quarter that has the
schedule, keyed by :class:`~call_report.core.ReportingPeriod`:

.. doctest::

   >>> schemas = report.get_schema(schedule="RI")
   >>> sorted(period.label for period in schemas)
   ['2014Q4', '2015Q1']

This is the quickest way to see a schedule's width move over a range:

.. doctest::

   >>> {period.label: len(schema) for period, schema in schemas.items()}
   {'2014Q4': 44, '2015Q1': 45}

:meth:`~call_report.fca.FCACallReport.get_layout` has the same shape, so
the two can be used interchangeably.

What a release actually declared
================================

:meth:`~call_report.fca.FCACallReport.get_layout` reads the release's own
layout file, and
:meth:`~call_report.fca.layout.FCALayout.to_field_schema` converts it into
the same ``FieldSchema`` vocabulary the canonical metadata uses. A layout
describes exactly one release and carries no period of its own, so the
period has to be supplied:

.. doctest::

   >>> layout = report.get_layout(schedule="RI", period="2015-03-31")
   >>> declared = layout.to_field_schema(period="2015-03-31")
   >>> len(declared)
   45

Comparing the two views
=======================

Because both sides are a ``FieldSchema``,
:meth:`~call_report.core.FieldSchema.compare` puts them side by side. This
is the cheapest way to notice a release whose layout disagrees with the
shipped metadata:

.. doctest::

   >>> canonical = report.get_schema(schedule="RI", period="2015-03-31")
   >>> declared.compare(other=canonical).is_empty
   True

An empty diff means the package's belief about 2015Q1 matches what FCA
actually published that quarter. A non-empty one is worth investigating: it
means the shipped metadata has drifted from the releases it was generated
from.

Tracking drift across quarters
==============================

The same comparison across two *different* quarters shows how a schedule
evolved. RI changed between 2014Q4 and 2015Q1:

.. doctest::

   >>> before = report.get_schema(schedule="RI", period="2014-12-31")
   >>> after = report.get_schema(schedule="RI", period="2015-03-31")
   >>> drift = before.compare(other=after)
   >>> drift.added
   ('NONTEMPIMPAIR', 'ProvDebtSec')
   >>> drift.removed
   ('NONTEMPIMPAIRN',)

``changed`` holds the fields present in both but not identical, each as a
:class:`~call_report.core.FieldChange` carrying the before and after
metadata. FCA rewrote ``PROVLNS``'s definition in that same quarter:

.. doctest::

   >>> change = next(item for item in drift.changed if item.name == "PROVLNS")
   >>> change.before.versions[0].definition
   'Provisions for Losses on Loans,Sales Contracts,Notes,and Leases'
   >>> change.after.versions[0].definition
   'Provisions for credit losses: On loans, sales contracts, notes, and leases'

One thing to know when comparing two quarters this way: each snapshot
stamps its fields with its own quarter, so every field common to both
quarters lands in ``changed`` on the strength of that stamp alone, whether
or not its dtype or definition moved:

.. doctest::

   >>> len(drift.changed)
   43

Read ``added`` and ``removed`` for which columns came and went. For content
changes, compare the ``dtype`` and ``definition`` on each
``FieldChange``'s ``before`` and ``after``, as above. Comparing a layout
against the canonical schema *for the same quarter*, as in the previous
section, has no such stamp difference, so there ``changed`` means only what
it says.

When metadata and a release disagree
====================================

``get_layout`` and ``get_schema`` answer from different sources, so they can
in principle disagree. If a release contains a schedule the canonical
metadata says was not published that quarter, ``get_schema`` raises
:class:`~call_report.exceptions.PeriodNotAvailableError` rather than
pretending the schedule is missing:

.. code-block:: text

   PeriodNotAvailableError: file 'RCI' was not published as of 2025Q3;
   known periods span 2000Q1 to 2017Q4.

That is a defect in the shipped metadata, and worth reporting. It is
deliberately not reported as
:class:`~call_report.exceptions.ScheduleNotFoundError`, which would claim
the release has no such schedule when it plainly does.

Next steps
==========

- :ref:`getting_started` covers loading the data itself, and reshaping it
  to wide or long format.
- :ref:`api_ref` documents every method used here in full.
