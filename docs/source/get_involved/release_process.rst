.. _release_process:

================
Release Process
================

This page walks through cutting a new ``call-report`` release, from bumping
the version to confirming it is live on PyPI and in the docs.

One-time setup (already completed for this repo)
===================================================

These are account/repository settings, not something a workflow file can
do. They were configured before the first release; listed here for
reference in case any of them ever need to be recreated (rotating a
trusted publisher, re-importing the ReadTheDocs project, etc.):

- **TestPyPI trusted publisher.** On the `TestPyPI project settings
  <https://test.pypi.org/manage/account/publishing/>`_, a trusted
  publisher is registered for this repository (``predict-ably/call-report``),
  workflow file ``publish-test.yml``, and environment name ``testpypi``.
- **PyPI trusted publisher.** Same as above on `PyPI
  <https://pypi.org/manage/account/publishing/>`_, but workflow file
  ``publish.yml`` and environment name ``pypi``. PyPI supports registering
  a trusted publisher for a project name that hasn't been published yet
  ("pending" publisher), which is how this one was originally registered
  before the first release.
- **GitHub environments.** Under repository Settings → Environments,
  ``testpypi`` and ``pypi`` exist (they're created automatically the first
  time a workflow references them; consider adding required reviewers to
  the ``pypi`` environment as an extra safety gate before anything
  publishes to real PyPI, if that isn't already in place).
- **ReadTheDocs project.** The repository is imported on `ReadTheDocs
  <https://readthedocs.org/dashboard/>`_, so tagged versions build
  automatically (see :ref:`release_process_docs` below).

Both publish workflows authenticate via `OIDC Trusted Publishing
<https://docs.pypi.org/trusted-publishers/>`_ -- there are no PyPI/TestPyPI
API tokens to create or rotate.

1. Bump the version
=====================

``__version__`` in ``src/call_report/__init__.py`` is the single source of
truth for the package version -- ``hatchling`` reads it directly
(``[tool.hatch.version] path = "src/call_report/__init__.py"`` in
``pyproject.toml``), so nothing in ``pyproject.toml`` itself needs editing.

Three other places need updating in the same commit:

- **``tests/test_version.py``** (required -- CI fails otherwise):
  ``EXPECTED_VERSION`` is a deliberate tripwire (see the comment at the top
  of that file) that fails ``test_version_matches_expected`` if you forget
  to update it.
- **``docs/source/_static/switcher.json``**: add an entry for the new
  version so it appears in the docs version dropdown (see
  :ref:`release_process_docs` below). Doesn't affect the package build, but
  easy to forget if it's not done alongside the version bump.
- **``docs/source/changelog.rst``**: add a new section (newest first)
  summarizing what changed, following the existing ``0.1.0`` entry's
  format. Reference merged PRs/issues/contributors with the ``sphinx_issues``
  roles (``:pr:``, ``:issue:``, ``:user:``) where relevant.

.. code-block:: json

   {
       "name": "0.1.0",
       "version": "v0.1.0",
       "url": "https://call-report.readthedocs.io/en/v0.1.0/"
   }

Add the new entry *before* the existing ``"dev"`` entry so ``"dev"``
(pointing at ``latest``) stays first/default in the dropdown.

.. _release_process_docs:

Why the docs need this
------------------------

``docs/source/conf.py`` builds the ``version_match`` the theme's version
switcher uses to highlight the current version: it's ``"v" + __version__``
for a tagged release (``"latest"`` for local/dev builds). If
``switcher.json`` has no entry whose ``"version"`` equals that string, the
switcher can't find itself in the dropdown. ReadTheDocs builds a new docs
version automatically for every pushed tag once the project is imported
(see the one-time setup above), so the switcher entry just needs its
``url`` to match the RTD-generated URL for that tag
(``https://call-report.readthedocs.io/en/v<version>/``).

2. Run the local checks and merge
====================================

.. code-block:: bash

   pytest --cov=call_report --cov-report=term-missing --cov-fail-under=100
   ruff check .
   ruff format .
   mypy
   pre-commit run --all-files

Commit the version bump (``__init__.py``, ``test_version.py``,
``switcher.json``, ``changelog.rst``) and land it on ``main`` through the normal
:ref:`pull request workflow <how_to_contribute>`.

Name the branch ``release/<version>`` (for example ``release/0.1.0``). The
prefix is what triggers the exhaustive archive regression, described in
:ref:`release_process_exhaustive` below. Watch for ``exhaustive archive
regression`` to appear alongside the usual checks. If it is missing, the
branch name is wrong and the run did not happen.

Always tag from ``main`` after CI (``test``, ``pre-commit``, ``security``,
``exhaustive archive regression``) is green on that commit. The publish
workflows only check that the tag matches ``__version__``, not that tests
passed, so a red ``main`` will happily get published if you tag it.

.. _release_process_exhaustive:

The exhaustive archive regression
-----------------------------------

The pull request suite samples the FCA release archive: the full history
under pandas, 20 seeded periods under all three dataframe backends, and 4
periods compared value-for-value across backends. The exhaustive run does
the whole cross product instead, every published release against every
backend, which takes tens of minutes rather than seconds. It is defined in
``.github/workflows/exhaustive-regression.yml``.

It runs on a pull request in two cases:

- The branch name starts with ``release/``. This makes the release case
  automatic, so it cannot be forgotten.
- The pull request carries the ``run-exhaustive`` label. Apply the label to
  an already-open pull request and the run starts immediately, no push
  needed.

Use the label on any pull request that changes what the archive contains or
how it is parsed. Adding a quarter's zip to ``data/fca-call-report/`` is the
clearest case, and it is not a release, so nothing else would trigger the
run.

The workflow can also be dispatched by hand from the `Actions tab
<https://github.com/predict-ably/call-report/actions/workflows/exhaustive-regression.yml>`_
against any ref, with a Python version and runner of your choosing. Locally,
the same tests run with:

.. code-block:: bash

   pytest tests/fca/test_release_archive.py --run-exhaustive -m exhaustive

3. Publish to TestPyPI first
===============================

Always do a TestPyPI dry run before the real release -- it exercises the
exact same build/publish path (packaging metadata, OIDC auth, the version
check) against a throwaway index, so a packaging mistake surfaces before
it's permanent on real PyPI.

.. code-block:: bash

   ./scripts/tag_release.sh --test

This reads ``__version__``, tags the current commit ``v<version>-test``
(e.g. ``v0.1.0-test``), and pushes the tag, which triggers
``.github/workflows/publish-test.yml``. Watch the
`publish-test workflow runs
<https://github.com/predict-ably/call-report/actions/workflows/publish-test.yml>`_
for it to go green, then verify the install works in a clean environment
(TestPyPI doesn't mirror PyPI, so dependencies like ``narwhals`` need to
resolve from real PyPI via ``--extra-index-url``):

.. code-block:: bash

   python -m venv /tmp/call-report-test-install
   source /tmp/call-report-test-install/bin/activate
   pip install --index-url https://test.pypi.org/simple/ \
       --extra-index-url https://pypi.org/simple/ \
       call-report==<version>
   python -c "import call_report; print(call_report.__version__)"

If anything looks wrong, delete the TestPyPI release/tag, fix the issue,
and re-run this step -- nothing here is user-facing yet.

4. Publish the real release
==============================

.. code-block:: bash

   ./scripts/tag_release.sh

This tags the current commit ``v<version>`` (no ``-test`` suffix) and
pushes it, triggering ``.github/workflows/publish.yml``, which:

1. Rebuilds the distribution and re-checks the tag against ``__version__``.
2. Publishes to PyPI via OIDC.
3. Creates a GitHub release for the tag (auto-generated notes from merged
   PRs since the last release, with the built wheel/sdist attached).

Watch the
`publish workflow runs
<https://github.com/predict-ably/call-report/actions/workflows/publish.yml>`_
for all three jobs to go green.

5. Verify the release
========================

- ``pip install call-report`` in a clean environment and confirm
  ``call_report.__version__`` matches.
- Check the `PyPI project page <https://pypi.org/project/call-report/>`_
  renders the README correctly and lists the right metadata.
- Check ReadTheDocs built the new tagged version and that the version
  switcher dropdown includes and correctly highlights it.
- Check the GitHub release notes are sensible; edit them by hand if the
  auto-generated summary needs cleanup.
