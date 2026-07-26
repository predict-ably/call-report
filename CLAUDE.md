# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this package is

`call-report` provides a consistent Python interface for retrieving, parsing, and
analyzing regulatory **call report** data filed by regulated U.S. financial
institutions. Three reporting regimes are initially in scope:

- **FCA** — Farm Credit Administration call reports for Farm Credit System institutions.
- **FFIEC** — Consolidated Reports of Condition and Income for banks.
- **FDIC** - For FDIC regulated institution Call Report and other FDIC regulated institution like summary of deposits
- **NCUA** — 5300 Call Reports for credit unions.

Each regime has its own filing agency, form structure, schedules, and history, but
they share the same essential shape: periodic, schedule-based financial and condition
data keyed by institution and reporting period. The goal is to expose that common
shape through one idiomatic, well-typed Python API while respecting each source's
specifics.

### Out of scope

FHFA-supervised entities do **not** file a standardized call report and are out of
scope: Fannie Mae and Freddie Mac (the Enterprises) and the Federal Home Loan Banks
report primarily through SEC filings (10-K/10-Q) and FHFA data feeds, not a call
report form comparable to FFIEC/NCUA/FCA. Do not add modules for them.

## Current focus

The goal is to proceed toward the first release of the package and then add subsequent releases that add new functionality. It is expected that the first versions (v0.1, v0.2, and v0.3) will progress toward full support of the **FCA call report source**.

### Version 0.1 Release Goals
Analyze the sources to determine a relatively standardized interface for working with the Call Report data. Then implement an initial "standard" object-oriented interface and other high-level package details.

This should include a package level configuration functionality to choose the dataframe backend to use (and later anything else necessary), the standard object-oriented interface.

Determination of any common objects that will be needed, including enumeration, objects that indicate the range of quarters to request data for (that can be passed to the standard interface), etc.

Context on data from different sources to achieve this:

