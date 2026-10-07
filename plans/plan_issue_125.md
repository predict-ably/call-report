# Plan: issue #125, Test the minimum versions of narwhals, pandas, and pyarrow

Status: implemented in #NNN

## Goal

Find the real lowest working version of each runtime dependency and
dataframe backend, declare it in `pyproject.toml`, and test it in CI.

In scope:

1. Raise three floors that do not work as declared.

   | Dependency | Declared now | New floor |
   |---|---|---|
   | narwhals | `>=1.0` | `>=2.10.1` |
   | pandas | `>=2.0` | `>=2.2.0` |
   | pyarrow | `>=15.0` | `>=20.0` |
   | polars | `>=1.0` | `>=1.0` (unchanged) |

2. Fix a bug that breaks `to_long_format` under every pandas 2.x release. A
   schedule set with no coded schedule gets the text `"None"` in
   `code_column` instead of a null.
3. Replace the pinned polars job from #121 with one "minimum versions" job
   on Python 3.11. It installs every floor in one resolve from a new pins
   file, `.github/minimum-versions.txt`, and checks each installed version.
4. Add a test that fails when a floor in `pyproject.toml` and its pin in
   `.github/minimum-versions.txt` disagree.
5. Update the "Dependency versions" rule in `CLAUDE.md` to cover every
   floor.
6. Changelog entries for the raised floors and the bug fix.

Out of scope:

- Floors for the development tools in `dev` (pytest, mypy, ruff, and the
  rest). They are not installed by users.
- The floors of `pandas-stubs` and `pyarrow-stubs`. They only matter for
  type checking, and the minimum versions job does not run mypy.
- A scheduled run against the newest dependencies. That stays the separate
  maintenance issue noted in the plan for #121.

## Why

### What was measured

Each finding below comes from a fresh virtual environment on Python 3.11,
installed in one resolve, running the full suite with the archive tests.

**narwhals.** 1.0.0 fails at import, because `nw.Schema` does not exist
yet. Every release up to and including 2.10.0 fails at least one test. In
2.10.0 the last failure is the dtype round trip in
`tests/core/test_schema_properties.py`: `repr(nw.List(nw.Int8))` gives
`List(<class 'narwhals.dtypes.Int8'>)`, which `call_report.core` cannot
parse back into a dtype. 2.10.1 is the first release that passes. Results
of the bisection, with the other floors pinned:

| narwhals | Result |
|---|---|
| 1.0.0 | import error |
| 1.23.0, 1.47.0 | fail |
| 2.3.0, 2.8.0, 2.10.0 | fail (dtype round trip) |
| 2.10.1, 2.10.2, 2.26.0 | pass |

**pandas.** Three separate problems.

1. *Every pandas 2.x.* `_with_is_multiple_flag` in
   `call_report/fca/_reshape.py` adds an absent `code_column` with
   `nw.lit(None, dtype=nw.String)`. On pandas 2.x, narwhals builds that
   column with `astype(str)`, which turns the null into the text `"None"`.
   pandas 3.0 gives a real null. Two existing tests catch it, but only
   under pandas 2.x, which CI never installs:
   `test_with_is_multiple_flag_adds_null_code_columns_when_entirely_absent`
   and `test_convert_long_format_to_code_grain_format_without_coded_rows_raises[pandas]`.
   A user on pandas 2.x who asks for only uncoded schedules gets `"None"`
   in `code_column`, and `convert_long_format_to_code_grain_format`
   returns a useless frame instead of raising `ReshapeError`. This is a
   bug in the package, so it is fixed rather than avoided by a floor.
2. *pandas older than 2.2.0.* Pivoting a nullable `Float64` column gives
   `object` columns. On the 2000Q1 release, 670 columns of the pandas wide
   format come back as `object` under pandas 2.1.4, and as `float64` under
   2.2.0. The cross-backend and round trip archive tests fail on every release
   tried from 2.0.0 to 2.1.4 (2.0.0 to 2.0.3, 2.1.0, 2.1.1, 2.1.4). They
   pass on 2.2.0, 2.2.1, 2.2.2, and 3.0.6.
3. *pandas 2.0.x and numpy 2.* pandas 2.0.0 does not cap numpy below 2,
   so a fresh install resolves numpy 2.x, and `import pandas` then fails
   with a binary incompatibility error. pandas 2.2.0 declares `numpy<2`
   itself, so the resolver picks numpy 1.26.4 and no numpy pin is needed.

