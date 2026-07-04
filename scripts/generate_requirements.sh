#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if ! command -v pip-compile >/dev/null 2>&1; then
    pip install --quiet pip-tools
fi

pip-compile pyproject.toml --all-extras --output-file=requirements.txt

echo "Generated requirements.txt from pyproject.toml"