[FCA Call Report Landing Page](https://www.fca.gov/bank-oversight/fcs-call-reports)

[FDIC API](https://api.fdic.gov/banks/docs/)

[FFIEC Data](https://cdr.ffiec.gov/public/PWS/PWSPage.aspx)

[NCUA Natural Person Credit Union & Corporate Credit Union Call Report Data](https://ncua.gov/ana)(lysis/credit-union-corporate-call-report-data/quarterly-data)

### Version 0.2 Release Goals
Create the interface for the **FCA call report source** that does not require network access. This is the API for the package first. Then we will work toward handling the ability to download the files from FCA as a follow-on segment of work.

One difficulty when we proceed to downloading the files will be that when downloading the data the FCA uses cloudfare. Consider Python solutions for being able to download the data despite this. Otherwise, suggest that the package includes support for downloading the data from the package itself (e.g., ships with the data) or we host the data in an Azure BLOB.

Note that each FCA release includes metadata and the files with the actual data. We need to be able to process both. Users should also be able to specify a range of FCA call report release and have the object oriented interface provide them with all of that data.

The files for quarterly FCA call reports from 2000 onwards are available on the [FCA call report download page](https://www.fca.gov/bank-oversight/call-report-data-for-download).

FCA makes the current Call Report instructions available here [online](https://www.fca.gov/template-fca/bank/UCRCallRptInstructionsJune2026.pdf).

Consider the impact of [FCA Call Report Disclosures]
(https://www.fca.gov/bank-oversight/call-report-disclosures) that outline potential issues.

### Version 0.3 Release Goals: Process FCA Call Reports
This release will include several related processing capabilities for creating a standardized dataset from the FCA call report data. This should be by providing an object-oriented interface to return a DataFrame of mergers that happened over time and the current UNINUM (institution identifier) for any prior UNINUM.

The first is to handle mergers and other Farm Credit System institution combinations based on the information published on the FCA website from 2003 onwards: [mergers are on archive report page](https://www.fca.gov/about/report-archives).

This release will also include functioanlity to process the FCA data supported in the Version 0.2 release download into several common sub-data architectures related to specific call-report schedules. For example, a dataset at the level of the "loan portfolio" that includes the dollars of exposure, charge-offs, non-performing loans, etc.

### Version 0.4+ Release Goals
After the completion of the releases to support the FCA call report, the project's releases will move on to generalize the patterns to support the FFIEC, FDIC
and NCUA call report and related regulatory data.

## Architecture

Each call report source lives in its **own sub-module** of the `call_report`
package. Functionality that is common across sources lives at the package level so it
can be reused rather than duplicated. Use pythonic package organization principles to organize common functionality at the package level (i.e., determine if sub-modules are needed and name them appropriately).

Follow pythonic package principles for organizing code artifacts within a sub-modules for a given call report source.

```
src/call_report/
├── __init__.py          # package version + top-level public API
├── <shared modules>     # reusable building blocks used by every source, e.g.:
│                        #   - HTTP/download client and caching
│                        #   - base parsing / schedule abstractions
│                        #   - reporting-period and date handling
│                        #   - shared data-model / schema types
├── fca/                 # Farm Credit Administration (implement first)
├── ffiec/               # bank call reports (version 0.4+)
├── fdic/                # bank call reports and other regulatory data (version 0.4+)
└── ncua/                # credit union 5300 call reports (version 0.4+)
```

Guidelines:

- Put source-specific logic (endpoints, form/schedule layouts, quirks, archived-vs-current
  handling) inside that source's sub-module.
- When two sources need the same capability, lift it to a shared package-level module
  rather than copying it. Design shared abstractions from what the FCA work reveals,
  not speculatively up front.
- Keep the public API consistent across sources so callers can switch regimes with
  minimal friction.

## Object Oriented Interface Architecture
The package will include object-oriented interfaces to Call Report and other regulatory data. Follow the type of object-oriented design patterns found in scikit-learn. Users should instantiate objects that have standard methods for performing the same tasks.

## Reference implementations

Before building a new source module, check the `references/` directory (gitignored,
not shipped) for prior or reference implementations that illustrate the source's data,
endpoints, and parsing quirks. Treat any reference only as a behavioral guide: verify
its assumptions against the live source, and reimplement idiomatically in Python
following this repo's conventions — never port another language's structure verbatim.

Note that the references for a given data source are in individual folders within `references/`. For example, `fca-call-report` contains information useful for the FCA call report implementation.

## Development environment

Editable install with dev tooling:

```bash
pip install -e ".[dev]"
```

Common commands:

```bash
pytest                      # run tests
pytest --cov=call_report --cov-report=term-missing --cov-fail-under=100
ruff check .                # lint
ruff format .               # format
mypy                        # type-check (config targets src and tests)
pre-commit run --all-files  # run the full hook suite
```

Docs (Sphinx, numpydoc, pydata theme):

```bash
pip install -e ".[docs]"
sphinx-build -b html docs docs/_build/html
```

## Conventions

- **Python** ≥ 3.11; support through 3.14. Use modern typing (`X | None`, `list[str]`).
- **Typing** is strict: `disallow_untyped_defs` is on. Every function/method needs
  type hints; keep the codebase clean under `mypy`.
- **Docstrings** follow the **numpy** convention and are validated by `numpydoc`
  (including examples and See Also where applicable). Public API needs complete docstrings.
- **Lint/format** via `ruff` (line length 88, double quotes). The lint rule set is broad
  (includes `E,W,F,I,UP,B,C4,SIM,TID,N,A,S,T20,PTH,RUF,D,Q`) — notably `PTH` (use
  `pathlib` over `os.path`), `S` (bandit security), and `T20` (no stray `print`).
- **Tests** live in `tests/` and run under `pytest`; branch coverage must stay at 100%.
  Add tests alongside every new feature.
- **First-party** import name is `call_report` (underscore); the distribution name is
  `call-report` (hyphen).
- **Version** is single-sourced from `src/call_report/__init__.py` (`__version__`) via
  hatchling; bump it there.

## Repo layout

- `src/call_report/` — the package (src layout).
- `tests/` — pytest suite.
- `docs/` — Sphinx documentation.
- `scripts/` — maintenance/release helpers.
- `pyproject.toml` — build, dependencies, and all tool configuration.
- `.pre-commit-config.yaml` — lint/format/type/docstring hooks.

## Working style
- Always run the package's pre-commit routine and tests on proposed code changes to ensure they pass. Run `ruff`, `mypy`, and `pytest` before considering any change done.
- Fix the underlying issue rather than suppressing a check. Don't reach for `# noqa`, `# type: ignore`, or `# numpydoc ignore` to make a lint/type/docstring failure go away unless the check is genuinely wrong for that line — e.g. two hooks make contradictory demands on the same object (such as ruff's `D418` forbidding docstrings on `@typing.overload` stubs while numpydoc-validation requires one). In that narrow case, prefer the most targeted available suppression (a specific `# numpydoc ignore=<CODE>` over a blanket `# noqa`), and only for the exact object in conflict — not the surrounding code.
- Use type hints everywhere, and make them precise and well-defined rather than reaching for `Any`. Prefer specific types, generics (`list[str]`, `Mapping[str, int]`), protocols, unions (`X | None`), and type variables that capture the real contract. Only use `Any` when it is genuinely the right choice for that context (e.g. bridging truly dynamic data), and prefer narrowing it as soon as the type is known. The package ships a `py.typed` marker, so its annotations are part of the public contract downstream users type-check against.
- Prefer small, well-tested increments. You should plan your implementation, then develop basic tests that can be used to check your implementation as it is being created. Then add the implementation and add any advanced testing.
- Aim for 100% test coverage (branch coverage included); the coverage gate is set to 100%. This is the starting goal for every change: cover the edge cases, error branches, and fallbacks, not just the happy path. Only fall back from 100% when a line is genuinely not meaningfully testable — and in that case exclude it explicitly and narrowly (e.g. `# pragma: no cover` on an `@overload`/`Protocol` stub's `...` body) rather than lowering the gate or leaving real code untested.
- Keep runtime dependencies minimal and deliberate, and **ask before adding any new dependency** (runtime, optional, or dev). `narwhals` is the one hard runtime third-party dependency currently and the **only** third-party library that may be hard-imported at module scope anywhere in `src/` at this time; any updates must be approved.
- Every third-party dependency other than `narwhals` must stay optional and must never be hard-imported at module scope in `src/` (test files may import them freely):
  - **Dataframe backends** (`pandas`, `polars`, `pyarrow`) are reached only through `narwhals` (e.g. `nw.from_dict(data, backend=...)` and `frame.to_native()` in `src/call_report/_backend.py`), which imports the selected backend lazily. They therefore stay optional install extras and are only ever test/dev dependencies.
  - **Any optional dependency narwhals does not front**  must be loaded lazily via the helpers in `src/call_report/_dependencies.py` — use `import_optional(...)` for an eager, checked import that raises a clear `pip install ...` error when the module is missing or older than a required `min_version`, and `_lazy_import`/`_LazyModule` for a deferred proxy. Reach for these instead of a bare `import`; the module follows polars' `_dependencies.py` pattern (https://github.com/pola-rs/polars/blob/main/py-polars/src/polars/_dependencies.py).
- The goal is to support multiple Python dataframe libraries via a package-level configuration that lets users choose the dataframe backend; prefer `narwhals` for this wherever possible, and keep any manual multi-library support behind optional ("soft") dependencies required only when that backend is configured.
- Match existing patterns in the FCA sub-module when extending to other sources.