**pyarrow.** The `pyarrow` extra and `dev` both require
`pyarrow-stubs>=20.0`, and every `pyarrow-stubs` release from 20.0 on
requires `pyarrow>=20`. So `pip install "call-report[pyarrow]"` can never
install a pyarrow older than 20 today. The declared `>=15.0` describes a
version the extra cannot deliver. With the stubs left out, pyarrow 15.0.0
also fails 15 tests under pandas 3, because it calls the deprecated
`make_block`, and the suite turns that warning into an error. pyarrow 17
and 20 pass.

**polars.** 1.0.0 passes, as #121 established. No change.

**Result.** With the bug fix applied, the environment
`narwhals==2.10.1 pandas==2.2.0 polars==1.0.0 pyarrow==20.0.0` on Python
3.11 (numpy 1.26.4 picked by the resolver) passes the full suite: 1895
passed, 100% branch coverage. Without the fix, the same environment fails
the two tests named above.

### Raise the pandas floor, or work around old pandas

Supporting pandas 2.0 and 2.1 would need a version check after every
pandas pivot, plus tests that drive that check both ways. pandas 2.2.0 was
released in January 2024, so the cost buys support for releases older
than that. It would also bring back the numpy 2 problem with pandas 2.0.x.
Raising the floor to 2.2.0 needs no version-specific code at all.

The null string bug is different. It affects every pandas 2.x release,
2.2 and 2.3 included, and the fix is the same on every backend. So it is
fixed in the package.

### One combined job on Python 3.11

The issue asked for one combined job unless a floor needs a different
Python. None does. Every pin has a wheel for Python 3.11, and pairing the
oldest Python with the oldest dependencies is the oldest environment a
user can actually build. So there is one job, `pytest (ubuntu-latest,
3.11, minimum versions)`.

A combined job does not say which dependency broke. That is acceptable,
because the job log lists every installed version, and a local run with
one pin changed finds the culprit in a minute.

### Dropping Python 3.11 later

The issue asks what happens to the pandas floor when Python 3.11 is
dropped. With the floor at 2.2.0, nothing. Every pin also has a Python
3.12 wheel (pandas 2.2.0, numpy 1.26.4, pyarrow 20.0.0, polars 1.0.0 as
`abi3`, narwhals as pure Python). So the job moves to Python 3.12 with no
floor change. The floors would next have to move when Python 3.12 is
dropped, because the first pandas with a Python 3.13 wheel is 2.2.3.

### A pins file, not matrix values

The polars job kept its pin in the workflow matrix. Four pins in matrix
values make a long `include` entry and a long install line. A plain
requirements file holds them in one place:

- `pip install -e ".[dev]" -r .github/minimum-versions.txt` installs the
  package and every pin in one resolve, as #121 required.
- The version check step reads the same file, so the check cannot drift
  from the install.
- A test reads both the file and `pyproject.toml`, so raising a floor
  without raising its pin fails the suite on every pull request.

### Alternatives considered

1. **Keep `pandas>=2.0` and patch the pivot result for old pandas.**
   Rejected. See above.
2. **Keep `pyarrow>=15.0` and move `pyarrow-stubs` out of the extra.**
   Rejected. The comment in `pyproject.toml` explains that the stubs are
   in the extra so that a downstream user's mypy sees `pyarrow.Table`
   rather than `Any`.
3. **Keep `narwhals>=1.0`.** Rejected. narwhals 1.0.0 cannot even import
   the package. Supporting anything before 2.10.1 would mean working
   around the dtype repr. narwhals is pure Python, so upgrading it costs a
   user nothing.
4. **One job per dependency.** Rejected. Three more jobs for failures the
   job log already points at.
5. **Pin numpy in the job.** Not needed. pandas 2.2.0 caps numpy itself.

## Approach

### 1. Fix the null `code_column` on pandas 2.x

In `_with_is_multiple_flag`, build `code_column` the same way `code_value`
is already built: a typed null literal, then `cast_nullable`, which casts
pandas columns to pandas' nullable dtypes.

