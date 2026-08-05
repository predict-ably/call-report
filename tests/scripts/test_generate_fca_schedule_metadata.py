"""Tests for the FCA schedule metadata generation script.

`scripts/` isn't a Python package, so the script is loaded directly from
its file path via `importlib.util` rather than a normal import. These
tests exercise the script's core logic (the version-builder state
machine, override application, base I/O, and the audit comparison) on
small synthetic inputs -- not the real archives, which are already
exercised by actually running the script (see its own bootstrap run and
``tests/fca/test_release_archive.py``/``tests/fca/test_schedule_metadata.py``
for that real-data coverage).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import narwhals as nw
import pytest

from call_report.core import (
    FieldAttributes,
    FieldSchema,
    FieldVersion,
    FileMetadata,
    PeriodRange,
    ReportingPeriod,
)
from call_report.exceptions import SchemaError

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "generate_fca_schedule_metadata.py"
)


def _load_script_module() -> ModuleType:
    """Load the standalone generation script as an importable module.

    Registered in `sys.modules` before execution -- `dataclasses.dataclass`
    resolves postponed (`from __future__ import annotations`) annotations
    by looking the defining module up there, which a bare
    `module_from_spec`/`exec_module` never populates on its own.
    """
    spec = importlib.util.spec_from_file_location(
        "generate_fca_schedule_metadata", _SCRIPT_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


generate = _load_script_module()

Q1 = ReportingPeriod.from_period_end(value="2020-03-31")
Q2 = ReportingPeriod.from_period_end(value="2020-06-30")
Q5 = ReportingPeriod.from_period_end(value="2021-03-31")  # not contiguous with Q2


def _field(definition: str = "original") -> FieldAttributes:
    """Build a single-version FieldAttributes spanning 2000-2026."""
    span = PeriodRange(start="2000-03-31", end="2026-03-31")
    return FieldAttributes(
        name="INV_CODE",
        versions=(FieldVersion(dtype=nw.Int64(), definition=definition, periods=span),),
    )


# ---------------------------------------------------------------------------
# _extend_field
# ---------------------------------------------------------------------------


def test_extend_field_opens_a_version_for_a_new_field() -> None:
    """A never-seen field name opens its first version."""
    builder = generate._RootBuilder(root="RCB")
    generate._extend_field(
        builder, name="X", dtype=nw.Int64(), definition="def", period=Q1
    )
    assert builder.fields["X"] == [
        generate._OpenVersion(dtype=nw.Int64(), definition="def", start=Q1, end=Q1)
    ]


def test_extend_field_extends_when_contiguous_and_unchanged() -> None:
    """A contiguous period with identical content extends the open version's end."""
    builder = generate._RootBuilder(root="RCB")
    generate._extend_field(
        builder, name="X", dtype=nw.Int64(), definition="def", period=Q1
    )
    generate._extend_field(
        builder, name="X", dtype=nw.Int64(), definition="def", period=Q2
    )
    assert len(builder.fields["X"]) == 1
    assert builder.fields["X"][0].end == Q2


def test_extend_field_opens_a_new_version_on_in_place_redefinition() -> None:
    """A contiguous period with different content opens a new version, not a gap."""
    builder = generate._RootBuilder(root="RCB")
    generate._extend_field(
        builder, name="X", dtype=nw.Int64(), definition="old", period=Q1
    )
    generate._extend_field(
        builder, name="X", dtype=nw.Int64(), definition="new", period=Q2
    )
    versions = builder.fields["X"]
    assert [v.definition for v in versions] == ["old", "new"]
    assert versions[1].start == Q2


def test_extend_field_opens_a_new_version_on_dtype_change() -> None:
    """A contiguous period with a changed dtype (same definition) also redefines."""
    builder = generate._RootBuilder(root="RCB")
    generate._extend_field(
        builder, name="X", dtype=nw.Int64(), definition="d", period=Q1
    )
    generate._extend_field(
        builder, name="X", dtype=nw.Float64(), definition="d", period=Q2
    )
    assert len(builder.fields["X"]) == 2


