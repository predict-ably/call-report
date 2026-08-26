.. _developer_guide_documentation:

=======================
Documentation Style
=======================

Docstring conventions
=======================

``call-report`` follows the `NumPy docstring format
<https://numpydoc.readthedocs.io/en/latest/format.html>`_, validated by the
`numpydoc`_ pre-commit hook (configured in ``pyproject.toml``'s
``[tool.numpydoc_validation]`` section).

- Public classes, functions, and methods need a complete docstring: a
  one-line summary, ``Parameters``, ``Returns`` (or ``Yields``), ``Raises``
  where applicable, and an ``Examples`` section. Dunder methods (e.g.
  ``__repr__``, ``__len__``) don't need their own ``Examples`` section --
  their behavior is usually already covered by the class's own example or
  another method's.
- ``Examples`` sections are executed as doctests via ``pytest
  --doctest-modules`` (see :ref:`code_standards`), not just illustrative
  prose -- construct real objects and show real output that actually
  passes. Prefer ``# doctest: +ELLIPSIS`` over ``# doctest: +SKIP`` for
  output that's correct but inherently variable (a temp path, a memory
  address); reserve ``+SKIP`` for examples that truly cannot run in a
  sandboxed test.
- Test functions (matched by ``^test_``/``\.test_``) and ``__init__.py``
  modules are excluded from validation, since their purpose is already
  clear from their name and module docstring.
- Only write a comment or extended docstring passage when the *why* is
  non-obvious -- a hidden constraint, a subtle invariant, or behavior that
  would otherwise surprise a reader. Don't restate what well-named
  identifiers and type hints already communicate.

Cross-referencing
==================

Use Sphinx cross-reference roles rather than plain text or markdown-style
links when referring to another ``call-report`` object:

.. code-block:: rst

   :class:`~call_report.fca.FCACallReport`
   :func:`~call_report.config.get_config`

The leading ``~`` displays only the final component (e.g. ``FCACallReport``)
rather than the fully-qualified path.

Building the documentation
============================

The documentation is built with `Sphinx <https://www.sphinx-doc.org/>`_,
using the `pydata-sphinx-theme
<https://pydata-sphinx-theme.readthedocs.io/>`_ and `numpydoc`_'s Sphinx
extension for rendering docstrings.

1. Install the ``docs`` extra:

   .. code-block:: bash

      pip install -e ".[docs]"

2. Build the HTML site:

   .. code-block:: bash

      sphinx-build -b html docs/source docs/_build/html

3. Open ``docs/_build/html/index.html`` in a browser to preview.

Adding a new page
-------------------

Add a new ``.rst`` file under ``docs/source/`` (or ``docs/source/get_involved/``
for a Get Involved subpage) and link to it from the appropriate ``toctree`` in
an existing page.

Adding a new public API member
--------------------------------

When you add a new public class or function, add it to the relevant
``autosummary`` list in ``docs/source/api_reference.rst`` (see :ref:`api_ref`)
so its docstring is picked up and rendered automatically.

Inherited methods
-------------------

``autodoc`` documents only the members a class defines itself, so a subclass
page would otherwise omit everything it inherits. ``docs/source/conf.py``
collects the classes deriving from
:class:`~call_report.core.BaseCallReport` and the autosummary class template
gives those, and only those, autodoc's ``:inherited-members:`` option. A new
source's entry point therefore documents ``load``, ``load_all``, and the rest
of the shared interface as soon as it subclasses that base, with nothing to
configure.

The option stays off everywhere else on purpose. Several public classes
subclass :class:`~enum.StrEnum`, and documenting their inherited ``str``
methods fails the ``numpydoc`` validation pass and aborts the build.

Style checks
=============

Beyond ``numpydoc``, two additional pre-commit hooks check the ``.rst``
source files themselves:

* `doc8 <https://github.com/PyCQA/doc8>`_ -- line length (88, matching the
  Python line length) and basic reStructuredText style.
* `sphinx-lint <https://github.com/sphinx-contrib/sphinx-lint>`_ -- common
  reStructuredText mistakes.

.. _numpydoc: https://numpydoc.readthedocs.io/en/latest/index.html