```python
if "code_column" not in frame.collect_schema():
    frame = frame.with_columns(
        nw.lit(None, dtype=nw.Float64).alias("code_column"),
        nw.lit(None, dtype=nw.Float64).alias("code_value"),
        nw.lit(value=False).alias("is_multiple"),
    )
    # A null literal cannot be built as String on pandas 2.x (it becomes
    # the text "None") or as Int64 (numpy int64 has no null), so both are
    # built as Float64 and cast afterwards.
    frame = cast_nullable(frame=frame, column="code_column", dtype=nw.String())
    return cast_nullable(frame=frame, column="code_value", dtype=nw.Int64())
```

This was tried locally. The fast suite passes on pandas 2.0.0, 2.2.2, and
3.0.6, and the 2000Q1 archive tests pass on 2.2.2 and 3.0.6. Under pandas 3
the column's native dtype becomes the nullable `string` instead of `str`.
Both map to narwhals `String`, so the schema the package reports does not
change.

No other `nw.lit(None, dtype=...)` in `src/` builds a string. The others
are `Float64`, which pandas holds as `NaN`.

### 2. Raise the floors in `pyproject.toml`

```toml
dependencies = [
    "narwhals>=2.10.1",
]

[project.optional-dependencies]
pandas = ["pandas>=2.2.0", "pandas-stubs>=2.0"]
polars = ["polars>=1.0"]
# pyarrow-stubs 20 requires pyarrow>=20, so the pyarrow floor matches it.
pyarrow = ["pyarrow>=20.0", "pyarrow-stubs>=20.0"]
dev = [
    ...
    "pandas>=2.2.0",
    "polars>=1.0",
    "pyarrow>=20.0",
    ...
]
```

### 3. `.github/minimum-versions.txt`

```text
# The lowest supported version of each runtime dependency and dataframe
# backend. Each pin equals the floor declared in pyproject.toml, which
# tests/test_minimum_versions.py checks. The minimum versions job in
# .github/workflows/test.yml installs these.
narwhals==2.10.1
pandas==2.2.0
polars==1.0.0
pyarrow==20.0.0
```

### 4. `.github/workflows/test.yml`

The `polars` matrix key becomes `deps`. Only the parts that change are
shown.

```yaml
  pytest:
    # Spelled out so the 12 newest-dependency jobs keep the names that
    # branch protection may list as required checks. Only the minimum
    # versions job gets a suffix.
    name: >-
      pytest (${{ matrix.os }}, ${{ matrix.python-version }}${{
      matrix.deps != 'latest' && format(', {0} versions', matrix.deps) || '' }})

    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, macos-latest, windows-latest]
        python-version: ["3.11", "3.12", "3.13", "3.14"]
        # "latest" must be a base matrix value. Without it, the include
        # entry below would merge into the existing ubuntu 3.11 job instead
        # of adding a new one.
        deps: ["latest"]
        include:
          # Every floor in .github/minimum-versions.txt, on the oldest
          # supported Python. The floors do not differ by OS, so one job
          # covers them.
          - os: ubuntu-latest
            python-version: "3.11"
            deps: "minimum"

    steps:
      - name: Install package with dev dependencies
        if: matrix.deps == 'latest'
        run: pip install -e ".[dev]"

      # The pins go in the same resolve as the dev dependencies, so a
      # dependency that needs a newer version fails the install loudly.
      - name: Install package with dev dependencies at minimum versions
        if: matrix.deps == 'minimum'
        run: pip install -e ".[dev]" -r .github/minimum-versions.txt

      - name: Check the installed minimum versions
        if: matrix.deps == 'minimum'
        run: |
          python - <<'EOF'
          from importlib.metadata import version
          from pathlib import Path

          from packaging.requirements import Requirement

          for line in Path(".github/minimum-versions.txt").read_text().splitlines():
              if line.strip() and not line.startswith("#"):
                  requirement = Requirement(line)
                  installed = version(requirement.name)
                  assert installed in requirement.specifier, (requirement, installed)
          EOF

      - name: Upload coverage to Codecov
        if: >-
          matrix.os == 'ubuntu-latest' && matrix.python-version == '3.12'
          && matrix.deps == 'latest'
```

The check uses `packaging`, which `dev` already installs. The install and
check steps run on ubuntu only, so the bash heredoc is safe.

### 5. `tests/test_minimum_versions.py`

