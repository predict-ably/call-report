# call-report

[![tests](https://github.com/predict-ably/call-report/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/predict-ably/call-report/actions/workflows/test.yml)
[![pre-commit](https://github.com/predict-ably/call-report/actions/workflows/pre-commit.yml/badge.svg?branch=main)](https://github.com/predict-ably/call-report/actions/workflows/pre-commit.yml)
[![codecov](https://codecov.io/gh/predict-ably/call-report/branch/main/graph/badge.svg)](https://codecov.io/gh/predict-ably/call-report)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![docs](https://readthedocs.org/projects/call-report/badge/?version=latest)](https://call-report.readthedocs.io/en/latest/)

`call-report` is a Python package for working with regulatory call report data filed by regulated U.S. financial institutions, including FFIEC call reports (banks), NCUA call reports (credit unions), and FCA call reports (Farm Credit System institutions). It aims to provide a consistent interface for retrieving, parsing, and analyzing each regulator's call report data and related regulatory filings.

## Documentation

Full documentation is hosted at
[call-report.readthedocs.io](https://call-report.readthedocs.io/en/latest/):

- [Getting started](https://call-report.readthedocs.io/en/latest/get_started.html)
  walks through the object-oriented interface, choosing a dataframe backend, and
  loading FCA Call Report data.
- [API reference](https://call-report.readthedocs.io/en/latest/api_reference.html)
  documents every public class and function.
- [Get involved](https://call-report.readthedocs.io/en/latest/get_involved.html)
  covers contributing, the code style, and the release process.
- [Changelog](https://call-report.readthedocs.io/en/latest/changelog.html)
  lists what changed in each release.

## Installation

Install the latest release from PyPI:

```bash
pip install call-report
```

### Development install

To work on `call-report` itself, clone the repository and install it in editable mode along with its development dependencies:

```bash
pip install -e ".[dev]"
```

## License

This project is licensed under the BSD 3-Clause License — see the [LICENSE](LICENSE) file for details.
