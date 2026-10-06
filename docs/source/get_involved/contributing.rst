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
- Fill in the pull request template that GitHub shows when you open the
  pull request. Delete any section that does not apply.
- Describe *why* the change is needed, not just what it does -- link the
  issue it addresses if there is one.
- Keep pull requests focused: prefer several small, reviewable PRs over one
  large one where practical.
- CI runs the same checks as the local suite (tests, coverage, ``ruff``,
  ``mypy``, ``pre-commit`` hooks, and a strict documentation build); please
  make sure these pass before requesting review.
- A pull request touching ``docs/`` or ``src/call_report/`` also gets a
  hosted Read the Docs preview, linked from the pull request's checks. See
  :ref:`the documentation on pull requests <docs_ci>`.
- Add the ``run-exhaustive`` label if your change touches the FCA release
  archive or the code that reads it. See
  :ref:`the exhaustive archive regression <release_process_exhaustive>` for
  what it does and when it is worth the wait.
- Be responsive to review feedback -- reviewers are trying to help land your
  change, not just find problems with it.

Changes made by AI coding agents
================================

The workflow above is for people. A change made by an AI coding agent,
such as Claude Code, follows a stricter workflow that ``CLAUDE.md`` sets
out in full. Every agent change, however small, goes through three steps.

1. **Plan.** The agent writes a plan for the issue to
   ``plans/plan_issue_<N>.md`` and opens a pull request holding only that
   file. A maintainer reviews the plan and merges it before any code is
   written.
2. **Implement.** A new agent session implements the merged plan on its own
   branch and opens a pull request that links the issue and the plan.
3. **Review.** A review agent checks the implementation against its plan
   and the project conventions, and requests changes where it finds
   problems.

A maintainer still reviews and merges every pull request. If you direct an
agent to work on this repository, point it at ``CLAUDE.md`` and expect a
plan pull request first.

Reporting bugs and requesting features
========================================

Please use `GitHub issues <https://github.com/predict-ably/call-report/issues>`_
for both, and choose the form that fits: a bug report, a data problem, a
feature request, or a documentation improvement. For bug reports, a minimal,
reproducible example is the single most helpful thing you can include.

Reporting a security vulnerability
=====================================

Please do **not** open a public issue for a security vulnerability. See
`SECURITY.md
<https://github.com/predict-ably/call-report/blob/main/SECURITY.md>`_ in the
repository root for how to report it privately.
