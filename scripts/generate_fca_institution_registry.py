"""Generate the FCA institution registry that ships with the package.

A maintainer tool, not part of the package or its runtime. It reads FCA's
institution roster (``INST``) from every release checked in under
``data/fca-call-report/`` and produces the JSON file
``call_report.fca.get_fca_institution_registry`` loads at runtime.

The pipeline has three stages, matching
``scripts/generate_fca_schedule_metadata.py``:

1. The rosters are combined into an `FCAInstitutionRegistry`, which records
   each UNINUM's name and address values and the quarters each value was
   reported. The result is written, with no hand corrections, to
   ``data/fca-institutions/base/registry.json``. That file does not ship.
   It is kept so later runs have something to extend and so the next stage
   is always reproducible. By default a run starts from this file and only
   reads the quarters after its last one, because FCA's archived quarters
   do not change once published. ``--full`` rebuilds from every quarter and
   checks the result against the existing file.
2. An optional, hand-maintained file,
   ``data/fca-institutions/overrides/registry.json``, corrects individual
   values on top of the base. See `_apply_overrides` for its format. An
   override that names a UNINUM, a column, or a starting quarter the base
   does not have is an error. That is what catches an override left behind
   after a regeneration changes the history it was written against.
3. The corrected registry is written to
   ``src/call_report/fca/data/institutions/registry.json``, the only one of
   the three files that ships in the wheel.

What the archived rosters contain, and what the code relies on:

- Within one UNINUM, ``SYSTEM``, ``DIST``, and ``ASSOC`` are the same in
  every quarter. Building the registry fails if a new release breaks this.
- A UNINUM can be missing from the roster for some quarters and appear
  again later. Most of these are district-25 institutions that the roster
  lists under a district 23 or 24 UNINUM in 2004 and from 2006Q1 to
  2008Q2. The registry records each UNINUM on its own and does not link
  one UNINUM to another.
- Short names change under an unchanged UNINUM for a handful of UNINUMs.
  The roster alone does not say whether such a change is a rename or a
  different institution using the same UNINUM, and the registry does not
  try to tell them apart.

Notes
-----
Extend the registry through the latest known period::

    python scripts/generate_fca_institution_registry.py

Rebuild from every quarter and check the result against the existing base::

    python scripts/generate_fca_institution_registry.py --full
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from call_report.core._periods import PeriodRange, ReportingPeriod
from call_report.exceptions import InstitutionError
from call_report.fca import FCAInstitution, FCAInstitutionRegistry
from call_report.fca._institution import _VERSIONED_COLUMNS, _build_history
from call_report.fca.catalog import EARLIEST_PERIOD, LATEST_KNOWN_PERIOD
from call_report.fca.institutions import _read_institutions_frame
from call_report.fca.transport import PackagedArchiveTransport

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BASE_PATH = _REPO_ROOT / "data" / "fca-institutions" / "base" / "registry.json"
_OVERRIDES_PATH = (
    _REPO_ROOT / "data" / "fca-institutions" / "overrides" / "registry.json"
)
_SHIPPED_PATH = (
    _REPO_ROOT
    / "src"
    / "call_report"
    / "fca"
    / "data"
    / "institutions"
    / "registry.json"
)

_ROSTER_COLUMNS = ("UNINUM", "SYSTEM", "DIST", "ASSOC", *_VERSIONED_COLUMNS)

_log = logging.getLogger("generate_fca_institution_registry")

RosterReader = Callable[[ReportingPeriod], Iterable[Mapping[str, Any]]]


def _load_base() -> FCAInstitutionRegistry | None:
    """Load the previously generated base registry, if there is one.

    An incremental run extends this registry with the quarters after its
    last one.

    Returns
    -------
    FCAInstitutionRegistry or None
        The base registry, or ``None`` before the first run.
    """
    if not _BASE_PATH.is_file():
        return None
    return FCAInstitutionRegistry.from_json(text=_BASE_PATH.read_text(encoding="utf-8"))


def _rosters_from_registry(
    registry: FCAInstitutionRegistry,
) -> dict[ReportingPeriod, list[dict[str, Any]]]:
    """Turn a registry back into the roster rows it was built from.

    A registry keeps every value for every quarter a UNINUM was reported, so
    this gives back exactly the rows that built it. Extending a registry is
    then a matter of adding new quarters' rows and building again.

    Parameters
    ----------
    registry : FCAInstitutionRegistry
        The registry to unpack.

    Returns
    -------
    dict[ReportingPeriod, list[dict[str, Any]]]
        Each quarter's roster rows.
    """
    rosters: dict[ReportingPeriod, list[dict[str, Any]]] = {}
    for institution in registry.values():
        for span in institution.periods:
            for quarter in span:
                snapshot = institution.as_of(period=quarter)
                row: dict[str, Any] = {
                    "UNINUM": snapshot.uninum,
                    "SYSTEM": snapshot.system,
                    "DIST": snapshot.district,
                    "ASSOC": snapshot.association,
                }
                row.update(
                    {
                        column: getattr(snapshot, stem)
                        for column, stem in _VERSIONED_COLUMNS.items()
                    }
                )
                rosters.setdefault(quarter, []).append(row)
    return rosters


def _generate_base(
    *,
    seed: FCAInstitutionRegistry | None,
    periods: Iterable[ReportingPeriod],
    read_roster: RosterReader,
) -> FCAInstitutionRegistry:
    """Build a registry from a seed registry plus new quarters' rosters.

    The seed is unpacked into its roster rows, the new quarters' rows are
    added, and the registry is built again from all of them.

    Parameters
    ----------
    seed : FCAInstitutionRegistry or None
        A previously generated registry to extend, or ``None`` to start
        from nothing.
    periods : Iterable[ReportingPeriod]
        The quarters to read and add. None of them may already be in
        `seed`.
    read_roster : Callable[[ReportingPeriod], Iterable[Mapping[str, Any]]]
        Returns one quarter's roster rows.

    Returns
    -------
    FCAInstitutionRegistry
        The combined registry.

    Raises
    ------
    InstitutionError
        If a quarter in `periods` is already in `seed`, or if the combined
        rows cannot build a registry.
    """
    rosters: dict[ReportingPeriod, list[Mapping[str, Any]]] = dict(
        _rosters_from_registry(seed) if seed is not None else {}
    )
    for period in periods:
        if period in rosters:
            raise InstitutionError(f"{period.label} is already in the base registry.")
        rosters[period] = list(read_roster(period))
        _log.info("Read the %s roster (%d rows).", period.label, len(rosters[period]))
    return FCAInstitutionRegistry.from_rosters(rosters=rosters.items())


def _read_archived_roster(
    transport: PackagedArchiveTransport,
) -> RosterReader:
    """Return a roster reader backed by the checked-in release archive.

    Each row keeps the roster's ``YEAR`` and ``MONTH`` values, so building
    the registry checks that every roster is labeled with the right quarter.

    Parameters
    ----------
    transport : PackagedArchiveTransport
        The transport that extracts each release.

    Returns
    -------
    Callable[[ReportingPeriod], Iterable[Mapping[str, Any]]]
        A function returning one quarter's roster rows.
    """

    def read(period: ReportingPeriod) -> list[dict[str, Any]]:
        """Return one quarter's roster rows.

        Only the columns the registry needs are kept.

        Parameters
        ----------
        period : ReportingPeriod
            The quarter to read.

        Returns
        -------
        list[dict[str, Any]]
            The quarter's roster rows.
        """
        frame = _read_institutions_frame(release_dir=transport.resolve(period=period))
        return [
            {column: row[column] for column in (*_ROSTER_COLUMNS, "YEAR", "MONTH")}
            for row in frame.rows(named=True)
        ]

    return read


def _load_overrides() -> dict[str, Any] | None:
    """Load the hand-maintained override file, if there is one.

    Most runs have no override file, and the base ships unchanged.

    Returns
    -------
    dict[str, Any] or None
        The parsed override document, or ``None`` if the file does not exist.
    """
    if not _OVERRIDES_PATH.is_file():
        return None
    return dict(json.loads(_OVERRIDES_PATH.read_text(encoding="utf-8")))


def _apply_overrides(
    registry: FCAInstitutionRegistry, overrides: Mapping[str, Any] | None
) -> FCAInstitutionRegistry:
    """Correct individual values in a registry.

    The override document looks like this::

        {
            "institutions": {
                "620000": {"ZIP": [{"period_start": "2013-03-31", "value": "29201"}]}
            }
        }

    Each correction names a UNINUM, a name or address column, and the first
    quarter of the history entry to change, and gives the new value. The
    quarters an entry covers are never changed. If a correction leaves two
    back-to-back entries with the same value, they are merged into one.

    Parameters
    ----------
    registry : FCAInstitutionRegistry
        The base registry. It is not modified.
    overrides : Mapping[str, Any] or None
        The parsed override document, or ``None``.

    Returns
    -------
    FCAInstitutionRegistry
        `registry` itself if there are no overrides, otherwise a corrected
        copy.

    Raises
    ------
    InstitutionError
        If a correction names a UNINUM that is not in `registry`, a column
        that is not a name or address column, or a starting quarter that
        does not begin an entry in that column's history.
    """
    if not overrides:
        return registry
    corrections: Mapping[str, Mapping[str, list[Mapping[str, Any]]]] = overrides.get(
        "institutions", {}
    )
    patched = dict(registry)
    for key, columns in corrections.items():
        uninum = int(key)
        if uninum not in registry:
            raise InstitutionError(
                f"override names UNINUM {uninum}, which is not in the registry."
            )
        patched[uninum] = _apply_institution_overrides(registry[uninum], columns)
    return FCAInstitutionRegistry(institutions=patched.values())


def _apply_institution_overrides(
    institution: FCAInstitution, columns: Mapping[str, list[Mapping[str, Any]]]
) -> FCAInstitution:
    """Apply one UNINUM's corrections.

    Each correction replaces the value of the entry that starts at the
    named quarter. Back-to-back entries left with the same value are merged.

    Parameters
    ----------
    institution : FCAInstitution
        The history to correct. It is not modified.
    columns : Mapping[str, list[Mapping[str, Any]]]
        The corrections, keyed by roster column name.

    Returns
    -------
    FCAInstitution
        The corrected history.

    Raises
    ------
    InstitutionError
        If a column is not a name or address column, or a correction's
        ``period_start`` does not begin an entry in that column's history.
    """
    changes: dict[str, Any] = {}
    for column, patches in columns.items():
        if column not in _VERSIONED_COLUMNS:
            raise InstitutionError(
                f"override for UNINUM {institution.uninum} names column {column!r}, "
                f"which is not one of {', '.join(_VERSIONED_COLUMNS)}."
            )
        history = list(institution._history(column))
        for patch in patches:
            start = patch["period_start"]
            index = next(
                (
                    i
                    for i, version in enumerate(history)
                    if version.periods[0].period_end.isoformat() == start
                ),
                None,
            )
            if index is None:
                raise InstitutionError(
                    f"override for UNINUM {institution.uninum} {column} names "
                    f"period_start {start!r}, but no entry starts there. The base "
                    "may have changed since the override was written."
                )
            history[index] = replace(history[index], value=patch["value"])
        # Rebuilding from per-quarter values merges any back-to-back entries
        # a correction has made equal.
        observations = [
            (quarter, version.value)
            for version in history
            for quarter in version.periods
        ]
        changes[f"{_VERSIONED_COLUMNS[column]}_history"] = _build_history(observations)
    return replace(institution, **changes)


def _write_json(path: Path, registry: FCAInstitutionRegistry) -> None:
    """Write a registry as JSON, creating parent directories as needed.

    Used for both the base and the shipped file, so the two are formatted
    the same way.

    Parameters
    ----------
    path : pathlib.Path
        The file to write.
    registry : FCAInstitutionRegistry
        The registry to write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(registry.to_json() + "\n", encoding="utf-8", newline="\n")


