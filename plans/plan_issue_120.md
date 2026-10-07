# Plan: issue #120, Polars 2.0 breaks is_in_null_safe on the Float64 code_value column

Status: proposed

## Goal

Make `code_value` a nullable 64-bit integer column (`narwhals.Int64`) on every
backend, in place of Float64. Polars 2.0 no longer coerces types for `is_in`, so
the Float64 column fails against the integer codes that `exclude_reported_totals`
and `add_derived_code_rows` test it against. An integer `code_value` removes that
mismatch at its source. It fixes the two failing tests on `main` under polars 1.x
and 2.x alike.

In scope:

1. `code_value` is `Int64` in long format, the code grain, and every domain
   dataset, under pandas, polars (eager and lazy), and pyarrow.
2. The decoding and code remap lookups, and the computed code rows, carry
   integer codes.
3. The public converters accept a long frame whose `code_value` is still a
   whole-number Float64, such as one saved from version 0.1.0, and normalize it
   to `Int64`.
4. A polars test for `add_derived_code_rows`, so its `is_in` call site is
   covered (issue step 2).
5. The doc, docstring, and changelog updates the dtype change needs.
6. A decision on the `polars>=1.0` pin (issue step 4). The plan keeps it as is.

Out of scope:

- Testing under both polars majors. That is #121.
- A scheduled `test` run (issue step 5). See "Open questions".
- The `value` column. It stays Float64.

## Why

### Codes are integers in the data

Every code column in every archived release from 2000Q1 to 2026Q1 is an integer
column with no nulls and no fractional values. A scan of all 105 zips in
`data/fca-call-report/` under the polars backend gives:

| Schedule | Code column | Rows | Min | Max |
|---|---|---:|---:|---:|
| RCB | `INV_CODE` | 309,503 | 10 | 180 |
| RCB2 | `AssetCodeRCB2` | 32,213 | 110 | 510 |
| RCB3 | `DebtMaturityCode` | 19,120 | 110 | 190 |
| RCF | `LOANSTATUS` | 59,112 | 10 | 80 |
| RCF1 | `LOANSTATUS` | 128,076 | 100 | 155 |
| RCI2B | `DerivCode` | 54,970 | 10 | 230 |
| RCI2C | `ExposureCode` | 28,680 | 10 | 140 |
| RCI2D | `DerivRMCode` | 26,290 | 10 | 110 |
| RCO | `ASSET_CODE` | 82,790 | 10 | 120 |
| RCR3 | `RegCapCode` | 40,125 | 100 | 600 |
| RCR7 | `RegCapCode` | 77,562 | 100 | 2100 |
| RID | `CAP_CODE` | 140,142 | 10 | 130 |
| RIE1 | `ACLCode` | 5,985 | 10 | 70 |

The reader already loads each of these as `Int64` on all three backends. The
pandas reader returns pandas' nullable `Int64` extension dtype, just as it does
for `UNINUM`. The package itself also models codes as integers.
`DomainDatasetCode.code`, `DomainDataset.total_codes`, and
`DomainDatasetCode.components` are all `int`. The wide column key casts
`code_value` back to `Int64` before turning it into text.

### Float64 was a workaround, not a choice

`melt_schedule_frame` sends `code_value` through `_cast_numeric_to_float64`, the
helper written for `value`. That helper exists because different fields infer
different numeric dtypes, so pieces from different schedules must agree before
they are concatenated. A code column is always `Int64`, so it never needed the
helper. The Float64 then spread to the declared lookup schemas, the float
conversions in `_domain_datasets.py`, and the `fill_null(0).cast(nw.Int64)` step
in the column key.

Nulls are the one real reason Float64 looked necessary. A row from a schedule
with no code column has a null `code_value`. A numpy-backed pandas `int64` cannot
hold a null, so a union concat turns it into `float64`. The pandas path in this
package does not use numpy-backed integers for this column, though. The reader
returns nullable `Int64`, and a prototype shows that dtype survives the union
concat, `is_in`, joins, `group_by`, `coalesce`, and the cast to `String` on
pandas. Polars and pyarrow hold nulls in integer columns natively.

### Alternatives considered

1. **Cast the values to float at the two call sites.** Pass
   `sorted(float(code) for code in total_codes)`. This is the smallest fix, but
   it leaves the column a float that every consumer has to treat as an integer.
   Any future `is_in` or join against an `int` breaks the same way.
