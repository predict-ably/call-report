"""Guard the shipped FCA institution registry against drift from its sources.

``src/call_report/fca/data/institutions/registry.json`` is generated from
``data/fca-call-report/`` by ``scripts/generate_fca_institution_registry.py``
and is the only one of that script's three files that ships in the wheel.

The failure this catches is silent. A contributor adds a new quarter's zip
to ``data/fca-call-report/`` and does not re-run the script. The shipped
registry then stops a quarter short, and any UNINUM first reported in the
new quarter is missing from it.

Each stage of the pipeline is checked separately, so a failure says which
one moved::

    data/fca-call-report/*.zip
      -> _generate_base()    -> data/fca-institutions/base/registry.json
      -> _apply_overrides()  -> src/call_report/fca/data/institutions/registry.json

Every check runs on every pull request. The full rebuild reads every
archived roster and takes a few seconds, so it carries ``slow`` to stay out
of the ``pytest -m "not slow"`` edit-test loop.
"""

from __future__ import annotations

import pytest

from call_report.core import PeriodRange
from call_report.fca import FCAInstitutionRegistry, get_fca_institution_registry
from call_report.fca.catalog import EARLIEST_PERIOD, LATEST_KNOWN_PERIOD
from call_report.fca.transport import PackagedArchiveTransport
from tests.helpers import load_institution_registry_script

generate = load_institution_registry_script()


def _base() -> FCAInstitutionRegistry:
    """Return the checked-in base registry.

    Returns
    -------
    FCAInstitutionRegistry
        The registry the script built from the archive, before corrections.
    """
    base = generate._load_base()
    assert base is not None, f"{generate._BASE_PATH} is missing."
    registry: FCAInstitutionRegistry = base
    return registry


def test_shipped_file_is_base_plus_overrides() -> None:
    """The shipped file is exactly what the script writes from its inputs.

    Compared as text, so a hand edit, a reformat, or an override added
    without re-running the script all fail here.
    """
    expected = generate._apply_overrides(_base(), generate._load_overrides())
    shipped = generate._SHIPPED_PATH.read_text(encoding="utf-8")
    assert shipped == expected.to_json() + "\n"


def test_loader_serves_the_shipped_file() -> None:
    """get_fca_institution_registry returns what the shipped file holds."""
    shipped = FCAInstitutionRegistry.from_json(
        text=generate._SHIPPED_PATH.read_text(encoding="utf-8")
    )
    assert get_fca_institution_registry() == shipped


def test_shipped_registry_spans_the_catalog() -> None:
    """The shipped registry runs from EARLIEST_PERIOD to LATEST_KNOWN_PERIOD.

    Catches a new quarter added to the archive and catalog without the
    registry being regenerated for it.
    """
    registry = get_fca_institution_registry()
    assert registry.first_period == EARLIEST_PERIOD
    assert registry.last_period == LATEST_KNOWN_PERIOD, (
        f"the shipped registry ends at {registry.last_period.label} but the "
        f"catalog runs through {LATEST_KNOWN_PERIOD.label}; re-run "
        "scripts/generate_fca_institution_registry.py."
    )


@pytest.mark.slow
def test_a_full_rebuild_reproduces_the_base() -> None:
    """Rebuilding from every archived roster reproduces the base exactly.

    The checks above take the base on trust. This one rebuilds it from the
    release zips, so it also catches a republished quarter whose roster
    changed, and any difference between the script's incremental and full
    paths.
    """
    with PackagedArchiveTransport() as transport:
        # Fail rather than skip on an empty archive, since a pass that
        # checked nothing would be misleading.
        assert sorted(transport.archive_root.glob("*.zip")), (
            "No archived release zips found under data/fca-call-report."
        )
        rebuilt = generate._generate_base(
            seed=None,
            periods=list(PeriodRange(start=EARLIEST_PERIOD, end=LATEST_KNOWN_PERIOD)),
            read_roster=generate._read_archived_roster(transport),
        )
    assert generate._audit(rebuilt, _base()), (
        "a full rebuild differs from the checked-in base; re-run "
        "scripts/generate_fca_institution_registry.py --full and review the log."
    )
