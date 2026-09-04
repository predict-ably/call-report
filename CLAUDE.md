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

### Version 0.2 Release Goals: Functionality to Process Metadata and Data
Finish creating the interface for the **FCA call report source** that does not require network access to download files. We will work toward handling the ability to download the files from FCA as a follow-on segment of work in release version 0.3.

This release will include several related processing capabilities for creating a standardized dataset from the FCA call report data.

1. We need to reason about and design a API for call report schedule APIs. This may be a further refinement of FCALayout or an update. This of a schema like object (e.g., like PyArrow or Polars schema) that provides a mapping of column names to column metadata (including definition, first and last period), etc. I will provide some ideas in the references section. We'll need some concept of schema drift over time. This includes comparing differences in schemas, but also being able to request schemas for a given schedule as a of given date. Do we have a master schema for each schedule that is cross-time, and it can return the schema present on a given date as a schema object? The schema should be dataframe agnostic at its core -- but have a method like to_dataframe(dataframe_type) that returns a dataframe with the schema information.

2. We can then inspect all the files from 2000 onwards to define schedule specific metadata schemas for each schedule and ship them with the project for user ease.

3. We need to extract the distinct UNINUM values that represent distinct institutions in the data from 2000 onwards. We need to create an API for getting information about the institution. It's most recent name, the lineage of names, addresses and other metadata stored as of the dates they changed. This will let us inspect that information at a point-in-time (quarter). We also will build toward the ability to know the current UNINUM post-merger of institutions so we can merger adjust. But that relies on other information. So it might come later. We just need the API for now. We should be able to get information on a single instution or convert all the institutions into a dataframe of information.

When we handle mergers and other Farm Credit System institution combinations, we'll need to do so based on the information published on the FCA website from 2003 onwards: [mergers are on archive report page](https://www.fca.gov/about/report-archives).