2. **Make `is_in_null_safe` cast the values to the column's dtype.** This is
   safe for every future caller, but it needs the frame's schema at expression
   build time, and it hides a dtype mismatch rather than removing it. A Float64
   column tested against `[155]` would still be a float column holding integers.
3. **Make `code_value` a string.** Codes are not arithmetic quantities, so a
   string is defensible. But every code the package declares is an `int`, the
   user guide and users filter on numbers, and a string sorts `100` before `20`.
   It would be a larger change for less benefit.
4. **Make `code_value` an `Int64` (chosen).** It matches the data, the reader,
   and the package's own types. It fixes the polars 2.0 failure at its source,
   and it removes code rather than adding it.

### What changes for users

`code_value` changes dtype in long format, the code grain, and domain datasets.

- pandas users get pandas' nullable `Int64` dtype (shown as `Int64`) rather than
  `float64`. Long format shows a missing code as `<NA>` rather than `NaN`.
  Comparisons such as `long["code_value"] == 81.0` and `.isin([54.0, 56.0])`
  still work, and a mask holding `<NA>` filters the row out.
- polars and pyarrow users get `Int64` (`int64` in pyarrow) rather than Float64.
- Printed values lose their trailing `.0`, so `110.0` reads `110`.
- The wide domain dataset column names (`pivot_domain_dataset_wide`) already use
  integer text (`100__accruing`) and do not change.

This is a visible change to a public column, so it gets a changelog entry.

### Why the integer must be nullable

Long format needs an integer dtype that can hold a null. Every row from a field
with no code (an RC field, for example) has no `code_value`. A numpy-backed
pandas `int64` cannot store a missing value, so stacking coded and plain rows
turns it into `float64` with `NaN`. That is how `code_value` became Float64. A
placeholder code such as `0` or `-1` would avoid the null, but a reader could
mistake it for a real code.

The code grain and domain datasets never hold a null `code_value`. They share
the column with long format, so one dtype everywhere keeps them consistent.

This adds no new requirement for pandas users. The pandas output already uses
the nullable dtypes for every other column. `load` returns `UNINUM`, the code
columns, and every integer measure as `Int64`, and `to_long_format` returns
`value` as `Float64`. These dtypes ship with pandas itself and are numpy-backed,
so they need neither pyarrow nor an extra install. Only one thing behaves
differently. `.astype("int64")` on the long format `code_value` raises because
of the nulls. It raises on today's Float64 column for the same reason.

## Approach

### A backend helper for a nullable integer cast

narwhals picks pandas' target dtype from the column's current one. A column that
is already nullable `Int64` (what the reader produces) casts cleanly. Two cases
start from a numpy-backed column, where `cast(nw.Int64)` maps to numpy `int64`
and raises on a null:

- an all-null `code_value` added with `nw.lit(None, ...)`, in
  `_with_is_multiple_flag` and `apply_domain_dataset_decoding`, and
- a caller's long frame whose `code_value` is numpy `float64` with `NaN`.

A new helper in `src/call_report/core/_backend.py` covers both. On pandas it
casts through the nullable dtype name that `nw.Schema.to_pandas` gives, the same
route `_build_declared_frame` already uses. On polars and pyarrow it is a plain
narwhals cast.

```python
def cast_nullable(
    *, frame: FrameOrLazy, column: str, dtype: nw.dtypes.DType
) -> FrameOrLazy:
    if frame.implementation is not nw.Implementation.PANDAS:
        return frame.with_columns(nw.col(column).cast(dtype))
    native = frame.to_native()
    pandas_dtype = nw.Schema({column: dtype}).to_pandas(dtype_backend="numpy_nullable")[
        column
    ]
    return nw.from_native(
        native.assign(**{column: native[column].astype(pandas_dtype)}),
        eager_only=True,
    )
```

Pandas has no lazy frame, so the pandas branch is always eager.

### Changes in `src/call_report/fca/_reshape.py`

| Where | Today | After |
|---|---|---|
| `melt_schedule_frame` | `_cast_numeric_to_float64(melted, column="code_value")` | `cast_nullable(..., dtype=nw.Int64())`, with `_cast_unknown_dtype` still resolving an empty piece |
| `_LOOKUP_SCHEMA["code_value"]` | `nw.Float64()` | `nw.Int64()` |
| `_DOMAIN_LOOKUP_SCHEMA["mapped_code"]` | `nw.Float64()` | `nw.Int64()` |
| `_CODE_REMAP_SCHEMA` `code_value`, `remapped_code` | `nw.Float64()` | `nw.Int64()` |
| `_with_is_multiple_flag` all-null column | `nw.lit(None, dtype=nw.Float64)` | added, then `cast_nullable(..., nw.Int64())` |
| `apply_domain_dataset_decoding` all-null column | same | same fix |
| `_with_column_key` | `fill_null(0).cast(nw.Int64).cast(nw.String)` | `cast(nw.String)` |
| `pivot_domain_dataset_wide` | same `fill_null(0)` step | `cast(nw.String)` |
| `_parse_wide_column_key` | `float(code_value_text)` | `int(code_value_text)` |
| `add_derived_code_rows` | `nw.lit(float(item.code)).cast(nw.Float64())` | `nw.lit(item.code, dtype=nw.Int64())` |

