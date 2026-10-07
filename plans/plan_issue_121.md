# Plan: issue #121, Run the test suite under polars 1.x and polars 2.x

Status: proposed

## Goal

Run the test suite in CI against both polars majors.

- **polars 2.x.** The newest release. The existing 12 job matrix already
  installs it, and it keeps doing so.
- **polars 1.x.** One extra job, pinned to the declared floor
  `polars==1.0.0`, on `ubuntu-latest` with Python 3.12.

Both must pass at 100% branch coverage. The polars floor in `pyproject.toml`
stays at `polars>=1.0`, because 1.0.0 installs and passes the suite (see
"Why").

In scope:

1. A `polars` key in the `pytest` matrix of `.github/workflows/test.yml`, with
   one `include` entry for the 1.0.0 job.
2. Installing the pinned version in the same resolve as the dev dependencies,
   and a step that fails the job if the wrong polars version ended up
   installed.
3. Keeping the existing job names and the single Codecov upload unchanged.
4. A rule, written into `CLAUDE.md`, for where code that differs between polars
   majors checks the version.

Out of scope:

- Floor jobs for `pandas>=2.0`, `pyarrow>=15.0`, and `narwhals>=1.0`. The issue
  calls these a follow-up. See "Open questions".
- A scheduled run against the newest dependencies. The issue lists it as a good
  addition but not a replacement. It stays a separate maintenance issue, as the
  plan for #120 also recommended.
- Any change to `src/`. Nothing in the package behaves differently by polars
  major today.

## Why

### What CI misses today

