.. _design_patterns:

===============
Design Patterns
===============

This page collects the implementation patterns used throughout
``call-report``. Follow them when adding new code -- especially a new
source module (FFIEC, FDIC, NCUA) -- so behavior and API shape stay
predictable across the whole package. It covers *why* the code is shaped
the way it is; see :ref:`code_standards` for the mechanical
formatting/typing/testing rules these patterns sit on top of.

The sklearn-style estimator interface
========================================

Every source's entry point (``FCACallReport`` today; future FFIEC/FDIC/NCUA
equivalents later) subclasses ``BaseCallReport`` and follows the
scikit-learn estimator convention:

- ``__init__`` only stores its parameters, verbatim, as identically-named
  attributes -- no validation, no I/O.
- A separate ``fetch()`` method does the actual validation and resolves
  release files, populating trailing-underscore attributes (``periods_``,
  ``releases_``, ...). ``load``-family methods call it automatically if it
  hasn't run yet.
- ``get_params()``/``set_params()``/``__repr__`` are inherited for free
  from ``BaseCallReport`` by introspecting ``__init__``'s signature -- a
  new source never redeclares them.

Internal hook + ``@final`` public method (template method)
==============================================================

Several public methods (``load``, ``load_all``, ``load_institutions``)
need to apply the *same* cross-cutting behavior across every source --
currently, converting the result to a requested ``dataframe_type`` as a
last step (see the :ref:`dataframe backend abstraction
<dataframe_backend_pattern>` below). Rather than trust every source's
implementation to remember that step, ``BaseCallReport`` splits each such
method in two:

- An ``@abstractmethod`` **internal hook**, prefixed with an underscore
  (``_load``, ``_load_all``, ``_load_institutions``), that a concrete
  source implements: it does the real work and returns a plain,
  unconverted native dataframe (or a ``dict`` of them for ``_load_all``).
- A concrete, ``typing.final``-decorated **public method** on
  ``BaseCallReport`` itself (``load``, ``load_all``, ``load_institutions``)
  that calls the hook and applies the shared behavior -- here,
  ``convert_dataframe_type`` -- exactly once. Because it's ``@final``,
  ``mypy`` rejects any subclass that tries to override it, so the shared
  behavior can't be silently skipped or duplicated by a new source.

.. code-block:: python

   class BaseCallReport(ABC):
       @abstractmethod
       def _load(self, *, schedule: Any) -> Any:
           """Return an already-finalized native dataframe; no conversion."""

       @final
       def load(
           self, *, schedule: Any, dataframe_type: DataFrameType | None = None
       ) -> Any:
           """Public entry point -- cannot be overridden."""
           return convert_dataframe_type(
               data=self._load(schedule=schedule), dataframe_type=dataframe_type
           )

A concrete source implements ``_load`` (and ``_load_all``,
``_load_institutions``) the same way it would have implemented ``load``
before this pattern existed -- the only difference is it drops the
``dataframe_type`` parameter and never calls ``convert_dataframe_type``/
``finalize_as`` itself. ``_load_all`` in particular should build its
per-schedule results by calling ``self._load(schedule=...)`` (the internal
hook), not ``self.load(...)`` (the public, converting wrapper) --
conversion happens exactly once, in ``load_all``'s own ``@final`` body,
applied to every value in the dict it gets back.

Reach for this pattern whenever a *new* piece of behavior needs to apply
uniformly across every ``load``-family method of every source: add the
shared step to the relevant ``@final`` public method on
``BaseCallReport``, not to each source's implementation.

.. _dataframe_backend_pattern:

The narwhals dataframe-backend abstraction
=============================================

No reader-facing function ever hard-imports ``pandas``, ``polars``, or
``pyarrow``; they're reached only through `narwhals
<https://narwhals-dev.github.io/narwhals/>`_, the package's one hard
runtime dependency (see :ref:`code_standards`). The shared primitives live
in ``call_report.core._backend``:

- ``build_frame(data=...)`` -- builds an eager ``narwhals.DataFrame`` from
  columnar data, using whichever backend ``call_report.config.get_config``
  currently names.
- ``finalize(frame=...)`` -- applies the configured laziness and unwraps
  to the native frame callers actually receive; the single point where an
  internal ``narwhals`` frame becomes a public return value.
- ``concat(frames=..., how=...)`` -- stacks multiple periods' frames
  together, reconciling schema differences per ``schema_policy``.
- ``convert_dataframe_type(data=..., dataframe_type=...)`` -- the final,
  optional step described above: converts an already-finalized native
  frame to a specific ``pandas``/``pyarrow_table``/``polars_dataframe``/
  ``polars_lazyframe`` type, independent of which backend built it.
  Zero-copy when the requested type already matches; ``None`` (the
  default) leaves the frame exactly as `finalize` produced it.
- ``finalize_as(frame=..., dataframe_type=...)`` -- ``finalize`` and
  ``convert_dataframe_type`` combined, for standalone parsing functions
  (e.g. ``read_schedule_file``) that aren't behind the ``BaseCallReport``
  template method above and so need to do both steps themselves.

Any new function that returns a dataframe to a caller should accept an
optional ``dataframe_type`` parameter and route through the relevant
combinator above as its last step, rather than returning ``finalize(...)``
directly.

Immutable value objects ("wither" methods)
=============================================

Value-like objects (``ReportingPeriod``, ``PeriodRange``, and the schema
vocabulary ``FieldAttributes``/``FieldSchema``/``FileMetadata``) are
frozen dataclasses (or, for ``FieldSchema``, a hand-written immutable
``Mapping``). They never mutate in place; instead, a method that produces
a "changed" version returns a **new** instance:

- ``FieldSchema.subset(names=...)`` / ``.add_fields(fields=..., index=...)``
  return a new ``FieldSchema``; the original is untouched.
- ``FieldSchema.as_of(period=...)`` / ``FileMetadata.as_of(period=...)``
  return a new, narrowed instance rather than mutating the receiver or
  returning a bare boolean/subset marker.

Follow the same shape for any new value object: validate invariants once,
in ``__post_init__`` (dataclasses) or ``__init__`` (hand-written classes),
and give it "wither" methods that return a new instance rather than
exposing setters.
