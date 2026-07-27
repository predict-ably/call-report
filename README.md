# call-report

[![tests](https://github.com/predict-ably/call-report/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/predict-ably/call-report/actions/workflows/test.yml)
[![pre-commit](https://github.com/predict-ably/call-report/actions/workflows/pre-commit.yml/badge.svg?branch=main)](https://github.com/predict-ably/call-report/actions/workflows/pre-commit.yml)
[![codecov](https://codecov.io/gh/predict-ably/call-report/branch/main/graph/badge.svg)](https://codecov.io/gh/predict-ably/call-report)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

`call-report` is a Python package for working with regulatory call report data filed by regulated U.S. financial institutions, including FFIEC call reports (banks), NCUA call reports (credit unions), and FCA call reports (Farm Credit System institutions). It aims to provide a consistent interface for retrieving, parsing, and analyzing each regulator's call report data and related regulatory filings.

## Installation

The package is not yet published to PyPI. To install it for development, clone the repository and install it in editable mode along with its development dependencies:

```bash
pip install -e ".[dev]"
```

Once published, it will be installable with:

```bash
pip install call-report
```

## License

This project is licensed under the BSD 3-Clause License — see the [LICENSE](LICENSE) file for details.
