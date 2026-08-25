"""Tests for the ``call_report`` package version.

``EXPECTED_VERSION`` is a manual tripwire: bump it in lockstep with
``__version__`` (``src/call_report/__init__.py``) as part of every release, so a
forgotten version bump fails the suite. See ``scripts/tag_release.sh``.
"""

from __future__ import annotations

from packaging.version import Version

import call_report

# Bump in lockstep with __version__ (src/call_report/__init__.py) at release time.
EXPECTED_VERSION = "0.1.0"


def test_version_is_nonempty_string() -> None:
    """``__version__`` is defined as a non-empty string."""
    assert isinstance(call_report.__version__, str)
    assert call_report.__version__


def test_version_is_valid_pep440() -> None:
    """``__version__`` parses as a valid PEP 440 version string."""
    # Version(...) raises InvalidVersion for a malformed string, failing the test.
    assert Version(call_report.__version__).release


def test_version_matches_expected() -> None:
    """``__version__`` matches the manually maintained release version."""
    assert call_report.__version__ == EXPECTED_VERSION
