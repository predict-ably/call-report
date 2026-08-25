#!/usr/bin/env bash
set -euo pipefail

# Regenerate the committed requirements files from pyproject.toml.
#
# Two files are produced, both committed to the repo so that
# .github/workflows/check-requirements.yml can diff a fresh regeneration
# against them and fail when they drift:
#
#   requirements.txt      every extra, the full resolved environment. Also
#                         what security.yml feeds to pip-audit.
#   requirements-dev.txt  the dev extra alone, for contributors who want
#                         the test and lint toolchain without the docs
#                         stack.
#
# pip-compile reuses the pins already present in an output file when they
# still satisfy pyproject.toml, so regenerating does not churn versions on
# unrelated changes. Pass --upgrade to this script to deliberately refresh
# them.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if ! command -v pip-compile >/dev/null 2>&1; then
    echo "Error: pip-compile not found. Install pip-tools first (pip install pip-tools)." >&2
    exit 1
fi

pip-compile pyproject.toml --all-extras --output-file=requirements.txt "$@"
pip-compile pyproject.toml --extra dev --output-file=requirements-dev.txt "$@"

echo "Generated requirements.txt and requirements-dev.txt from pyproject.toml"