A new test module next to `tests/test_version.py`. It reads
`pyproject.toml` with `tomllib` and `.github/minimum-versions.txt` with
`packaging.requirements.Requirement`, then checks three things:

- Each of narwhals, pandas, polars, and pyarrow has exactly one `>=` floor
  wherever it is declared (`dependencies`, its own extra, and `dev`), and
  every declaration of a package gives the same floor.
- The pins file pins each of those four packages with `==` to that floor.
- The pins file pins nothing else.

Versions are compared as `packaging.version.Version`, so `20.0` and
`20.0.0` are equal.

### 6. `CLAUDE.md`

The "Dependency versions" bullet under "Conventions" becomes:

> **Dependency versions.** CI runs the full matrix on the newest release of
> every dependency, plus one job on the oldest supported Python that
> installs the floors pinned in `.github/minimum-versions.txt`. Every floor
> in `pyproject.toml` for narwhals, pandas, polars, and pyarrow has a pin
> there, and `tests/test_minimum_versions.py` fails when the two disagree.
> Raise both together. Code that has to behave differently on each version
> of a dependency checks the version in one helper in
> `call_report.core._backend`, never inline. Each job enforces 100% branch
> coverage by itself, so a version-specific branch is covered by tests that
> drive the helper both ways, not by `# pragma: no cover`. When the oldest
> supported Python is dropped, move the job to the next one.

The line under "Development environment" becomes:

```bash
pip install -e ".[dev]" -r .github/minimum-versions.txt   # reproduce the minimum versions job
```

### 7. Changelog

Two entries under "Unreleased" in `docs/source/changelog.rst`:

- The minimum supported versions are now narwhals 2.10.1, pandas 2.2.0,
  and pyarrow 20.0. polars stays at 1.0. Earlier versions do not work
  with this package (:issue:`125`).
- With pandas 2.x, `to_long_format` returns a null `code_column` when no
  requested schedule is coded. It returned the text `"None"`.
  `convert_long_format_to_code_grain_format` now raises `ReshapeError` for
  such a frame, as documented (:issue:`125`).

## Public API

No change to any class, function, or parameter. The supported versions of
three dependencies change, which the changelog records.

## Data

None. No shipped or checked-in data changes. `.github/minimum-versions.txt`
is CI configuration.

## Pull requests

One pull request, titled
`[MNT] Test and raise the minimum dependency versions (#125)`, closing
#125. It needs neither `run-exhaustive` nor `no-changelog`, because it
changes what users can install and fixes a bug they can hit.

The bug fix lands in the same pull request because the minimum versions
job is the only CI job that runs pandas 2.x. Split apart, the fix would
merge untested, or the job would merge red.

## Tests

- **Existing tests, newly run under pandas 2.x.** The two tests named in
  "Why" cover the bug fix. They fail in the minimum versions job without
  the fix and pass with it.
- **`tests/test_minimum_versions.py`.** One test per check in Approach
  step 5. Each states the rule it enforces. The module reads repository
  files, which tests already do for `data/`.
- **No new slow tests.** The archive tests run in the minimum versions job
  as in every other job, which is how the pandas pivot problem was found.

The pull request's test plan checks that:

- The run has 13 `pytest` jobs, and the 12 existing jobs keep their names.
- `pytest (ubuntu-latest, 3.11, minimum versions)` passes, including its
  version check step, and "Show dependencies" lists the four pins.
- `pytest (ubuntu-latest, 3.12, polars 1.0.0)` no longer runs.
- Every job passes at 100% branch coverage.
- Only `pytest (ubuntu-latest, 3.12)` uploads to Codecov.

Locally, before pushing, the implementer runs the full suite in a fresh
Python 3.11 environment installed with
`pip install -e ".[dev]" -r .github/minimum-versions.txt`, and the fast
suite on the newest dependencies.

## Open questions

1. **Required checks.** The job `pytest (ubuntu-latest, 3.12, polars
   1.0.0)` goes away. If branch protection lists it, it has to be replaced
   by `pytest (ubuntu-latest, 3.11, minimum versions)` when this merges,
   or every later pull request waits on a check that never runs. The
   recommendation is to make the new job required.
2. **A separate bug issue.** The `code_column` bug is user-facing. Should
   it get its own issue for the record, closed by the same pull request?
   The recommendation is no. The changelog entry and this plan describe
   it.
