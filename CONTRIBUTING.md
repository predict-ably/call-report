# Contributing to call-report

Thanks for your interest in improving `call-report`! Contributions of every
kind are welcome — bug reports, documentation improvements, and code.

The full contributor guide lives in the **Get Involved** section of the
documentation. Start there:

- [Developer installation](docs/source/get_involved/developer_installation.rst) — set
  up an editable install with the dev tooling.
- [How to contribute](docs/source/get_involved/contributing.rst) — the branch and
  pull-request workflow, and what a change needs before it can be merged.
- [Code style](docs/source/get_involved/code_style.rst) — linting, typing, and
  formatting conventions.
- [Documentation style](docs/source/get_involved/documentation_style.rst) — docstring
  and docs conventions.

To build and read the documentation locally:

```bash
pip install -e ".[docs]"
sphinx-build -b html -W --keep-going docs/source docs/_build/html
```

By participating in this project, you agree to abide by our
[Code of Conduct](CODE_OF_CONDUCT.md).

We look forward to your contributions!
