#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if ! command -v pip-compile >/dev/null 2>&1; then
    echo "Error: pip-compile not found. Install pip-tools first (pip install pip-tools)." >&2
    exit 1
fi

pip-compile pyproject.toml --all-extras --output-file=requirements.txt

echo "Generated requirements.txt from pyproject.toml"
