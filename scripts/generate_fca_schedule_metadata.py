"""Generate the canonical, shipped FCA schedule metadata.

Maintainer tool, not part of the package or its runtime -- reads the real,
already-checked-in archives under ``data/fca-call-report/`` and produces the
JSON files ``call_report.fca.get_fca_file_metadata`` loads at runtime.

Pipeline, per schedule root (every ``FCASchedule`` plus the institution
roster, ``"INST"``):

1. Scan each period's release (via `parse_layout`/`scan_release`), and
   incrementally build a `FileMetadata`: a field's version is extended in
   place when its dtype/definition stays constant from one quarter to the
   next; a new version opens when it's redefined in place (no presence
   gap) or reappears after a real gap. By default this resumes from the
   previously-generated base rather than reprocessing every period --
   FCA's archived quarters never change once published, so only newly
   added periods need attention. Written, unmodified by any hand
   correction, to ``data/fca-schedule-metadata/base/<root>.json`` (not
   shipped -- this is the mechanically-derived source of truth, kept so
   future runs have something to extend and so the merge below is always
   reproducible).
2. A small, optional, hand-maintained override file at
   ``data/fca-schedule-metadata/overrides/<root>.json`` patches specific
   field-version attributes (typically `definition` text) on top of the
   base. An override naming an unknown field, or a period the base
   doesn't have a version starting at, is a hard error -- this is what
   catches a stale override after a later regeneration shifts version
   boundaries.
3. The merged, authoritative result is written to
   ``src/call_report/fca/data/schedules/<root>.json`` -- the only one of
   the three that ships in the wheel, and the one place in the repo that
   answers "what does the package think this schedule looks like".

Notes
-----
Incrementally extend every schedule through the latest known period::

    python scripts/generate_fca_schedule_metadata.py

Rebuild everything from scratch and audit it against the previously
existing base (a periodic integrity check -- a clean rebuild should
reproduce the incrementally-maintained base exactly)::

    python scripts/generate_fca_schedule_metadata.py --full

Restrict processing to one or more schedules (skips the full-set audit)::

    python scripts/generate_fca_schedule_metadata.py --schedule RCB
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import narwhals as nw

from call_report.core import FieldAttributes, FieldSchema, FieldVersion, FileMetadata
from call_report.core._periods import PeriodRange, ReportingPeriod
from call_report.core._schema import _dtype_from_repr
from call_report.exceptions import SchemaError
from call_report.fca._discovery import scan_release
from call_report.fca.catalog import EARLIEST_PERIOD, LATEST_KNOWN_PERIOD
from call_report.fca.enums import FCASchedule
from call_report.fca.institutions import INSTITUTIONS_ROOT
from call_report.fca.layout import infer_field_dtype, parse_layout
from call_report.fca.transport import PackagedArchiveTransport

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BASE_DIR = _REPO_ROOT / "data" / "fca-schedule-metadata" / "base"
_OVERRIDES_DIR = _REPO_ROOT / "data" / "fca-schedule-metadata" / "overrides"
_SHIPPED_DIR = _REPO_ROOT / "src" / "call_report" / "fca" / "data" / "schedules"

_EXPECTED_ROOTS = frozenset(
    {member.value for member in FCASchedule} | {INSTITUTIONS_ROOT}
)

_log = logging.getLogger("generate_fca_schedule_metadata")


@dataclass
class _OpenVersion:
    """A field version still being extended while scanning periods.

    The mutable counterpart to `call_report.core.FieldVersion`, which is
    frozen and can't have its `end` extended in place.

    Attributes
    ----------
    dtype : narwhals.dtypes.DType
        This version's dtype so far.
    definition : str
        This version's definition text so far.
    start : ReportingPeriod
        The first period this version covers.
    end : ReportingPeriod
        The last period this version covers so far.
    """  # numpydoc ignore=PR01

    dtype: nw.dtypes.DType
    definition: str
    start: ReportingPeriod
    end: ReportingPeriod


@dataclass
class _RootBuilder:
    """The mutable, in-progress metadata for one schedule root.

    Built up period by period via `_extend_file_span`/`_extend_field`,
    then frozen into a real `FileMetadata` by `_finalize`.

    Attributes
    ----------
    root : str
        The schedule root name, e.g. ``"RCB"``.
    file_spans : list[tuple[ReportingPeriod, ReportingPeriod]]
        This root's own publication spans so far, each as a
        ``(start, end)`` pair.
    fields : dict[str, list[_OpenVersion]]
        Each field's versions so far, keyed by name in first-observed
        order.
    """  # numpydoc ignore=PR01

    root: str
    file_spans: list[tuple[ReportingPeriod, ReportingPeriod]] = field(
        default_factory=list
    )
    fields: dict[str, list[_OpenVersion]] = field(default_factory=dict)


def _extend_file_span(builder: _RootBuilder, *, period: ReportingPeriod) -> None:
    """Extend `builder`'s current file-level span, or open a new one after a gap.

    Mirrors `_extend_field`'s contiguity check, but for the root's own
    publication span rather than a single field's.

    Parameters
    ----------
    builder : _RootBuilder
        The root being built; mutated in place.
    period : ReportingPeriod
        The period just observed for this root.
    """
    if builder.file_spans and builder.file_spans[-1][1] == period.previous():
        start, _ = builder.file_spans[-1]
        builder.file_spans[-1] = (start, period)
    else:
        builder.file_spans.append((period, period))


def _extend_field(
    builder: _RootBuilder,
    *,
    name: str,
    dtype: nw.dtypes.DType,
    definition: str,
    period: ReportingPeriod,
) -> None:
    """Extend a field's current version, or open a new one.

    A new version opens either because the field was just redefined in
    place (contiguous with the last version, but different content) or
    because it's reappearing after a real gap (not contiguous, regardless
    of content).

    Parameters
    ----------
    builder : _RootBuilder
        The root being built; mutated in place.
    name : str
        The field's name.
    dtype : narwhals.dtypes.DType
        The field's dtype as observed this period.
    definition : str
        The field's definition as observed this period.
    period : ReportingPeriod
        The period just observed for this field.
    """
    versions = builder.fields.setdefault(name, [])
    if versions:
        last = versions[-1]
        contiguous = last.end.next() == period
        if contiguous and last.dtype == dtype and last.definition == definition:
            last.end = period
            return
    versions.append(
        _OpenVersion(dtype=dtype, definition=definition, start=period, end=period)
    )


def _builder_from_file_metadata(metadata: FileMetadata) -> _RootBuilder:
    """Seed a mutable builder from a previously-generated FileMetadata.

    The inverse of `_finalize`; used to resume incremental generation from
    an existing base rather than reprocessing every period.

    Parameters
    ----------
    metadata : FileMetadata
        A base file previously written by this script.

    Returns
    -------
    _RootBuilder
        A builder ready to have later periods extend it.
    """
    fields = {
        name: [
            _OpenVersion(
                dtype=version.dtype,
                definition=version.definition,
                start=version.periods[0],
                end=version.periods[-1],
            )
            for version in metadata.file_schema[name].versions
        ]
        for name in metadata.file_schema.names
    }
    file_spans = [(span[0], span[-1]) for span in metadata.periods]
    return _RootBuilder(root=metadata.name, file_spans=file_spans, fields=fields)


def _finalize(builder: _RootBuilder) -> FileMetadata:
    """Freeze a mutable builder into an immutable FileMetadata.

    The inverse of `_builder_from_file_metadata`.

    Parameters
    ----------
    builder : _RootBuilder
        The builder to freeze.

    Returns
    -------
    FileMetadata
        The frozen result.
    """
    fields = [
        FieldAttributes(
            name=name,
            versions=tuple(
                FieldVersion(
                    dtype=version.dtype,
                    definition=version.definition,
                    periods=PeriodRange(start=version.start, end=version.end),
                )
                for version in versions
            ),
        )
        for name, versions in builder.fields.items()
    ]
    periods = tuple(
        PeriodRange(start=start, end=end) for start, end in builder.file_spans
    )
    return FileMetadata(
        name=builder.root, periods=periods, file_schema=FieldSchema(fields=fields)
    )


def _load_existing_bases() -> dict[str, FileMetadata]:
    """Load every already-generated base FileMetadata, keyed by root name.

    Reads from `_BASE_DIR`, the non-shipped, mechanically-derived source
    of truth incremental runs extend.

    Returns
    -------
    dict[str, FileMetadata]
        Empty if `_BASE_DIR` doesn't exist yet (the very first bootstrap
        run).
    """
    bases: dict[str, FileMetadata] = {}
    if not _BASE_DIR.is_dir():
        return bases
    for path in sorted(_BASE_DIR.glob("*.json")):
        metadata = FileMetadata.from_json(text=path.read_text(encoding="utf-8"))
        bases[metadata.name] = metadata
    return bases


def _generate_bases(
    *,
    resume_period: ReportingPeriod,
    roots_filter: frozenset[str] | None,
    seed: dict[str, FileMetadata],
) -> dict[str, FileMetadata]:
    """Scan every period from `resume_period` through the latest known one.

    The core of the generation pipeline: for each period, every schedule
    root FCA actually published that quarter is discovered via
    `scan_release`, its layout parsed, and each field's version extended
    or opened via `_extend_field`.

    Parameters
    ----------
    resume_period : ReportingPeriod
        The first period to scan; periods before it are assumed already
        covered by `seed`.
    roots_filter : frozenset[str] or None
        If given, only these roots are scanned/updated; every other
        already-seeded root is returned unchanged.
    seed : dict[str, FileMetadata]
        Previously-generated base metadata to extend, keyed by root name.
        Empty for a from-scratch (``--full``) rebuild.

    Returns
    -------
    dict[str, FileMetadata]
        Every processed root's updated base metadata, keyed by root name.
    """
    builders = {
        root: _builder_from_file_metadata(metadata) for root, metadata in seed.items()
    }
    if resume_period > LATEST_KNOWN_PERIOD:
        _log.info(
            "Already up to date through %s; nothing to do.",
            LATEST_KNOWN_PERIOD.label,
        )
        return {root: _finalize(b) for root, b in builders.items()}

    transport = PackagedArchiveTransport()
    for period in PeriodRange(start=resume_period, end=LATEST_KNOWN_PERIOD):
        release_dir = transport.resolve(period=period)
        manifest = scan_release(release_dir=release_dir)
        for root, files in manifest.items():
            if roots_filter is not None and root not in roots_filter:
                continue
            layout = parse_layout(path=files.layout_path)
            builder = builders.setdefault(root, _RootBuilder(root=root))
            _extend_file_span(builder, period=period)
            for row in layout.variables_as_dicts():
                dtype = infer_field_dtype(
                    var_type=row["type"], decimal_position=row["decimal_position"]
                )
                _extend_field(
                    builder,
                    name=row["name"],
                    dtype=dtype,
                    definition=row["definition"],
                    period=period,
                )
        _log.info("Processed %s (%d root(s) so far).", period.label, len(builders))

    return {root: _finalize(builder) for root, builder in builders.items()}


def _load_overrides(root: str) -> dict[str, Any] | None:
    """Load a root's hand-maintained override file, if one exists.

    Most schedules have none -- overrides only exist where an SME has
    made a specific, targeted correction.

    Parameters
    ----------
    root : str
        The schedule root name.

    Returns
    -------
    dict[str, Any] or None
        The parsed override document, or ``None`` if no override file
        exists for `root`.
    """
    path = _OVERRIDES_DIR / f"{root}.json"
    if not path.is_file():
        return None
    return dict(json.loads(path.read_text(encoding="utf-8")))


def _apply_field_override(
    field_attributes: FieldAttributes, patches: list[dict[str, Any]]
) -> FieldAttributes:
    """Apply one field's override patches, matched by each version's start period.

    The version's `periods` span itself is never overridden -- only its
    `dtype`/`definition` -- keeping this a targeted correction mechanism
    rather than a way to reshape version boundaries.

    Parameters
    ----------
    field_attributes : FieldAttributes
        The base field to patch.
    patches : list[dict[str, Any]]
        Each patch names a ``period_start`` and one or both of `dtype`
        (a narwhals dtype repr, parsed the same way shipped JSON is) and
        `definition`.

    Returns
    -------
    FieldAttributes
        A new instance with the matched version(s) patched.

    Raises
    ------
    SchemaError
        If a patch's `period_start` doesn't match any existing version's
        start.
    """
    versions = list(field_attributes.versions)
    for patch in patches:
        start_key = patch["period_start"]
        index = next(
            (
                i
                for i, version in enumerate(versions)
                if version.periods[0].period_end.isoformat() == start_key
            ),
            None,
        )
        if index is None:
            raise SchemaError(
                f"override for field {field_attributes.name!r} names period_start "
                f"{start_key!r}, which doesn't match any existing version's start "
                "-- the base metadata may have shifted; check the override is "
                "still current."
            )
        old = versions[index]
        new_dtype = _dtype_from_repr(patch["dtype"]) if "dtype" in patch else old.dtype
        new_definition = patch.get("definition", old.definition)
        versions[index] = FieldVersion(
            dtype=new_dtype, definition=new_definition, periods=old.periods
        )
    return FieldAttributes(name=field_attributes.name, versions=tuple(versions))


def _apply_overrides(
    metadata: FileMetadata, overrides: dict[str, Any] | None
) -> FileMetadata:
    """Apply a root's override file (if any) on top of its base FileMetadata.

    Produces the authoritative result written to `_SHIPPED_DIR`; `metadata`
    itself (the base) is never mutated.

    Parameters
    ----------
    metadata : FileMetadata
        The mechanically-derived base metadata.
    overrides : dict[str, Any] or None
        The parsed override document (see `_load_overrides`), or ``None``.

    Returns
    -------
    FileMetadata
        `metadata` unchanged if `overrides` is ``None``; otherwise a new
        instance with the named fields patched.

    Raises
    ------
    SchemaError
        If `overrides` names a field that isn't in `metadata`.
    """
    if not overrides:
        return metadata
    field_overrides: dict[str, Any] = overrides.get("fields", {})
    unknown = sorted(set(field_overrides) - set(metadata.file_schema.names))
    if unknown:
        raise SchemaError(
            f"override(s) for unknown field(s) {unknown!r} in {metadata.name!r}."
        )
    patched_fields = [
        _apply_field_override(field_attributes, field_overrides[field_attributes.name])
        if field_attributes.name in field_overrides
        else field_attributes
        for field_attributes in metadata.file_schema.values()
    ]
    return FileMetadata(
        name=metadata.name,
        periods=metadata.periods,
        file_schema=FieldSchema(fields=patched_fields),
    )


def _write_json(path: Path, metadata: FileMetadata) -> None:
    """Write `metadata` to `path` as JSON, creating parent directories as needed.

    Shared by the base and shipped-authoritative write steps.

    Parameters
    ----------
    path : pathlib.Path
        The destination file.
    metadata : FileMetadata
        The metadata to serialize.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(metadata.to_json() + "\n", encoding="utf-8", newline="\n")


