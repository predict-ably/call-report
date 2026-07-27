.. _code_standards:

==========
Code Style
==========

``call-report`` follows standard Python conventions (`PEP 8
<https://www.python.org/dev/peps/pep-0008/>`_) enforced by a set of
automated tools, all configured in ``pyproject.toml``.

Formatting, linting, and typing
================================

* `ruff <https://docs.astral.sh/ruff/>`_ -- formatting (line length 88,
  double quotes) and linting. The lint rule set is broad: ``E``, ``W``,
  ``F``, ``I`` (import sorting), ``UP`` (modern syntax), ``B``, ``C4``,
  ``SIM``, ``TID``, ``N``, ``A``, ``S`` (bandit security), ``T20`` (no
  stray ``print``), ``PTH`` (use :mod:`pathlib` over ``os.path``), ``RUF``,
  ``D`` (docstrings), and ``Q``.
* `mypy <https://mypy.readthedocs.io/>`_ -- strict type-checking
  (``disallow_untyped_defs``) over ``src`` and ``tests``. Every function and
  method needs type hints.
* `numpydoc <https://numpydoc.readthedocs.io/>`_ -- validates that public
  docstrings follow the NumPy convention; see :ref:`developer_guide_documentation`.
* `pre-commit <https://pre-commit.com/>`_ -- runs all of the above (plus
  ``codespell``, ``doc8``, and ``sphinx-lint``) automatically before each
  commit. Set it up once with ``pre-commit install`` (see :ref:`dev_install`).

Python-specific conventions
=============================

- Python 3.11+ is required, with support maintained through 3.14. Use
  modern typing syntax (``X | None``, ``list[str]``) rather than
  ``typing.Optional``/``typing.List``.
- Type hints should be precise and well-defined rather than defaulting to
  ``Any``: prefer specific types, generics, protocols, unions, and type
  variables that capture the real contract. Reach for ``Any`` only when it
  is genuinely the right choice (e.g. bridging truly dynamic data), and
  narrow it as soon as the real type is known. The package ships a
  ``py.typed`` marker, so its annotations are part of the public contract
  downstream users type-check against.
- Fix the underlying issue rather than suppressing a check. Avoid
  ``# noqa``, ``# type: ignore``, and ``# numpydoc ignore`` unless a check
  is genuinely wrong for that specific line (e.g. two hooks make
  contradictory demands on the same object). When a suppression truly is
  needed, use the most targeted form available and apply it only to the
  exact object in conflict.
- Don't add speculative abstractions, unused flexibility, or defensive
  error handling for scenarios that can't occur. Prefer three similar lines
  over a premature abstraction.

Testing and coverage
=====================

- Tests live in ``tests/`` and run under ``pytest``.
- The coverage gate is set to **100%** (branch coverage included). This is
  the starting goal for every change: cover edge cases and error branches,
  not just the happy path. Add tests alongside every new feature rather
  than after the fact.
- Only fall back from 100% when a line is genuinely not meaningfully
  testable -- and in that case, exclude it explicitly and narrowly (for
  example, ``# pragma: no cover`` on an ``@overload`` or ``Protocol``
  stub's ``...`` body) rather than lowering the gate or leaving real code
  untested.

Dependency management
=======================

- Runtime dependencies are kept minimal and deliberate. `narwhals
  <https://narwhals-dev.github.io/narwhals/>`_ is currently the only hard
  runtime dependency, and the only third-party library that may be
  hard-imported at module scope anywhere in ``src/``.
- Dataframe backends (``pandas``, ``polars``, ``pyarrow``) are reached only
  through narwhals, which imports the configured backend lazily; they stay
  optional install extras and are only ever test/dev dependencies.
- Any other optional dependency must be loaded lazily via the helpers in
  ``call_report._dependencies`` (``import_optional``, ``_lazy_import``,
  ``_LazyModule``) rather than a bare ``import``, following the pattern
  used by `polars' _dependencies.py
  <https://github.com/pola-rs/polars/blob/main/py-polars/src/polars/_dependencies.py>`_.
- Adding any new dependency (runtime, optional, or dev) should be discussed
  first -- see :ref:`how_to_contribute`.

Running the checks
====================

See :ref:`dev_install` for the exact commands (``pytest``, ``ruff``,
``mypy``, ``pre-commit``) to run locally before opening a pull request.
