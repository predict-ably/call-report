"""Guard the shipped FCA schedule metadata against drift from its sources.

``src/call_report/fca/data/schedules/*.json`` is generated from
``data/fca-call-report/`` by ``scripts/generate_fca_schedule_metadata.py``
and is the only one of the pipeline's three artifacts that ships in the
wheel. Nothing else in the suite checks that the two ends stay in
agreement: :mod:`tests.fca.test_release_archive` parses releases directly
and never consults the shipped JSON.

The failure this exists to catch is silent. A contributor drops a new
quarter's zip into ``data/fca-call-report/``, which ``CLAUDE.md`` documents
as the routine way to update the archive, and does not re-run the
generation script. Every schedule's ``last_period`` is then stale,
`FileMetadata.as_of` returns nothing for the new quarter, and a field
introduced that quarter is missing from the canonical schema entirely.

The pipeline has three stages, and each boundary is checked separately
here so a failure says which one moved::

    data/fca-call-report/*.zip
      -> _generate_bases()   -> data/fca-schedule-metadata/base/<root>.json
      -> _apply_overrides()  -> src/call_report/fca/data/schedules/<root>.json

The base-to-shipped checks and the latest-period sentinel are cheap (no
archive scan) and run on every pull request. Only the full rebuild from
every archived release is expensive, and that one is gated behind
``--run-exhaustive``.

`tests.fca.test_report.test_a_real_release_layout_matches_the_shipped_metadata`
is a related but narrower spot check: one release's layout against the
shipped metadata for three schedules at one period. This module is the
guarantee that check only samples.
"""

from __future__ import annotations

import functools

import pytest

from call_report.core import FileMetadata
from call_report.fca import (
    FCASchedule,
    get_fca_file_metadata,
    get_institutions_file_metadata,
)
from call_report.fca.institutions import INSTITUTIONS_ROOT
from tests.helpers import load_generation_script

generate = load_generation_script()

# Derived from the enum rather than from a glob over the base directory, so
# a base file that goes missing fails these tests instead of silently
# dropping out of the parametrization.
ALL_ROOTS = (*(schedule.value for schedule in FCASchedule), INSTITUTIONS_ROOT)


@functools.cache
def _bases() -> dict[str, FileMetadata]:
    """Return every checked-in base FileMetadata, keyed by root name.

    Cached because `_load_existing_bases` reads and parses all 38 base
    files on every call, and the parametrized tests below would otherwise
    pay that cost once per root.

    Returns
    -------
    dict[str, FileMetadata]
        The mechanically-derived base metadata the script maintains.
    """
    bases: dict[str, FileMetadata] = generate._load_existing_bases()
    return bases


@functools.cache
def _authoritative(root: str) -> FileMetadata:
    """Return one root's base metadata with its overrides applied.

    This is the artifact the generation script writes to `_SHIPPED_DIR`,
    rebuilt here from the two inputs it is derived from. Cached because
    `_load_existing_bases` reads all 38 base files on every call, and the
    parametrized tests below would otherwise re-read them once per root.

    Parameters
    ----------
    root : str
        The schedule root name, or `INSTITUTIONS_ROOT`.

    Returns
    -------
    FileMetadata
        `root`'s base metadata with any override applied.
    """
    return generate._apply_overrides(_bases()[root], generate._load_overrides(root))


def _shipped(root: str) -> FileMetadata:
    """Return one root's shipped metadata through the package's own loader.

    Reading through `get_fca_file_metadata` rather than the JSON file ties
    these checks to what the package actually serves at runtime, not just
    to what is on disk.

    Parameters
    ----------
    root : str
        The schedule root name, or `INSTITUTIONS_ROOT`.

    Returns
    -------
    FileMetadata
        The shipped, canonical metadata for `root`.
    """
    if root == INSTITUTIONS_ROOT:
        return get_institutions_file_metadata()
    return get_fca_file_metadata(schedule=FCASchedule(root))


# ---------------------------------------------------------------------------
# base + overrides == shipped (cheap: no archive scan)
# ---------------------------------------------------------------------------


def test_every_expected_root_has_a_base_file() -> None:
    """The base directory holds exactly one file per schedule plus the roster.

    Without this, a deleted base file would quietly shrink the coverage of
    every parametrized test below rather than failing anything.
    """
    assert set(_bases()) == set(ALL_ROOTS)


@pytest.mark.parametrize("root", ALL_ROOTS)
def test_base_plus_overrides_reproduces_the_shipped_metadata(root: str) -> None:
    """Re-applying the overrides to the base yields what the package serves.

    Catches a partial regeneration: a base edited without re-running the
    script, an override added without regenerating, or a hand-edited
    shipped file. Compared as parsed objects so the failure names the
    fields that moved rather than showing a text diff.
    """
    diff = _authoritative(root).compare(other=_shipped(root), check_order=True)
    assert diff.is_empty, (
        f"{root}: shipped metadata differs from base+overrides: {diff}"
    )


@pytest.mark.parametrize("root", ALL_ROOTS)
def test_shipped_file_text_matches_a_fresh_serialization(root: str) -> None:
    """The shipped JSON is byte-for-byte what the script would write today.

    The parsed comparison above cannot see formatting: a reindented or
    reordered file carries identical metadata. This catches that, and so
    keeps the shipped files reproducible rather than merely equivalent.
    """
    path = generate._SHIPPED_DIR / f"{root}.json"
    assert path.read_text(encoding="utf-8") == _authoritative(root).to_json() + "\n"