On pandas the literal in `add_derived_code_rows` is numpy `int64`. That is safe,
because the literal is never null and pandas promotes `int64` stacked on
nullable `Int64` to `Int64`.

`_cast_numeric_to_float64` keeps its job for `value`. Its docstring stops
mentioning `code_value`.

The docstrings that explain the `fill_null` step, and the
`_LOOKUP_SCHEMA` and `_CODE_REMAP_SCHEMA` docstrings, are updated to say the
column is `Int64`.

### Normalizing a caller's long frame

`convert_long_format_to_wide_format` and
`convert_long_format_to_code_grain_format` take a frame the caller supplies.
Without a cast, a Float64 `code_value` from version 0.1.0 would key a column as
`RCB__INV_CODE_10.0__BKVAL`, which `_parse_wide_column_key` then rejects. Each
converter normalizes the column on entry.

```python
def _with_integer_code_value(frame: FrameOrLazy) -> FrameOrLazy:
    """Cast a caller's `code_value` to Int64 if it is not already."""
    if frame.collect_schema()["code_value"] == nw.Int64():
        return frame
    return cast_nullable(frame=frame, column="code_value", dtype=nw.Int64())
```

A fractional code is not valid FCA data, so the plan does not check for one.
See "Open questions".

### Changes in `src/call_report/fca/_domain_datasets.py`

`_code_remap_lookup` and `_decoding_lookup` stop converting codes with `float()`.
Their docstrings say codes are carried as integers to match `code_value`. The
`_code_remap_lookup` doctest changes from `rows[60.0]` / `35.0` to `rows[60]` /
`35`.

### `is_in_null_safe`

It is unchanged. With an integer column, every current caller passes values of
the column's own dtype. Its docstring gains one sentence saying the values must
share the column's dtype, because polars does not convert them.

### The polars pin

`polars>=1.0` stays in both the extra and `dev`. The change works on polars 1.x
and 2.x, and #121 adds the matrix that keeps it that way. Capping at `<2` would
hold users back for a problem this change removes.

## Public API

No signature changes. The visible change is the dtype of a public column.

| Output | Column | Before | After |
|---|---|---|---|
| `FCACallReport.to_long_format` | `code_value` | Float64 | Int64, null for a plain field |
| `FCACallReport.to_code_grain_format` | `code_value` | Float64 | Int64 |
| `FCACallReport.to_domain_dataset` (long or code grain) | `code_value` | Float64 | Int64 |
| `convert_wide_format_to_long_format` | `code_value` | Float64 | Int64 |
| `convert_long_format_to_code_grain_format` | `code_value` | Float64 | Int64 |

`convert_long_format_to_wide_format` and
`convert_long_format_to_code_grain_format` gain this sentence in their
`long` parameter description: "A Float64 `code_value` holding whole numbers is
accepted and treated as Int64."

The `FCACallReport.to_long_format` docstring (`report.py:1072`) says `value`
and `code_value` are Float64. It changes to say `value` is Float64 and
`code_value` is Int64.

New private helper, in `call_report.core._backend`:

```python
def cast_nullable(
    *, frame: FrameOrLazy, column: str, dtype: nw.dtypes.DType
) -> FrameOrLazy:
    """Cast `column` to `dtype`, keeping its nulls on every backend."""
```

## Data

None. No shipped or checked-in data changes. The schedule metadata and the
institution registry do not store `code_value`.

## Pull requests

One pull request, on branch `issue-120-integer-code-value`, titled
`[BUG] Make code_value a nullable Int64 (#120)`.

The change fixes the red `test` workflow on `main` and changes the dtype in the
same step, so it does not split cleanly. It touches about 40 lines in `src/`,
plus tests and docs. It carries the `run-exhaustive` label, since it changes how
the archive is parsed into long format.

## Tests

All in the existing modules. None is slow except the archive tests that already
are.

