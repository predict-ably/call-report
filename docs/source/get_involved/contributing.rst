.. _how_to_contribute:

=================
How to Contribute
=================

This page covers the workflow for proposing a change, once you have a
:ref:`development environment set up <dev_install>`.

Finding something to work on
=============================

- Browse `open issues <https://github.com/predict-ably/call-report/issues>`_,
  especially any labeled as a good starting point.
- For a new idea or a larger change, please open an issue describing it
  before writing code -- this avoids duplicated effort and lets maintainers
  weigh in on the approach early.
- See the project's ``CLAUDE.md`` (at the repository root) for the current
  release roadmap and architectural conventions.

Making a change
================

1. Create a branch for your change (see :ref:`dev_install`).
2. Make your change in small, well-tested increments. Add or update tests
   alongside every change -- see :ref:`code_standards` for the project's
   100% coverage expectation, and :ref:`design_patterns` for the
   architectural conventions to follow (especially when adding a new
   source module).
3. Update any affected docstrings and, if you touched public API, update
   :ref:`api_ref` and this documentation site.
4. Run the full local check suite (see :ref:`dev_install`) and make sure it
   passes.
5. Commit your change with a clear, descriptive message.

Opening a pull request
========================

- Push your branch and open a pull request against ``main``.
- Describe *why* the change is needed, not just what it does -- link the
  issue it addresses if there is one.
- Keep pull requests focused: prefer several small, reviewable PRs over one
  large one where practical.
- CI runs the same checks as the local suite (tests, coverage, ``ruff``,
  ``mypy``, ``pre-commit`` hooks); please make sure these pass before
  requesting review.
- Be responsive to review feedback -- reviewers are trying to help land your
  change, not just find problems with it.

Reporting bugs and requesting features
========================================

Please use `GitHub issues <https://github.com/predict-ably/call-report/issues>`_
for both. For bug reports, a minimal, reproducible example is the single
most helpful thing you can include.

Reporting a security vulnerability
=====================================

Please do **not** open a public issue for a security vulnerability. See
`SECURITY.md
<https://github.com/predict-ably/call-report/blob/main/SECURITY.md>`_ in the
repository root for how to report it privately.