def test_extend_field_opens_a_new_version_after_a_real_gap() -> None:
    """A non-contiguous period opens a new version, even with identical content."""
    builder = generate._RootBuilder(root="RCB")
    generate._extend_field(
        builder, name="X", dtype=nw.Int64(), definition="def", period=Q1
    )
    generate._extend_field(
        builder, name="X", dtype=nw.Int64(), definition="def", period=Q5
    )
    versions = builder.fields["X"]
    assert len(versions) == 2
    assert versions[0].end == Q1
    assert versions[1].start == Q5


# ---------------------------------------------------------------------------
# _extend_file_span
# ---------------------------------------------------------------------------


def test_extend_file_span_extends_when_contiguous() -> None:
    """A contiguous period extends the current span."""
    builder = generate._RootBuilder(root="RCB")
    generate._extend_file_span(builder, period=Q1)
    generate._extend_file_span(builder, period=Q2)
    assert builder.file_spans == [(Q1, Q2)]


def test_extend_file_span_opens_a_new_span_after_a_gap() -> None:
    """A non-contiguous period opens a new span."""
    builder = generate._RootBuilder(root="RCB")
    generate._extend_file_span(builder, period=Q1)
    generate._extend_file_span(builder, period=Q5)
    assert builder.file_spans == [(Q1, Q1), (Q5, Q5)]


# ---------------------------------------------------------------------------
# _builder_from_file_metadata / _finalize round trip
# ---------------------------------------------------------------------------


def test_builder_round_trips_through_finalize() -> None:
    """Seeding a builder from a FileMetadata and finalizing it reproduces it exactly.

    This is what lets incremental generation resume from an existing base
    rather than reprocessing every period.
    """
    span = PeriodRange(start="2000-03-31", end="2020-03-31")
    field = FieldAttributes(
        name="UNINUM",
        versions=(FieldVersion(dtype=nw.Int64(), definition="", periods=span),),
    )
    original = FileMetadata(
        name="RCB", periods=(span,), file_schema=FieldSchema(fields=[field])
    )
    builder = generate._builder_from_file_metadata(original)
    assert generate._finalize(builder) == original


# ---------------------------------------------------------------------------
# _apply_field_override / _apply_overrides
# ---------------------------------------------------------------------------


def test_apply_field_override_patches_definition() -> None:
    """A patch matching an existing version's start updates its definition."""
    patched = generate._apply_field_override(
        _field(), [{"period_start": "2000-03-31", "definition": "corrected"}]
    )
    assert patched.versions[0].definition == "corrected"
    assert patched.versions[0].dtype == nw.Int64()


def test_apply_field_override_patches_dtype() -> None:
    """A patch can also override dtype, parsed the same way shipped JSON is."""
    patched = generate._apply_field_override(
        _field(), [{"period_start": "2000-03-31", "dtype": "Float64"}]
    )
    assert patched.versions[0].dtype == nw.Float64()
    assert patched.versions[0].definition == "original"


def test_apply_field_override_rejects_unmatched_period_start() -> None:
    """A period_start that doesn't match any version's start is a hard error.

    This is what catches a stale override after a later regeneration
    shifts version boundaries.
    """
    with pytest.raises(SchemaError, match="doesn't match any existing version"):
        generate._apply_field_override(
            _field(), [{"period_start": "1999-03-31", "definition": "x"}]
        )


def test_apply_overrides_returns_metadata_unchanged_when_none() -> None:
    """No override file means the base metadata passes through untouched."""
    metadata = FileMetadata(
        name="RCB",
        periods=(PeriodRange(start="2000-03-31", end="2026-03-31"),),
        file_schema=FieldSchema(fields=[_field()]),
    )
    assert generate._apply_overrides(metadata, None) is metadata


def test_apply_overrides_patches_named_field_only() -> None:
    """Only the named field is patched; every other field passes through unchanged."""
    span = PeriodRange(start="2000-03-31", end="2026-03-31")
    other = FieldAttributes(
        name="UNINUM",
        versions=(FieldVersion(dtype=nw.Int64(), definition="", periods=span),),
    )
    metadata = FileMetadata(
        name="RCB", periods=(span,), file_schema=FieldSchema(fields=[_field(), other])
    )
    overrides = {
        "fields": {"INV_CODE": [{"period_start": "2000-03-31", "definition": "fixed"}]}
    }
    patched = generate._apply_overrides(metadata, overrides)
    assert patched.file_schema["INV_CODE"].versions[0].definition == "fixed"
    assert patched.file_schema["UNINUM"] is other