def _audit(rebuilt: FCAInstitutionRegistry, previous: FCAInstitutionRegistry) -> bool:
    """Compare a full rebuild with the previously generated base.

    On a difference, the UNINUMs involved are logged so the change can be
    reviewed before committing.

    Parameters
    ----------
    rebuilt : FCAInstitutionRegistry
        The result of rebuilding from every quarter.
    previous : FCAInstitutionRegistry
        The base that existed before this run.

    Returns
    -------
    bool
        ``True`` if the two are equal.
    """
    if rebuilt == previous:
        return True
    only_rebuilt = sorted(set(rebuilt) - set(previous))
    only_previous = sorted(set(previous) - set(rebuilt))
    changed = sorted(
        uninum
        for uninum in set(rebuilt) & set(previous)
        if rebuilt[uninum] != previous[uninum]
    )
    _log.error(
        "Full rebuild differs from the base. Only in the rebuild: %s. Only in "
        "the base: %s. Different histories: %s.",
        only_rebuilt,
        only_previous,
        changed,
    )
    return False


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse this script's command-line arguments.

    The only option is ``--full``.

    Parameters
    ----------
    argv : list[str], optional
        Arguments to parse. Defaults to ``sys.argv[1:]``.

    Returns
    -------
    argparse.Namespace
        The parsed arguments.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full",
        action="store_true",
        help="Rebuild from every quarter, ignoring the existing base, then check "
        "the result against that base.",
    )
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None, *, read_roster: RosterReader | None = None
) -> int:
    """Run the three-stage pipeline described in the module docstring.

    Both the base and the shipped file are written, unless the ``--full``
    check finds a difference.

    Parameters
    ----------
    argv : list[str], optional
        Arguments to parse. Defaults to ``sys.argv[1:]``.
    read_roster : Callable[[ReportingPeriod], Iterable[Mapping[str, Any]]], optional
        Returns one quarter's roster rows. Defaults to reading the
        checked-in release archive.

    Returns
    -------
    int
        ``0`` on success, ``1`` if the ``--full`` check found a difference.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parse_args(argv)
    previous = _load_base()
    seed = None if args.full else previous
    start = EARLIEST_PERIOD if seed is None else seed.last_period.next()
    periods = (
        list(PeriodRange(start=start, end=LATEST_KNOWN_PERIOD))
        if start <= LATEST_KNOWN_PERIOD
        else []
    )

    if read_roster is None:
        with PackagedArchiveTransport() as transport:
            base = _generate_base(
                seed=seed, periods=periods, read_roster=_read_archived_roster(transport)
            )
    else:
        base = _generate_base(seed=seed, periods=periods, read_roster=read_roster)

    if args.full and previous is not None and not _audit(base, previous):
        _log.error("Review the difference before committing.")
        return 1

    _write_json(_BASE_PATH, base)
    _write_json(_SHIPPED_PATH, _apply_overrides(base, _load_overrides()))
    _log.info(
        "Wrote %d UNINUMs (%s to %s) to %s.",
        len(base),
        base.first_period.label,
        base.last_period.label,
        _SHIPPED_PATH,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
