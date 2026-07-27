#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INIT_FILE="$REPO_ROOT/src/call_report/__init__.py"

# On Windows (Git Bash/MSYS), $INIT_FILE is a POSIX-style path (e.g. /c/...),
# but the available Python interpreters are native Windows builds that don't
# understand it and silently resolve it against the wrong drive. Convert to a
# native Windows path (still forward-slash, so it's safe inside the Python
# string below) whenever cygpath is available; other platforms are unaffected.
PY_INIT_FILE="$INIT_FILE"
if command -v cygpath >/dev/null 2>&1; then
    PY_INIT_FILE="$(cygpath -m "$INIT_FILE")"
fi

PYTHON_BIN="python3"
if ! command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python"
fi

VERSION="$("$PYTHON_BIN" -c "
import re
with open('$PY_INIT_FILE') as f:
    content = f.read()
match = re.search(r'__version__\s*=\s*[\"\']([^\"\']+)[\"\']', content)
if not match:
    raise SystemExit('Could not find __version__ in $PY_INIT_FILE')
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
