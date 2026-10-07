# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this package is

`call-report` provides a consistent Python interface for retrieving, parsing, and
analyzing regulatory **call report** data filed by regulated U.S. financial
institutions. Four reporting regimes are initially in scope:

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
Status: released as v0.1.0.

Established the standard object-oriented interface (`FCACallReport`), package-level configuration of the dataframe backend, and shared types such as `ReportingPeriod`, `PeriodRange`, and `FCASchedule`.

### Version 0.2 Release Goals: Process, Load, and Download FCA Call Reports
Status: goals 1 to 4 are complete. Goal 5 is the remaining work.

Finish the interface for the **FCA call report source**. Version 0.2.0 is cut (issue #64) only after goal 5 is complete. Merger adjustment is not part of this release. It is version 0.3.

1. Schedule schemas that track changes over time. `FieldSchema` maps column names to their metadata and the quarters each version applied. It supports `as_of`, `compare`, and `to_dataframe`.
2. Shipped schedule metadata for every schedule from 2000 onwards, loaded by `get_fca_file_metadata`.
3. Institution API (issues #57, #58, #59). `FCAInstitution` and `FCAInstitutionRegistry` give each UNINUM's name and address history, with a point-in-time view for any quarter. The registry ships with the package and is loaded by `get_fca_institution_registry`. The API tracks changed name and address attributes for each UNINUM. It does not link one UNINUM to another, and it does not merger adjust.
4. Reshaped datasets. `FCACallReport` returns long, wide, and code-grain formats, and curated domain datasets (for example a loan portfolio dataset) through `to_domain_dataset`.
5. Load and download release files. Release files reach `FCACallReport` through a transport. `LocalDirectoryTransport` reads extracted releases from a directory, and `PackagedArchiveTransport` reads the zips checked into `data/fca-call-report/`. This goal adds a transport that downloads releases.

   FCA's site uses Cloudflare, which may block automated downloads. First find out whether it does. If it does, find a Python approach that downloads the files reliably. If no reliable approach exists, suggest a source we control instead, such as data shipped with the package or hosted in an Azure BLOB.

   Each FCA release includes metadata and the files with the actual data. We need to be able to process both. Users should be able to specify a range of FCA call report releases and have the object oriented interface provide them with all of that data, whichever transport supplies it.

   The files for quarterly FCA call reports from 2000 onwards are available on the [FCA call report download page](https://www.fca.gov/bank-oversight/call-report-data-for-download). The [FCA call report landing page](https://www.fca.gov/bank-oversight/fcs-call-reports) describes the reports.

   FCA makes the current Call Report instructions available here [online](https://www.fca.gov/template-fca/bank/UCRCallRptInstructionsJune2026.pdf).

   Consider the impact of [FCA Call Report Disclosures](https://www.fca.gov/bank-oversight/call-report-disclosures) that outline potential issues.

### Version 0.3 Release Goals: Merger Adjustment
Status: not started.

Link UNINUMs across mergers and other Farm Credit System combinations, so that data can be merger adjusted. This is issue #60 (the successor API) and issue #61 (the dataset of successor events). The version 0.2 institution API is the base this builds on.

When an institution merges, its UNINUM usually stops reporting and the surviving institution reports under a different UNINUM, often with a different name. Names are therefore not used to infer succession. Every link between two UNINUMs comes from a sourced event.

1. Successor events (#61). A curated, shipped file of events. Each event records the predecessor UNINUM, the successor UNINUM, the quarter it took effect, the kind of event (for example a merger or a consolidation), and the source it was taken from. Events come from the information FCA publishes from 2003 onwards: [mergers are on the archive report page](https://www.fca.gov/about/report-archives).

2. Successor API (#60). Resolve any UNINUM to its current UNINUM by following events forward in time from a starting quarter. Each UNINUM resolves to one of three statuses.
   - *active*: it reports in the latest quarter.
   - *succeeded*: an event chain leads to a UNINUM that is active.
   - *unresolved*: it stopped reporting and no event explains why.

   An unresolved UNINUM is reported as unresolved. It is never guessed. A merger adjustment function adds the current UNINUM and its status as columns on a dataframe of call report data.

3. Gap report. List every UNINUM that stopped reporting without a recorded event. This is the work list for curating #61's events, and it shrinks as events are added.

### Version 0.4+ Release Goals
After the completion of the releases to support the FCA call report, the project's releases will move on to generalize the patterns to support the FFIEC, FDIC
and NCUA call report and related regulatory data. Sources:

- [FDIC API](https://api.fdic.gov/banks/docs/)
- [FFIEC Data](https://cdr.ffiec.gov/public/PWS/PWSPage.aspx)
- [NCUA Natural Person Credit Union & Corporate Credit Union Call Report Data](https://ncua.gov/analysis/credit-union-corporate-call-report-data/quarterly-data)

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
pytest -m "not slow"        # skip the slow archive tests while iterating
pytest --cov=call_report --cov-report=term-missing --cov-fail-under=100
ruff check .                # lint
ruff format .               # format
mypy                        # type-check (config targets src and tests)
pre-commit run --all-files  # run the full hook suite
pip install -e ".[dev]" -r .github/minimum-versions.txt   # reproduce the minimum versions job
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
- **Pull requests and issues** follow the templates in `.github/`. Fill in every
  section of `.github/PULL_REQUEST_TEMPLATE.md`, and open issues with the forms in
  `.github/ISSUE_TEMPLATE/`. This applies to pull requests opened by agents too.
- **Dependency versions.** CI runs the full matrix on the newest release of
  every dependency, plus one job on the oldest supported Python that installs
  the floors pinned in `.github/minimum-versions.txt`. Every floor in
  `pyproject.toml` for narwhals, pandas, polars, and pyarrow has a pin there,
  and `tests/test_minimum_versions.py` fails when the two disagree. Raise both
  together. Code that has to behave differently on each version of a
  dependency checks the version in one helper in `call_report.core._backend`,
  never inline. Each job enforces 100% branch coverage by itself, so a
  version-specific branch is covered by tests that drive the helper both ways,
  not by `# pragma: no cover`. When the oldest supported Python is dropped,
  move the job to the next one.

## Writing docstrings and other documentation

Beyond the numpy convention and the `numpydoc` gate, this project holds
docstrings to a specific standard of *voice* and *content*. A docstring
documents the code for someone reading it later. It is not a place to record
how the work went.

The same voice and content rules apply to every other piece of prose this
project ships, including the Sphinx user guide (`docs/source/user_guide/`)
and any other `docs/source/**` page. A user guide page documents what a
reader can rely on and how to use it. It is not a place to record how a
feature was curated, what alternatives were investigated, or why one design
was chosen over another. That reasoning belongs in a code comment, a test
docstring, a commit message, or a pull request description, whichever is
closest to the decision, not in the page a user reads to learn the API.

Not this:

> This dataset draws on schedule X alone rather than joining schedule Y,
> because spot-checking archive data found that Y's ending balance does not
> tie out to X's totals, a gap that held steady across several quarters and
> whose cause would need the official instructions to confirm.

This:

> This dataset draws on schedule X alone.

The first is a research note. The second is what a reader of the user guide
actually needs: the fact, stated plainly.

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

**No anecdotes.** Do not write notes to the reviewer into a docstring or a
documentation page. These have all appeared here and have all been removed:

- "confirmed real", "verified", "confirmed directly", "confirmed to vary"
- "this has not been observed in any real FCA release"
- narrating how a bug was reproduced, which CI platform surfaced it, or
  which of two frames happened to be listed first

Keep the *fact* when it constrains the code, stated plainly: "RCO has zero
rows at 2000Q1" is useful. "Confirmed real: RCO has zero rows at 2000Q1" is
the same fact with a note about the author's confidence attached.

**Document the contract, not the line.** If a sentence explains why one
specific statement is written the way it is, it belongs in a code comment
next to that statement, not in the docstring or the user guide. A docstring
or a user guide page describes what a caller can rely on. Rationale for a
`collect_schema()` call over `.columns` is a comment; rationale for why a
dataset draws on one schedule and not another is a comment, a commit
message, or a pull request description.

**Keep repeated parameters identical.** When the same parameter appears on
many functions (`dataframe_type`, `backend`, `schedule`), its description is
written once and copied verbatim. Three different wordings of one parameter
is a defect.

**Examples are tests.** `--doctest-modules` runs every docstring `Examples`
block, and Sphinx's doctest builder runs every `.. doctest::` block in
`docs/source/**`, so both must execute and match their output. Construct
real objects and show real, meaningful output. Prefer `# doctest: +ELLIPSIS`
for genuinely variable output; reserve `+SKIP` for what cannot run in a
sandbox, such as live network access.

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

- `src/call_report/` — the package (src layout). It also holds generated
  data that ships in the wheel and is loaded lazily:
  - `fca/data/schedules/`: one JSON `FileMetadata` per schedule root,
    loaded by `call_report.fca.get_fca_file_metadata`.
  - `fca/data/institutions/registry.json`: the name and address history
    of every UNINUM, loaded by `call_report.fca.get_fca_institution_registry`.
- `tests/` — pytest suite.
- `data/` — source-published archives checked into the repo, one folder per
  source (`data/fca-call-report/` holds every FCA release zip). Not shipped
  in the wheel. The test suite regression-tests against it. Add each new
  quarter's zip as FCA publishes it. `data/fca-schedule-metadata/` and
  `data/fca-institutions/` hold the `base/` and `overrides/` inputs of the
  two generation scripts.
- `docs/` — Sphinx documentation.
- `scripts/` — maintainer scripts, including
  `generate_fca_schedule_metadata.py` and
  `generate_fca_institution_registry.py`, which produce the shipped data
  above. Re-run both after adding a quarter's zip to `data/fca-call-report/`.
- `plans/` — one `plan_issue_<N>.md` per planned issue (see "Workflow"
  below). Not shipped in the built wheel.
- `pyproject.toml` — build, dependencies, and all tool configuration.
- `.pre-commit-config.yaml` — lint/format/type/docstring hooks.

## Workflow: plan, implement, review

Every change follows this workflow, however small. Work on an issue runs in three steps. Each step is a separate session, and each hands off through a file or a pull request rather than through conversation history.

1. **Plan.** Write the plan for issue N to `plans/plan_issue_<N>.md` on its own branch and open a pull request holding only that file (see "Git and pull requests" for branch and title names). The plan is reviewed and merged to `main` before any implementation starts. `plans/` sits at the repo root and is not shipped in the wheel.
2. **Implement.** A new session starts from the latest `main`, reads the merged plan, and implements it on a new branch. It opens a pull request that links the issue and the plan. In the same pull request, it changes the plan's status line to `Status: implemented in #NNN`. When the implementation has to depart from the plan, the pull request description says where and why.
3. **Review.** A review agent reviews the implementation pull request. It does not open a pull request of its own. It checks the change against its plan and against this file, and posts a GitHub review on the pull request. The review reports anything the plan asked for that is missing, anything added that the plan did not ask for, and any breach of the conventions above. When it finds problems, the review requests changes, and they are fixed on the same branch before the pull request merges.

### Plan layout

Every plan uses the same layout. It starts with a title line, `# Plan: issue #<N>, <issue title>`, followed by a `Status:` line. While the plan is under review, the status is `Status: proposed`. The sections below follow, as `##` headings, with these names and in this order. A section that does not apply stays in the plan and says "None." so the reader knows it was considered.

1. **Goal.** What the issue asks for, and what is out of scope.
2. **Why.** The problem this solves and who it is for. This is where design reasoning belongs. It covers the alternatives considered and why this approach was chosen over them.
3. **Approach.** How the change is implemented. It names the modules and files to add or change, the main steps, and how they fit the existing code.
4. **Public API.** New or changed classes, functions, and parameters, with their signatures and the docstring summary each will carry.
5. **Data.** Any shipped or checked-in data the change adds or regenerates, and the script that produces it.
6. **Pull requests.** How the work divides, if it needs more than one pull request, and the order they land in.
7. **Tests.** The behaviors each test covers, including the error branches needed for 100% coverage, and whether any test is slow.
8. **Open questions.** Decisions the plan leaves for the reviewer to make before it merges.

A small change gets short sections, not fewer sections.

### Writing a plan

A plan has a different purpose from a docstring. A docstring states the contract and leaves out the reasoning. A plan explains why the change is made and how it will be built, because the reviewer has to judge both before it merges.

The plain language rules for docstrings still apply to a plan, with the same aim of being easy to read and understand. Do not use dashes or semicolons as sentence punctuation. Prefer short sentences and ordinary words. Attach every phrase to the thing it describes. Lists and tables are welcome where they make the plan easier to follow.

Show code wherever it makes the plan clearer. This is strongly encouraged. Write proposed interfaces as real Python signatures with type hints, and describe complex logic in short pseudocode.

A plan is a Markdown file in the repo, so it must pass `pre-commit` like any other file (codespell, trailing whitespace, and the other hooks). A plan is not edited after it merges, except for the status line.

## Git and pull requests

**Branches.** Every pull request has its own branch.

- A plan goes on `plan/issue-<N>`.
- An implementation goes on `issue-<N>-<short-slug>`, for example `issue-60-successor-api`.
- When a session is given a branch name, it uses that name instead.
- A branch whose pull request has merged is not reused for new work. Start again from the latest `main`.

**Commits.** The subject line is `[TAG] Imperative summary (#N)`, about 72 characters or fewer, where `#N` is the issue. The tags are:

| Tag    | Use for                     |
|--------|-----------------------------|
| `ENH`  | new features                |
| `BUG`  | bug fixes                   |
| `DOC`  | documentation               |
| `TST`  | tests only                  |
| `MNT`  | maintenance, CI, tooling    |
| `PLAN` | a plan in `plans/`          |

The body explains why the change was made, not only what changed. Generated files are never edited by hand. Regenerate them with their script in `scripts/`.

**Pull requests.**

- The title follows the commit format. A plan pull request is titled `[PLAN] Issue #<N>: <issue title>`.
- The description links the issue (`Closes #N`, or `Part of #N` when the plan splits the work) and the plan. It summarizes the change, lists any departures from the plan with the reason for each, and ends with a test plan checklist.
- One issue per pull request, unless the plan divides the work differently.
- Once review has started, do not rebase or force-push. Merge `main` into the branch to bring it up to date.
- Add the `run-exhaustive` label when the change affects what the FCA archive contains or how it is parsed. Add `no-changelog` to maintenance-only changes.
- Pull requests are squash merged, so each one becomes a single commit on `main`.

## Working style
- Run `pre-commit`, `ruff`, `mypy`, and `pytest` before considering any change done.
- Fix the underlying issue rather than suppressing a check. Use `# noqa`, `# type: ignore`, or `# numpydoc ignore` only when the check is genuinely wrong for that line, for example when two hooks make contradictory demands on the same object (ruff's `D418` forbids docstrings on `@typing.overload` stubs while numpydoc requires one). Then use the narrowest suppression available (`# numpydoc ignore=<CODE>` over a blanket `# noqa`), on that object only.
- Make type hints precise. Prefer specific types, generics (`list[str]`, `Mapping[str, int]`), protocols, unions, and type variables over `Any`. Use `Any` only for genuinely dynamic data, and narrow it as soon as the type is known. The package ships `py.typed`, so its annotations are part of the public contract.
- Work in small, well-tested increments. Write basic tests first, then the implementation, then the advanced tests.
- Keep coverage at 100%, branches included. Cover edge cases, error branches, and fallbacks. Exclude a line only when it is genuinely not testable, and do it narrowly (`# pragma: no cover` on an `@overload` or `Protocol` stub's `...` body).
- **Ask before adding any dependency** (runtime, optional, or dev). `narwhals` is the only hard runtime dependency and the only third-party library that may be imported at module scope in `src/`. Test files may import anything.
  - The dataframe backends (`pandas`, `polars`, `pyarrow`) are reached only through narwhals (see `src/call_report/core/_backend.py`), which imports the configured backend lazily. They stay optional extras.
  - Any other optional dependency is loaded through `src/call_report/core/_dependencies.py`. Use `import_optional(...)` for a checked import that raises a clear `pip install ...` error, and `_lazy_import` for a deferred proxy. The module follows [polars' `_dependencies.py`](https://github.com/pola-rs/polars/blob/main/py-polars/src/polars/_dependencies.py).
- Match existing patterns in the FCA sub-module when extending to other sources.
