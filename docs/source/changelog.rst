.. _changelog:

=========
Changelog
=========

All notable changes to ``call-report`` are documented on this page, newest
release first. See the :ref:`release_process` for how this page fits into
cutting a release.

.. _changelog_unreleased:

Unreleased
==========

- ``code_value`` is now a nullable ``Int64`` in long format, the code grain,
  and domain datasets. It was ``Float64``. A plain field's ``code_value`` is
  null. pandas shows it as ``<NA>`` rather than ``NaN``.
  :func:`~call_report.fca.convert_long_format_to_wide_format` and
  :func:`~call_report.fca.convert_long_format_to_code_grain_format` still
  accept a ``Float64`` ``code_value`` holding whole numbers.
- :meth:`~call_report.fca.FCACallReport.to_domain_dataset` with
  ``include_totals=False`` no longer raises under polars 2.0 (:issue:`120`).
- The minimum supported versions are now narwhals 2.10.1, pandas 2.2.0, and
  pyarrow 20.0. polars stays at 1.0. Earlier versions do not work with this
  package (:issue:`125`).
- With pandas 2.x, :meth:`~call_report.fca.FCACallReport.to_long_format`
  returns a null ``code_column`` when no requested schedule is coded. It
  returned the text ``"None"``.
  :func:`~call_report.fca.convert_long_format_to_code_grain_format` now raises
  :class:`~call_report.exceptions.ReshapeError` for such a frame, as
  documented (:issue:`125`).
- The FCA archive now covers 2026Q2. It also holds FCA's 2026-08-31
  regeneration of 2023Q2 through 2026Q1, which amends a few values and leaves
  some fields empty that were ``0``. The shipped
  schedule metadata and institution registry are regenerated to match. The
  regenerated releases show six institutions' current addresses in every one
  of those quarters. The registry keeps the addresses the institutions
  reported at the time (:issue:`130`).
- Every transport now has an ``available_periods()`` method, which returns the
  periods it can supply. :meth:`~call_report.fca.FCACallReport.fetch` accepts
  a range that starts and ends within those periods, and
  :meth:`~call_report.fca.FCACallReport.available_periods` returns them. So a
  quarter extracted into a folder, or added to an archive, can be read without
  a new package release (:issue:`119`).
- **Breaking:** a transport written for 0.1 needs an ``available_periods()``
  method to work with :class:`~call_report.fca.FCACallReport`. See
  :class:`~call_report.fca.transport.FCATransport` (:issue:`119`).
- **Breaking:** ``call_report.fca.catalog.LATEST_KNOWN_PERIOD`` and
  ``call_report.fca.catalog.EARLIEST_PERIOD`` are removed.
  :func:`~call_report.fca.catalog.construct_fca_download_url` accepts any
  quarter from 2000Q1 through the quarter containing today (:issue:`119`).
- :class:`~call_report.fca.transport.PackagedArchiveTransport` raises
  :class:`~call_report.exceptions.ZipArchiveUnpackingError` for a zip it
  cannot unpack, including one with a member whose path would land outside
  its folder. :meth:`~call_report.fca.FCACallReport.fetch` records the error
  in ``errors_`` (:issue:`119`).
- New :func:`~call_report.fca.catalog.fca_release_stem` and
  :func:`~call_report.fca.catalog.parse_fca_release_filename` convert between
  a period and the name of its FCA release zip (:issue:`119`).
- New :class:`~call_report.fca.catalog.FCAReleaseCatalog` lists the FCA
  release zips a source holds and the URL of each. It reads FCA's download
  page, or this project's GitHub mirror of every release, which also gives
  each zip's SHA-256. New
  :func:`~call_report.fca.catalog.construct_fca_mirror_url` builds the mirror
  URL of a period's zip (:issue:`119`).
- New :func:`~call_report.core.default_download_dir` gives the folder
  downloaded files are kept in. Set the ``CALL_REPORT_DOWNLOAD_DIR``
  environment variable to choose another (:issue:`119`).

.. _changelog_0.1.0:

0.1.0 (2026-07-26)
====================

Initial release.

- **Package configuration** (:mod:`call_report.config`): choose the
  dataframe backend a reader returns native frames of (``pandas``,
  ``polars``, or ``pyarrow``), and whether frames are returned eager or
  lazy, via :func:`~call_report.config.get_config`,
  :func:`~call_report.config.set_config`, and
  :func:`~call_report.config.config_context`.
- **Shared, source-agnostic core vocabulary** (:mod:`call_report.core`):
  the calendar-quarter reporting-period objects
  :class:`~call_report.core.ReportingPeriod` and
  :class:`~call_report.core.PeriodRange`, the cross-source
  :class:`~call_report.core.Quarter`, :class:`~call_report.core.Source`,
  and :class:`~call_report.core.FileType` enums, and
  :class:`~call_report.core.BaseCallReport`, the abstract, scikit-learn-style
  interface every source-specific entry point implements.
- A shared exception hierarchy (:mod:`call_report.exceptions`).
- **The first regulatory source**, :mod:`call_report.fca`:
  :class:`~call_report.fca.FCACallReport` reads already-extracted Farm
  Credit Administration Call Report releases from a local directory, with
  lower-level building blocks (layout parsing, schedule and institution
  data reading, and pluggable transports) available on their own. Live
  downloading directly from FCA is not yet supported; this repository
  ships every quarterly release since 2000 as a packaged archive, usable
  immediately via
  :class:`~call_report.fca.transport.PackagedArchiveTransport`.
- Supports Python 3.11 through 3.14 on Linux, macOS, and Windows.

Live downloading from FCA and the FFIEC, FDIC, and NCUA sources are
planned for later releases -- see the project's ``CLAUDE.md`` for the
current roadmap.