`pip install -e ".[dev]"` resolves `polars>=1.0` to the newest release on every
run. So CI tests only the newest polars. It never tests the floor that
`pyproject.toml` promises, and a new major lands on every job at once. When
polars 2.0.0 changed `is_in`, all 12 jobs went red on an unrelated merge (#120).
A job pinned to each major would have shown one red major and one green one,
which points straight at the cause.

### The floor works as declared

Before choosing a pin, the floor was tried locally on `main` (after #120).

| Python | polars | narwhals | Suite | Result |
|---|---|---|---|---|
| 3.13 | 1.0.0 | 2.26.0 | full, archive tests included | 1895 passed, 100% branch coverage |
| 3.13 | 1.0.0 | 2.26.0 | `-m "not slow"` | 1438 passed, 100% branch coverage |
| 3.11 | 1.0.0 | 2.26.0 | `-m "not slow"` | 1438 passed, 100% branch coverage |
| 3.13 | 2.0.0 | 2.26.0 | the existing CI matrix | green on `main` |

The polars 1.0.0 wheel is `cp38-abi3`, so the same wheel installs on every
Python from 3.11 to 3.14. The issue allows a fallback to the newest 1.x if the
floor does not install or work. That fallback is not needed, so the pin is
`1.0.0` and `pyproject.toml` does not change.

Pinning the exact floor rather than the newest 1.x is deliberate. The newest
2.x is already covered by the main matrix. The value of the extra job is
proving the lower bound the package advertises. A user on polars 1.20 is very
likely fine if both 1.0 and 2.x pass.

### One job, not twelve

The polars layer that differs between majors is the same on every OS and
Python version. A single `ubuntu-latest` 3.12 job catches a polars 1.x break at
a twelfth of the cost of a second full matrix. Python 3.12 matches the job that
uploads coverage, so the two ubuntu 3.12 jobs differ only in polars.

### Keeping job names stable

Adding a `polars` key to the matrix changes the default name of every job, from
`pytest (ubuntu-latest, 3.12)` to `pytest (ubuntu-latest, 3.12, latest)`. If
branch protection lists the existing job names as required checks, every pull
request would then wait forever on checks that no longer exist. An explicit
`name:` on the job keeps the 12 existing names exactly as they are, and gives
only the new job a polars suffix:

```yaml
name: >-
  pytest (${{ matrix.os }}, ${{ matrix.python-version }}${{
  matrix.polars != 'latest' && format(', polars {0}', matrix.polars) || '' }})
```

The new job is named `pytest (ubuntu-latest, 3.12, polars 1.0.0)`.

### Why `latest` is in the base matrix

GitHub merges an `include` entry into an existing combination when none of its
keys overwrite that combination's values. If `polars` were not a base matrix
key, an entry `{os: ubuntu-latest, python-version: "3.12", polars: "1.0.0"}`
would not add a job. It would silently turn the existing ubuntu 3.12 job into
the 1.0.0 job, and polars 2.x would lose its coverage upload job. Declaring
`polars: ["latest"]` in the base matrix makes the entry conflict with every
existing combination, so GitHub adds it as a new 13th job. The issue's sketch
already does this. The plan keeps it and adds a comment saying why.

### Installing the pin in one resolve

The issue's sketch installs `.[dev]` first and then runs
`pip install "polars==1.0.0"` as a second step. That downgrades polars after
the fact, and pip does not check that the rest of the environment still accepts
it. Passing the pin in the same command makes pip resolve everything together,
so a dependency that needs a newer polars fails the install loudly instead of
leaving a broken environment:

```bash
pip install -e ".[dev]" "polars==1.0.0"
```

A step after the install then checks the installed version against the matrix
value. Without it, a future change to the install step could quietly test the
newest polars twice and stay green.

### Version-specific code

The issue asks that code which must differ between majors checks the polars
version in one place. Nothing needs such a check today. The #120 fix made
`code_value` an integer, which removed the only known difference at its source.
`CLAUDE.md` says to design shared abstractions from what the work reveals, not
speculatively, so the plan adds no helper now. It records the rule in
`CLAUDE.md` instead, so the first contributor who needs one knows where it goes
and how to keep coverage at 100%.

### Coverage

Each job enforces `--cov-fail-under=100` on its own. With no version-specific
branch in `src/`, both jobs reach 100% independently, as the local runs show.
So the plan does not combine coverage reports.

The rule added to `CLAUDE.md` covers the future case. A branch that runs only
under one major cannot reach 100% in the other job. It gets a test on each job
that drives the helper both ways, for example by monkeypatching the version
the helper reads. It does not get a `# pragma: no cover`. Combining coverage
across jobs would also work, but it moves the 100% gate out of each job into a
separate step, which is more machinery than one helper needs.

### Alternatives considered

1. **Pin `polars<2`.** This hides the break but leaves users on 2.x
   unsupported. Rejected in the issue.
2. **A second full matrix for polars 1.x.** Twelve more jobs for a difference
   that does not depend on OS or Python. Rejected for cost.
3. **A separate `pytest-polars-floor` job.** This avoids touching the matrix
   and its names, but it duplicates every step of the `pytest` job, and the two
   copies drift. The explicit `name:` gives the same stable names with one
   job definition.
4. **Pin the newest 1.x instead of 1.0.0.** This tests what most 1.x users run,
   but not the advertised floor. The floor works, so there is no reason to
   fall back.
5. **Raise the floor.** Not needed. 1.0.0 passes.

## Approach

### `.github/workflows/test.yml`

The `pytest` job becomes:

```yaml
jobs:
  pytest:
    # Spelled out so the 12 polars-latest jobs keep the names that branch
    # protection may list as required checks. Only the pinned job gets a
    # polars suffix.
    name: >-
      pytest (${{ matrix.os }}, ${{ matrix.python-version }}${{
      matrix.polars != 'latest' && format(', polars {0}', matrix.polars) || '' }})
    runs-on: ${{ matrix.os }}

    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, macos-latest, windows-latest]
        python-version: ["3.11", "3.12", "3.13", "3.14"]
        # "latest" must be a base matrix value. Without it, the include
        # entry below would merge into the existing ubuntu 3.12 job instead
        # of adding a new one.
        polars: ["latest"]
        include:
          # The floor declared in pyproject.toml (polars>=1.0). polars does
          # not differ by OS or Python here, so one job covers the 1.x major.
          - os: ubuntu-latest
            python-version: "3.12"
            polars: "1.0.0"

    steps:
      - name: Checkout
        # unchanged

      - name: Setup Python
        # unchanged

      - name: Install package with dev dependencies
        if: matrix.polars == 'latest'
        run: pip install -e ".[dev]"

      - name: Install package with dev dependencies and pinned polars
        if: matrix.polars != 'latest'
        run: pip install -e ".[dev]" "polars==${{ matrix.polars }}"

      - name: Check the polars version
        if: matrix.polars != 'latest'
        env:
          EXPECTED_POLARS: ${{ matrix.polars }}
        run: >-
          python -c "import os, polars;
          assert polars.__version__ == os.environ['EXPECTED_POLARS'],
          polars.__version__"

      - name: Show dependencies
        run: python -m pip list

      - name: Run tests
        # unchanged

      - name: Upload coverage to Codecov
        if: >-
          matrix.os == 'ubuntu-latest' && matrix.python-version == '3.12'
          && matrix.polars == 'latest'
        # rest unchanged
```

Notes on the steps:

- The version check reads the matrix value through `env`, not by putting
  `${{ }}` inside the script. That is the pattern the GitHub hardening guide
  recommends for any value that reaches a shell.
- The upload condition gains `matrix.polars == 'latest'`. Without it, the new
  job (also ubuntu 3.12) would upload a second report for the same commit.
- The `paths` filter already lists `.github/workflows/**` and
  `pyproject.toml`, so the pull request runs the new job.
- The slow archive tests run in the 1.0.0 job like in every other job. That
  matches tier 1 of the archive regression in `CLAUDE.md`.
- `exhaustive-regression.yml` is not changed. Tier 3 keeps running on the
  newest polars only.

### `CLAUDE.md`

A short paragraph is added under "Conventions", next to the dependency rules:

> **Dependency versions.** CI runs the full matrix on the newest polars, plus
> one job pinned to the declared floor (`polars==1.0.0`). Code that has to
> behave differently on each polars major checks the version in one helper in
> `call_report.core._backend`, never inline. Each job enforces 100% branch
> coverage by itself, so a version-specific branch is covered by tests that
> drive the helper both ways, not by `# pragma: no cover`. Raising a floor in
> `pyproject.toml` means raising the pin in `.github/workflows/test.yml` too.

The "Development environment" section gains the local equivalent:

```bash
pip install -e ".[dev]" "polars==1.0.0"   # reproduce the polars floor job
```

### Nothing else

No change to `src/`, `tests/`, `pyproject.toml`, or the docs. The changelog
gets no entry, because users see no change. The pull request carries the
`no-changelog` label.

## Public API

None. No public or private Python API changes.

## Data

None.

## Pull requests

One pull request, on branch `issue-121-polars-matrix`, titled
`[MNT] Test the polars 1.0 floor alongside polars 2.x (#121)`, with the
`no-changelog` label. It does not need `run-exhaustive`, since it changes
neither the archive nor how it is parsed.

#120 has merged, so polars 2.x is green on `main` and nothing blocks this.

## Tests

No new Python tests. The change is to CI, so the pull request's own CI run is
the test. The pull request description's test plan checks that:

- The run has 13 `pytest` jobs.
- The 12 existing jobs keep their exact names, for example
  `pytest (ubuntu-latest, 3.12)`.
- The new job is named `pytest (ubuntu-latest, 3.12, polars 1.0.0)`, its
  "Check the polars version" step passes, and "Show dependencies" lists
  `polars 1.0.0`.
- Every job passes at 100% branch coverage, the archive tests included.
- Only `pytest (ubuntu-latest, 3.12)` uploads to Codecov.
- `pre-commit run --all-files` passes, which lints the workflow YAML and the
  `CLAUDE.md` change.

Locally, before pushing, the implementer reproduces the pinned job with
`pip install -e ".[dev]" "polars==1.0.0"` and the same `pytest` command CI
uses.

## Open questions

1. **Floors for the other dependencies.** `pandas>=2.0`, `pyarrow>=15.0`, and
   `narwhals>=1.0` are also untested. The local runs above all used narwhals
   2.26.0, so whether narwhals 1.0 still works is unknown. pandas 2.0.0 ships
   no wheels for Python 3.12 and later, so its floor job would have to run on
   3.11. The
   recommendation is one follow-up issue that finds the real floor for each,
   raises any that are wrong, and adds them to the pinned job (renamed to a
   general "minimum versions" job). Should that be filed now?
2. **Required checks.** The plan keeps the existing job names in case branch
   protection lists them. Should the new polars 1.0.0 job also be made a
   required check once it is green? The recommendation is yes, so a break on
   the floor blocks a merge just as a break on the newest polars does.
3. **Scheduled run.** A weekly `schedule` trigger on `test.yml` would show a
   new upstream major before an unrelated merge does. The recommendation is to
   keep it out of this pull request and track it in its own maintenance issue.