`tests/core/test_backend.py`

- `cast_nullable` turns a numpy-backed pandas `float64` column holding `NaN`
  into nullable `Int64`, keeping the null. Parametrized over the three backends
  with the `backend` fixture.
- `cast_nullable` on a lazy polars frame stays lazy.
- `is_in_null_safe` against an `Int64` column with a null, under all three
  backends, drops the match and keeps the null. This replaces the Float64 case.

`tests/fca/test_reshape.py`

- `melt_schedule_frame` gives `code_value` dtype `Int64` for a coded schedule.
  It replaces `test_melt_schedule_frame_empty_coded_input_produces_float64_code_value`
  with the same empty-input case asserting `Int64`.
- `to_long_format` across a coded and a plain schedule gives `Int64` with a null
  for the plain rows, under all three backends. Without the pandas branch of
  `cast_nullable`, pandas would turn this into `float64`.
- `convert_wide_format_to_long_format` without a coded schedule gives an all-null
  `Int64` `code_value` (replaces the Float64 assertion).
- `test_code_grain_value_columns_are_float64` is split. `value` columns stay
  Float64, and `code_value` is `Int64`.
- `convert_long_format_to_wide_format` and
  `convert_long_format_to_code_grain_format` accept a long frame with a Float64
  `code_value`, under all three backends, and key it as `INV_CODE_15`, not
  `INV_CODE_15.0`. This covers the cast branch of `_with_integer_code_value`.
  The existing Int64 tests cover the early return.
- `exclude_reported_totals` drops a matching code with an `Int64` `code_value`,
  under all three backends. This is the test that fails on polars 2.0 today.
- `exclude_reported_totals` keeps a null `code_value`, under all three backends.
- `add_derived_code_rows` under polars with an `Int64` `code_value` appends the
  computed code row. This is issue step 2. The computed row's `code_value` is
  `Int64`, so the `strict` concat with the original frame succeeds.
- `pivot_domain_dataset_wide` names columns `100__accruing` with an `Int64`
  `code_value` (the existing tests, built from integer codes).
- `apply_domain_dataset_decoding` for a dataset drawing only on name-encoded
  sources gives an `Int64` `code_value` under pandas. This covers the new
  `cast_nullable` call there.

`tests/fca/test_reshape_properties.py`

- `test_coded_column_key_round_trips` draws integer codes and asserts the parsed
  `code_value` is an `int`.

`tests/fca/test_report.py`

- `test_to_domain_dataset_under_lazy_polars` passes. It is the other test that
  fails on polars 2.0 today.
- `to_domain_dataset(..., include_totals=False)` under eager polars drops every
  total code. This is the user path the issue reports.

`tests/fca/test_release_archive.py` (slow)

- The existing tiers cover every release. Any assertion on Float64 `code_value`
  moves to `Int64`. The cross-backend value comparison already compares
  `code_value`, so a pandas and polars mismatch fails it.

Docs and doctests

- `docs/source/user_guide/reshaping.rst` filters on `== 81.0`, `== 110.0`,
  `.isin([54.0, 56.0, 57.0])` and so on. These become integers, and the printed
  tables lose their `.0`. The Sphinx doctest builder runs them.
- `docs/source/changelog.rst` gains an unreleased entry: "`code_value` is now a
  nullable `Int64` in long format, the code grain, and domain datasets. It was
  Float64. This also fixes `to_domain_dataset(include_totals=False)` under
  polars 2.0."
- The `FCACallReport.to_domain_dataset` doctest at `report.py:1516` changes
  `== 110.0` to `== 110`.

## Open questions

1. **A fractional code.** The plan casts a caller's Float64 `code_value` without
   checking for a fraction. Polars truncates `10.5` to `10` silently, while
   pandas and pyarrow raise. Should the converters raise `ReshapeError` on a fractional code, so
   every backend fails the same way? It costs one extra check per call. The
   recommendation is to leave it out, since no FCA code has a fractional part.
2. **Version 0.1.0 frames.** The plan accepts a Float64 `code_value` from a
   caller. The alternative is to require `Int64` and raise. Accepting it costs
   one helper and keeps saved 0.1.0 frames usable. Is that compatibility worth
   keeping?
3. **Scheduled `test` run (issue step 5).** Adding a weekly `schedule` trigger to
   `test.yml` would surface a new upstream major release before an unrelated
   merge does. It is unrelated to the dtype change. The recommendation is a
   separate maintenance issue, so this pull request stays a bug fix.
