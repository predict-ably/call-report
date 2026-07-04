#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INIT_FILE="$REPO_ROOT/src/call_report/__init__.py"

PYTHON_BIN="python3"
if ! command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python"
fi

VERSION="$("$PYTHON_BIN" -c "
import re
with open('$INIT_FILE') as f:
    content = f.read()
match = re.search(r'__version__\s*=\s*[\"\']([^\"\']+)[\"\']', content)
if not match:
    raise SystemExit('Could not find __version__ in $INIT_FILE')
print(match.group(1))
")"

TAG="v${VERSION}"
if [[ "${1:-}" == "--test" ]]; then
    TAG="${TAG}-test"
fi

if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
    echo "Error: tag '$TAG' already exists locally. Did you forget to bump __version__ in src/call_report/__init__.py?" >&2
    exit 1
fi

if git ls-remote --exit-code --tags origin "refs/tags/$TAG" >/dev/null 2>&1; then
    echo "Error: tag '$TAG' already exists on origin. Did you forget to bump __version__ in src/call_report/__init__.py?" >&2
    exit 1
fi

git tag "$TAG"
git push origin "$TAG"
echo "Created and pushed tag $TAG"
