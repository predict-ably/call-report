.. _dev_install:

=========================
Developer Installation
=========================

These steps set up a local environment for working on ``call-report``
itself (as opposed to just using the package).

1. Fork and clone the repository, then create a branch for your change:

   .. code-block:: bash

      git clone https://github.com/<your-fork>/call-report.git
      cd call-report
      git checkout -b my-change

2. Install an editable copy of the package with the ``dev`` extra, which
   includes ``pytest``, ``ruff``, ``mypy``, ``pre-commit``, and every
   supported dataframe backend:

   .. code-block:: bash

      pip install -e ".[dev]"

3. Install the ``pre-commit`` hooks so formatting, linting, type-checking,
   and docstring validation run automatically before each commit:

   .. code-block:: bash

      pre-commit install

4. To also build the documentation locally, install the ``docs`` extra (see
   :ref:`developer_guide_documentation`):

   .. code-block:: bash

      pip install -e ".[docs]"

Supported Python versions
==========================

``call-report`` supports Python 3.11 through 3.14, on Linux, macOS, and
Windows.

Running the checks locally
============================

Run the full set of checks the same way CI does before opening a pull
request:

.. code-block:: bash

   pytest --cov=call_report --cov-report=term-missing --cov-fail-under=100
   ruff check .
   ruff format .
   mypy
   pre-commit run --all-files

See :ref:`code_standards` for what each of these checks enforces.