4. This release will also include functioanlity to process the FCA data supported in the Version 0.1 release download into several common architectures. This includes a long dataframe that has the UNINUM, Release_Date, Schedule, Variable_Name, and Value stored (long-format). The ability to pivot to wide-format (note we'll have to handle variables that appear in multiple schedules when we do this). Finally, we want to make it easy to create sub-architecture related to specific call-report schedules. For example, a dataset at the level of the "loan portfolio" that includes the dollars of exposure, charge-offs, non-performing loans, etc that are reported across multiple portfolios. We can provide a function that takes in the schedules and provides a Dataframe with institutions, loan portfolio, and release dates defining the rows and each variable measured for that combination parsed into a column.

### Version 0.3 Release Goals: Download FCA Call Reports
One difficulty when we proceed to downloading the files will be that when downloading the data the FCA uses cloudfare. Consider Python solutions for being able to download the data despite this. Otherwise, suggest that the package includes support for downloading the data from the package itself (e.g., ships with the data) or we host the data in an Azure BLOB.

Note that each FCA release includes metadata and the files with the actual data. We need to be able to process both. Users should also be able to specify a range of FCA call report release and have the object oriented interface provide them with all of that data.

The files for quarterly FCA call reports from 2000 onwards are available on the [FCA call report download page](https://www.fca.gov/bank-oversight/call-report-data-for-download).

FCA makes the current Call Report instructions available here [online](https://www.fca.gov/template-fca/bank/UCRCallRptInstructionsJune2026.pdf).

Consider the impact of [FCA Call Report Disclosures]
(https://www.fca.gov/bank-oversight/call-report-disclosures) that outline potential issues.

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

Both `pytest` commands above also execute every doctest in `src/call_report/**` docstrings (`[tool.pytest.ini_options]` adds `src/call_report` to `testpaths` with `--doctest-modules`). A docstring's `Examples` section is therefore live test code, not illustrative prose: it must actually run and match its shown output. Public classes, functions, and methods need a genuinely working example -- construct real objects and show real (ideally meaningful, not merely illustrative) output rather than a placeholder. Dunder methods (`__repr__`, `__len__`, etc.) don't need their own Examples section -- their behavior is usually already covered by the class's own example or another method's. Prefer `# doctest: +ELLIPSIS` over `# doctest: +SKIP` for output that's correct but inherently variable (a temp path, a memory address); reserve `+SKIP` for examples that truly cannot run in a sandboxed test (e.g. real network access).

Docs (Sphinx, numpydoc, pydata theme):

```bash
pip install -e ".[docs]"
sphinx-build -b html -W --keep-going docs/source docs/_build/html
```

`-W` makes any Sphinx warning a build failure. `.github/workflows/docs.yml`
builds the same way on every pull request touching `docs/` or
`src/call_report/`, and `.readthedocs.yaml` sets `fail_on_warning: true`, so a
warning that passes locally without `-W` still fails CI and the hosted build.
The one warning that is not the repo's own fault is an unreachable
`intersphinx` inventory (the Python, pandas, polars, and pyarrow object
inventories are fetched on every build). Re-run before looking for a cause in
the change itself.

`conf.py` also sets `nitpicky = True`, so every Python cross-reference must
resolve or the build fails. It is set there rather than passed as `-n` so
Read the Docs enforces it too. Two consequences when writing a docstring:
annotate a backend type by its full import path (`pandas.DataFrame`, not
`pd.DataFrame`), and reference an object this package documents rather than a
module that only holds it. A target that genuinely cannot resolve goes in
`nitpick_ignore` in `conf.py`, with its reason.

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
  Add tests alongside every new feature. See "Writing tests" below.
- **First-party** import name is `call_report` (underscore); the distribution name is
  `call-report` (hyphen).
- **Version** is single-sourced from `src/call_report/__init__.py` (`__version__`) via
  hatchling; bump it there.

## Writing docstrings

Beyond the numpy convention and the `numpydoc` gate, this project holds
docstrings to a specific standard of *voice* and *content*. A docstring
documents the code for someone reading it later. It is not a place to record
how the work went.

**Plain language.**

- Do not use dashes as sentence punctuation. That includes the em dash (—),
  the en dash (–), and the ASCII `--`, which Sphinx renders as an en dash.
  Use a full stop, a comma, or parentheses instead.
- Do not use semicolons. Split into two sentences, or use a comma-separated
  list.
- Prefer short sentences over one long sentence carrying three clauses. If a
  summary needs three things said, say them in three sentences or a list.

**Write for the reader.** Accuracy is not enough. Prefer the ordinary word
over the unusual one, say what happens rather than describing it at a
remove, and make sure every phrase is attached to the thing it describes. A
sentence a reader has to go back and re-parse is a defect even when it is
correct.

Not this:

> A missing or misshapen key raises `SchemaError` too, naming the dataset,
> the way the shipped-metadata parsers in `call_report.core` raise for the
> same kind of issue.

This:

> A definition with a missing or misspelled key also raises `SchemaError`,
> and the message names the dataset. The parsers in `call_report.core`
> raise the same error for the same problem.

The first packs three clauses into one sentence, reaches for "misshapen"
where "misspelled" is meant, leaves "naming the dataset" hanging off the
sentence with no clear subject, and ends on "raise for the same kind of
issue", which takes a second pass to resolve. The second splits it in two,
uses plain words, and says which thing does the naming.

**No anecdotes.** Do not write notes to the reviewer into a docstring. These
have all appeared here and have all been removed:

- "confirmed real", "verified", "confirmed directly", "confirmed to vary"
- "this has not been observed in any real FCA release"
- narrating how a bug was reproduced, which CI platform surfaced it, or
  which of two frames happened to be listed first

Keep the *fact* when it constrains the code, stated plainly: "RCO has zero
rows at 2000Q1" is useful. "Confirmed real: RCO has zero rows at 2000Q1" is
the same fact with a note about the author's confidence attached.

**Document the contract, not the line.** If a sentence explains why one
specific statement is written the way it is, it belongs in a code comment
next to that statement, not in the docstring. A docstring describes what a
caller can rely on. Rationale for a `collect_schema()` call over `.columns`
is a comment.

**Keep repeated parameters identical.** When the same parameter appears on
many functions (`dataframe_type`, `backend`, `schedule`), its description is
written once and copied verbatim. Three different wordings of one parameter
is a defect.

**Examples are tests.** `--doctest-modules` runs every `Examples` block, so
they must execute and match their output. Construct real objects and show
real, meaningful output. Prefer `# doctest: +ELLIPSIS` for genuinely variable
output; reserve `+SKIP` for what cannot run in a sandbox, such as live
network access.

## Writing tests

**State what is being tested and why.** Every test gets a docstring naming
the behavior under test. When a test exists because of a specific bug, say
what breaks without it, in one or two sentences, without narrating the
investigation.

**Never let a test leak global state.** `call_report.config` is
process-global. The autouse `reset_config` fixture in `tests/conftest.py`
restores it after every test, so no test needs its own `try/finally` and no
test can be broken by one that ran before it. Any future global state gets
the same treatment. Do not hand-roll cleanup.

**Test order is randomized.** `pytest-randomly` shuffles every run, so tests
must not depend on execution order. A failure under a shuffled order is a
real bug, not noise; reproduce it with the seed pytest prints
(`-p randomly --randomly-seed=<seed>`).

**Warnings are errors.** `filterwarnings = ["error"]` is set. A new warning
from pandas, polars, pyarrow, or this package fails the suite. Fix the cause.
Add a narrow, commented `ignore` entry only when the warning is genuinely
outside this project's control, never a blanket category suppression.

**Use fixtures where they earn their place.** A fixture that only returns a
constant is indirection without benefit; inline it. A fixture that sets up
state, tears it down, or parametrizes a test is worth having. Prefer the
`backend`, `polars_backend`, and `lazy_polars_backend` fixtures over an
inline `config_context` block, and make sure the backend stays active for
the *whole* test body, not just the arrange step.

**Helpers live in modules, not `conftest.py`.** pytest loads every
`conftest.py` as a plugin itself, so importing one as a library gives it two
identities in `sys.modules`. Shared helpers go in `tests/helpers.py` or a
sibling module such as `tests/fca/layouts.py`. `conftest.py` holds fixtures
and hooks only.

**Mark slow tests.** `tests/fca/test_release_archive.py` drives real archived
releases and dominates runtime. It carries `pytestmark = pytest.mark.slow`,
so contributors can iterate with `pytest -m "not slow"` (seconds instead of
minutes) while CI still runs everything. Register any new marker in
`[tool.pytest.ini_options]`; `--strict-markers` rejects unregistered ones.

**Know the three tiers of the archive regression.** Real-data testing is
layered so each tier costs what it is worth:

1. *Every pull request.* The full release history under pandas, a seeded
   stratified sample of 20 periods under all three backends, and 4 evenly
   spaced periods compared value-for-value across backends.
2. *`pytest -m "not slow"`.* None of the above. Seconds, for the edit-test
   loop.
3. *`pytest --run-exhaustive`.* Every archived release against every
   backend, plus a cross-backend value comparison on every release. Minutes,
   and skipped unless the flag is passed, so it never slows an ordinary pull
   request.

Tier 3 is gated by a flag rather than a marker alone because a marker can be
selected by accident with the wrong `-m` expression, while an unpassed flag
cannot. The tests stay collected either way, so `--collect-only` shows what
an exhaustive run would cover.

`.github/workflows/exhaustive-regression.yml` runs tier 3 in CI. It fires on
a pull request from a `release/*` branch, and on any pull request carrying
the `run-exhaustive` label. Both gate the merge, so a release or a new
quarter of archive data is covered before it reaches `main` rather than
after. Add the label to any pull request that changes what the archive
contains or how it is parsed, most obviously one dropping a quarter's zip
into `data/fca-call-report/`. The workflow can also be dispatched by hand
against any ref. It fails rather than skips when no archive zips are
present, because a green run of 735 skipped tests is worse than a red one.

**Reach for property-based tests on laws.** Where behavior has an invariant
that should hold for every input, not just chosen examples, use `hypothesis`
(see `tests/core/test_periods_properties.py`). Round trips, inverses, and
additivity are the usual candidates. `hypothesis` ships its own pytest
plugin, so there is no `pytest-hypothesis` package to add. Example-based
tests stay valuable for specific, known-tricky cases such as the FCA
2014/2015 era boundary; the two complement each other.

**Prefer real data for regression.** `data/fca-call-report/` holds every
published FCA release, and the suite regression-tests against all of them.
Synthetic fixtures cover structural scenarios; real archives catch what
synthetic data cannot reproduce.

## Repo layout

- `src/call_report/` — the package (src layout). Includes generated,
  shipped data alongside the code: `src/call_report/fca/data/schedules/`
  holds the canonical, authoritative FCA schedule metadata (one JSON
  `FileMetadata` per schedule root, produced by
  `scripts/generate_fca_schedule_metadata.py`), loaded lazily at runtime
  by `call_report.fca.get_fca_file_metadata`.
- `tests/` — pytest suite.
- `data/` — real, source-published regulatory archives checked into the
  repo, one subfolder per source (e.g. `data/fca-call-report/`, so it
  stays unambiguous once FFIEC/FDIC/NCUA equivalents are added). Not
  shipped in the built wheel (`[tool.hatch.build.targets.wheel] packages`
  only includes `src/call_report`); it exists so the repo itself ships
  ready-to-use historical data (no live/Cloudflare-protected download
  needed) and so `tests/fca/test_release_archive.py` can regression-test
  every real archived release. Update it by dropping in each new
  quarter's zip as FCA publishes it. `data/fca-schedule-metadata/base/`
  and `data/fca-schedule-metadata/overrides/` are the same kind of
  not-shipped, checked-in working data, specific to the schedule-metadata
  generation pipeline above -- see that script's module docstring.
- `docs/` — Sphinx documentation.
- `scripts/` — maintenance/release helpers, including
  `generate_fca_schedule_metadata.py`, the FCA schedule-metadata
  generation pipeline (run by a maintainer, not part of CI or the
  package's own runtime).
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
  - **Dataframe backends** (`pandas`, `polars`, `pyarrow`) are reached only through `narwhals` (e.g. `nw.from_dict(data, backend=...)` and `frame.to_native()` in `src/call_report/core/_backend.py`), which imports the selected backend lazily. They therefore stay optional install extras and are only ever test/dev dependencies.
  - **Any optional dependency narwhals does not front**  must be loaded lazily via the helpers in `src/call_report/core/_dependencies.py` — use `import_optional(...)` for an eager, checked import that raises a clear `pip install ...` error when the module is missing or older than a required `min_version`, and `_lazy_import`/`_LazyModule` for a deferred proxy. Reach for these instead of a bare `import`; the module follows polars' `_dependencies.py` pattern (https://github.com/pola-rs/polars/blob/main/py-polars/src/polars/_dependencies.py).
- The goal is to support multiple Python dataframe libraries via a package-level configuration that lets users choose the dataframe backend; prefer `narwhals` for this wherever possible, and keep any manual multi-library support behind optional ("soft") dependencies required only when that backend is configured.
- Match existing patterns in the FCA sub-module when extending to other sources.