def _audit(
    full_bases: dict[str, FileMetadata], previous_bases: dict[str, FileMetadata]
) -> bool:
    """Compare a fresh full rebuild against the previously-existing base.

    Used by ``--full`` as a periodic integrity check: a clean incremental
    history should always be reproducible from scratch.

    Parameters
    ----------
    full_bases : dict[str, FileMetadata]
        The result of a from-scratch rebuild.
    previous_bases : dict[str, FileMetadata]
        The base metadata that existed before this run, keyed by root
        name.

    Returns
    -------
    bool
        ``True`` if every previously-known root's base is unchanged.
    """
    clean = True
    for root, previous in previous_bases.items():
        current = full_bases.get(root)
        if current is None:
            _log.error(
                "Root %r existed in the previous base but not in this rebuild.", root
            )
            clean = False
            continue
        diff = current.compare(other=previous, check_order=True)
        if not diff.is_empty:
            _log.error("Drift detected for %r: %s", root, diff)
            clean = False
    return clean


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse this script's command-line arguments.

    A thin wrapper around `argparse`, factored out so `main` stays testable.

    Parameters
    ----------
    argv : list[str], optional
        Arguments to parse; defaults to `sys.argv[1:]`.

    Returns
    -------
    argparse.Namespace
        The parsed arguments.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full",
        action="store_true",
        help="Rebuild every period from scratch (ignoring any existing base), then "
        "audit the result against the previously-existing base. Ignored when "
        "--schedule restricts processing, since a partial rebuild can't be "
        "compared against the full previous base.",
    )
    parser.add_argument(
        "--schedule",
        action="append",
        dest="schedules",
        metavar="ROOT",
        help="Restrict processing to this schedule root (repeatable). Omit to "
        "process every schedule.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the generator: parse real archives, apply overrides, ship the result.

    See the module docstring for the full three-stage pipeline this
    orchestrates.

    Parameters
    ----------
    argv : list[str], optional
        Arguments to parse; defaults to `sys.argv[1:]`.

    Returns
    -------
    int
        ``0`` on success; ``1`` if ``--full``'s audit found drift.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parse_args(argv)
    roots_filter = frozenset(args.schedules) if args.schedules else None

    previous_bases = _load_existing_bases()
    if args.full:
        seed: dict[str, FileMetadata] = {}
        resume_period = EARLIEST_PERIOD
    else:
        seed = previous_bases
        resume_period = (
            max(metadata.last_period for metadata in seed.values()).next()
            if seed
            else EARLIEST_PERIOD
        )

    bases = _generate_bases(
        resume_period=resume_period, roots_filter=roots_filter, seed=seed
    )

    if args.full and roots_filter is None and not _audit(bases, previous_bases):
        _log.error(
            "Full rebuild drifted from the existing base -- review before committing."
        )
        return 1

    processed_roots = frozenset(bases)
    if roots_filter is None and processed_roots != _EXPECTED_ROOTS:
        missing = sorted(_EXPECTED_ROOTS - processed_roots)
        extra = sorted(processed_roots - _EXPECTED_ROOTS)
        _log.warning("Root set mismatch. Missing: %s. Unexpected: %s.", missing, extra)

    write_roots = roots_filter if roots_filter is not None else frozenset(bases)
    for root in sorted(write_roots):
        metadata = bases[root]
        _write_json(_BASE_DIR / f"{root}.json", metadata)
        authoritative = _apply_overrides(metadata, _load_overrides(root))
        _write_json(_SHIPPED_DIR / f"{root}.json", authoritative)

    _log.info("Wrote %d schedule(s) to %s.", len(write_roots), _SHIPPED_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