def test_apply_overrides_rejects_unknown_field() -> None:
    """An override naming a field the base doesn't have is a hard error."""
    span = PeriodRange(start="2000-03-31", end="2026-03-31")
    metadata = FileMetadata(
        name="RCB", periods=(span,), file_schema=FieldSchema(fields=[_field()])
    )
    overrides = {
        "fields": {"NOT_REAL": [{"period_start": "2000-03-31", "definition": "x"}]}
    }
    with pytest.raises(SchemaError, match="unknown field"):
        generate._apply_overrides(metadata, overrides)


# ---------------------------------------------------------------------------
# _write_json / _load_existing_bases / _load_overrides
# ---------------------------------------------------------------------------


def test_write_json_and_load_existing_bases_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A written base file is found and parsed correctly by _load_existing_bases."""
    metadata = FileMetadata(
        name="RCB",
        periods=(PeriodRange(start="2000-03-31", end="2026-03-31"),),
        file_schema=FieldSchema(fields=[_field()]),
    )
    generate._write_json(tmp_path / "RCB.json", metadata)
    assert (tmp_path / "RCB.json").read_text(encoding="utf-8").endswith("\n")

    monkeypatch.setattr(generate, "_BASE_DIR", tmp_path)
    assert generate._load_existing_bases() == {"RCB": metadata}


def test_load_existing_bases_returns_empty_when_dir_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No base directory yet (the very first bootstrap run) returns an empty dict."""
    monkeypatch.setattr(generate, "_BASE_DIR", tmp_path / "does-not-exist")
    assert generate._load_existing_bases() == {}


def test_load_overrides_returns_none_when_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schedule with no override file loads as None."""
    monkeypatch.setattr(generate, "_OVERRIDES_DIR", tmp_path)
    assert generate._load_overrides("RCB") is None


def test_load_overrides_parses_an_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing override file is parsed as JSON."""
    (tmp_path / "RCB.json").write_text(json.dumps({"fields": {}}), encoding="utf-8")
    monkeypatch.setattr(generate, "_OVERRIDES_DIR", tmp_path)
    assert generate._load_overrides("RCB") == {"fields": {}}


# ---------------------------------------------------------------------------
# _audit
# ---------------------------------------------------------------------------


def test_audit_clean_when_rebuild_matches_previous() -> None:
    """Identical rebuild and previous bases audit clean."""
    metadata = FileMetadata(
        name="RCB",
        periods=(PeriodRange(start="2000-03-31", end="2026-03-31"),),
        file_schema=FieldSchema(fields=[_field()]),
    )
    assert generate._audit({"RCB": metadata}, {"RCB": metadata}) is True


def test_audit_detects_drift() -> None:
    """A rebuild that differs from the previous base is flagged, not raised."""
    span = PeriodRange(start="2000-03-31", end="2026-03-31")
    previous = FileMetadata(
        name="RCB", periods=(span,), file_schema=FieldSchema(fields=[_field("old")])
    )
    rebuilt = FileMetadata(
        name="RCB", periods=(span,), file_schema=FieldSchema(fields=[_field("new")])
    )
    assert generate._audit({"RCB": rebuilt}, {"RCB": previous}) is False


def test_audit_detects_a_root_missing_from_the_rebuild() -> None:
    """A root present in the previous base but absent from the rebuild is flagged."""
    metadata = FileMetadata(
        name="RCB",
        periods=(PeriodRange(start="2000-03-31", end="2026-03-31"),),
        file_schema=FieldSchema(fields=[_field()]),
    )
    assert generate._audit({}, {"RCB": metadata}) is False


# ---------------------------------------------------------------------------
# _parse_args
# ---------------------------------------------------------------------------


def test_parse_args_defaults() -> None:
    """With no arguments, --full is False and no schedule filter is set."""
    args = generate._parse_args([])
    assert args.full is False
    assert args.schedules is None


def test_parse_args_full_flag() -> None:
    """--full sets the full flag."""
    assert generate._parse_args(["--full"]).full is True


def test_parse_args_schedule_is_repeatable() -> None:
    """--schedule can be passed multiple times, collecting a list."""
    args = generate._parse_args(["--schedule", "RCB", "--schedule", "RC"])
    assert args.schedules == ["RCB", "RC"]
