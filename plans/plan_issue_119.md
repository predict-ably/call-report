# Plan: issue #119, Download FCA call report releases with a downloader and a transport

Status: proposed

## Goal

Give an installed user a way to get FCA release files from the package itself.
This is goal 5 of the v0.2 release, and the last goal before 0.2.0 is cut
(#64). It is tracked in #54.

In scope:

1. A mirror of every FCA release zip, published as assets of a GitHub release
   of this repository, with a manifest of checksums. A workflow keeps it in
   step with `data/fca-call-report/`.
2. A shared, standard library HTTP helper with retries, and a shared zip
   extraction helper, in `call_report.core`.
3. `FCAReleaseCatalog`, which maps each quarter to its zip URL. It can be built
   offline from the hardcoded snapshot, or by reading the mirror's manifest or
   scraping FCA's download page when the user asks.
4. `FCAReleaseDownloader`, which downloads a range of quarters into a cache
   directory, verifies each zip, and extracts it to `<cache>/<zip stem>/`. It
   downloads from the GitHub mirror by default, and from the FCA website when
   asked. A quarter already in the cache is not downloaded again. Failures are
   recorded on the downloader and can be retried.
5. `DownloadTransport`, a thin `FCATransport` that asks a downloader for each
   period and then reads it with `LocalDirectoryTransport`.
6. An `available_periods()` method on every transport. `FCACallReport`
   validates a requested range against the transport rather than against
   `LATEST_KNOWN_PERIOD`.
7. Tests that never touch the internet in the default suite, and opt-in tests
   of the live mirror and the live FCA site.
8. A user guide page on downloading, and updates to the getting started page,
   the API reference, and the changelog.

Out of scope:

- Merger adjustment (version 0.3).
- Regenerating the shipped schedule metadata or institution registry for a
  quarter found by discovery. Those still come with a package release.
- Downloading in parallel from inside `FCACallReport.fetch`. A user who wants
  parallel downloads calls the downloader first.
- FFIEC, FDIC, and NCUA, though the shared helpers are written for them too.

## Why

### The gap

Two transports ship today. `LocalDirectoryTransport` reads releases the user
has already downloaded and extracted. `PackagedArchiveTransport` reads the zips
in `data/fca-call-report/`, which are not in the wheel. So an installed user
has to download and unzip every quarter by hand. `construct_fca_download_url`
already builds each URL and `DownloadError` already exists, but nothing
fetches a file.

### A mirror we control, and FCA as an option

FCA's site sits behind Cloudflare, which may block automated downloads, and a
government site can change its URLs or page layout without notice. This
repository already holds every release in `data/fca-call-report/`, 12 MB in
all. Publishing those zips as assets of a GitHub release gives every user a
source that:

- does not depend on Cloudflare's view of the client,
- has stable, predictable URLs,
- carries a SHA-256 for every file, so a download is verified, not trusted,
- costs nothing and needs no new account or credentials,
- is updated by the same pull request that adds a quarter's zip to the repo.

So the mirror is the default source. The FCA website stays available as
`source="fca"`, for a user who wants the file straight from the publisher, or
a quarter FCA has published before the mirror is updated.

The mirror lives in one rolling release with the tag `fca-call-report-data`,
not in the package's version tags. A new quarter adds one asset to it. Its tag
does not match `publish.yml`'s `v[0-9]+.[0-9]+.[0-9]+` pattern, so it never
triggers a PyPI publish, and it is created with `--latest=false` so the
package's own release stays the one GitHub shows as latest.

Alternatives considered for the mirror:

- **One release per quarter** (`fca-data-2026Q1`). Immutable, but a download
  URL then needs a lookup of which tag holds which quarter, and the release
  list fills with data tags.
- **A snapshot release per package version.** Every snapshot repeats all
  105 zips, and a quarter added between package releases is not mirrored
  until the next one.
- **Azure Blob Storage.** Reliable, but it needs an account, credentials for
  uploads, and someone to pay for it.
- **Zips in the wheel.** Offline, but the wheel grows every quarter, and a
  user who wants one quarter installs all of them.

### Cloudflare findings

PENDING. The development container's network policy still blocks
`www.fca.gov`, so the probe has not run. This section will record, for a
modern era (`2026March.zip`) and a legacy era (`Sept2014.zip`) URL and for the
download page:

- the status code and headers from plain `urllib.request`,
- whether a browser-like `User-Agent` and `Accept` header change the result,
- whether the response is the zip or a Cloudflare challenge page
  (`cf-mitigated: challenge`, or HTML where a zip is expected).

The finding no longer decides whether downloads work, because the mirror is
the default. It decides how well `source="fca"` works.

| Probe result | What `source="fca"` does |
|---|---|
| Plain `urllib` gets the zip | Uses it as is. |
| Only browser-like headers get the zip | The shared HTTP helper sends those headers. |
| Challenged or blocked either way | Raises a `DownloadError` that names Cloudflare and suggests `source="github"`. The docs say so. |

In every case the shared HTTP helper detects a challenge page and raises,
rather than writing an HTML page to disk as if it were a zip.

### Two layers over one implementation

The issue asks for a downloader and a transport, with only the downloader
fetching files. A downloader alone would make every user manage two objects
for one task. A transport alone would leave no clean way to download ahead of
time, fill a cache in CI or on an air-gapped machine, or recover from a failed
download. With both, the downloader does the work and `DownloadTransport` only
calls it.

Both write the folder layout that `LocalDirectoryTransport` reads
(`<cache>/<zip stem>/`). So downloaded and hand-extracted releases are read by
one code path, and only that path needs regression testing against
`PackagedArchiveTransport`.

### A default cache directory

"Download once, then read from the cache" works best when the user does not
have to choose a folder. So `cache_dir` is optional. When it is omitted, the
downloader uses a documented default.

1. The `CALL_REPORT_CACHE_DIR` environment variable, if set.
2. Otherwise `%LOCALAPPDATA%\call-report\Cache` on Windows,
   `~/Library/Caches/call-report` on macOS, and `$XDG_CACHE_HOME/call-report`
   (or `~/.cache/call-report`) elsewhere.

FCA files go in an `fca/` subfolder of it. The cache is a plain folder of
extracted releases, so `LocalDirectoryTransport(data_dir=<same folder>)` reads
it later with no network access, as the issue asks. A small stdlib function
does this. `platformdirs` would do the same, but it is a dependency for about
fifteen lines.

### Standard library only

| Client | New dependencies | Concurrency | Retries |
|---|---|---|---|
| `urllib.request` | none | `concurrent.futures` threads | written here |
| `requests` | urllib3, certifi, idna, charset-normalizer | threads | through urllib3 |
| `httpx` | httpcore, h11, anyio, certifi, idna | threads or async | connection errors only |
| `curl_cffi` | one binary wheel | threads or async | written here |

Downloads are about a hundred files of roughly 100 KB each. Connection pooling
and HTTP/2 do not matter at that size. If Cloudflare blocks by TLS
fingerprint, `urllib`, `requests`, and `httpx` all fail the same way, so a
third party pure Python client would add dependencies without changing the
outcome. Only `curl_cffi`, which copies a browser's TLS fingerprint, would. It
is not proposed. With the mirror as the default it is not needed.

Python releases the GIL during network reads, so a thread pool downloads
several files at once without async code. The pool is small by default (4
workers).

### Discovery is an object the user asks for

`LATEST_KNOWN_PERIOD` is a hardcoded snapshot. A user who wants a quarter
published after their package version was released needs a way to find it.

There are two ways to discover quarters, one per source.

- **The mirror's manifest.** `manifest.json` in the mirror release lists every
  quarter it holds. Reading it is one small request.
- **FCA's download page.** It can be scraped with `html.parser.HTMLParser` and
  a regular expression, so this needs no dependency either.

Both are kept separate and opt in, as ways to build a catalog object, rather
than a `discover=True` flag on the downloader, because:

- one lookup serves the downloader, the transport, and the report,
- parsing (`from_manifest_json`, `from_html`) is pure, so tests feed it saved
  input,
- the user can look at `catalog.latest` before downloading anything,
- a failed lookup raises where the user asked for it, instead of a flag
  quietly falling back to the snapshot,
- a maintainer can compare a discovered catalog with the snapshot to see when
  a new quarter has appeared.

The default stays the snapshot, so the default behavior makes no request
beyond the zips themselves and the mirror's manifest (see below).

### Each transport says which periods it can supply

`FCACallReport.fetch` rejects a range that ends after `LATEST_KNOWN_PERIOD`,
and `FCACallReport.available_periods()` returns the snapshot. That is wrong
for every transport once quarters can come from anywhere other than the
snapshot. A user who drops a new zip into `data/fca-call-report/`, or extracts
one into their own folder, cannot read it until a package release bumps the
constant.

So every transport gains `available_periods()`, and the report asks the
transport.

| Transport | `available_periods()` comes from |
|---|---|
| `PackagedArchiveTransport` | the zips present in `archive_root` |
| `LocalDirectoryTransport` | the release folders present in `data_dir` |
| `DownloadTransport` | its catalog |

The catalog stays a download concept. It maps periods to URLs, and a local
transport has no URLs. What every transport shares is the answer to "which
periods can you supply", not a catalog.

Adding a method to the `FCATransport` protocol breaks a transport a user has
written. The package is pre-1.0 alpha and the protocol shipped in 0.1.0, so
this plan accepts that and records it in the changelog. See "Open questions"
for the softer alternative.

### Failures are recorded, and can be retried

A failed quarter in a range does not stop the others. It is recorded on the
downloader, the same way `FCACallReport.fetch` records an unresolvable period
in `errors_`. Nothing in `src/` retries today. This plan adds two layers.

- Automatic retries inside the HTTP helper, for transient failures only.
- A `retry_failed()` method that downloads again only the quarters in
  `errors_`.

## Approach

### The mirror

**Release.** One GitHub release with the tag `fca-call-report-data` and the
title "FCA call report data". Its assets are:

- one zip per quarter, named exactly as FCA names it (`2026March.zip`,
  `Sept2014.zip`), byte for byte the file in `data/fca-call-report/`,
- `manifest.json`.

So a quarter's mirror URL is
`https://github.com/predict-ably/call-report/releases/download/fca-call-report-data/<stem>.zip`,
built from the stem with no lookup.

**Manifest.** `manifest.json` looks like this.

```json
{
  "schema_version": 1,
  "generated": "2026-10-12",
  "releases": [
    {
      "period": "2026-03-31",
      "file": "2026March.zip",
      "sha256": "…",
      "size": 123456,
      "source_url": "https://www.fca.gov/template-fca/bank/2026March.zip"
    }
  ]
}
```

**Script.** `scripts/build_fca_mirror_manifest.py` reads `data/fca-call-report/`
and writes `manifest.json`. It uses `parse_fca_release_filename` and
`construct_fca_download_url` from the package, so the mirror and the package
agree on names. It fails if a zip's name does not parse, or if a zip has no
`D_*.TXT` file.

**Workflow.** `.github/workflows/fca-mirror.yml` runs on a push to `main` that
changes `data/fca-call-report/**`, and by hand (`workflow_dispatch`). It:

1. builds `manifest.json` with the script,
2. creates the release with `gh release create fca-call-report-data --latest=false`
   if it does not exist,
3. uploads every zip whose name is not already an asset, and replaces
   (`gh release upload --clobber`) any whose SHA-256 differs from the published
   manifest,
4. uploads `manifest.json` last, so the manifest never lists a file that is
   not there yet.

It needs `contents: write` and uses the workflow's own `GITHUB_TOKEN`, as
`publish.yml` does. The first run, by hand after the implementation merges,
uploads all 105 quarters.

### The default source and the manifest

With `source="github"` (the default), the downloader reads `manifest.json`
once per downloader instance, and uses it for two things.

- **Verification.** Each zip's SHA-256 must match the manifest, or the quarter
  is recorded as an error and nothing is extracted.
- **Range.** If the user passed no catalog, the snapshot is extended with any
  newer quarter the manifest lists. So a quarter added to the mirror after a
  package release is downloadable without opt-in discovery. This is the one
  request the default makes beyond the zips.

If the manifest cannot be read, the downloader falls back to the snapshot for
the range and skips checksum verification, after the same retries as any
other request. The zip's own structure checks (below) still apply. See "Open
questions".

With `source="fca"`, there is no manifest. Each zip is checked for structure
only.

### New shared module: `src/call_report/core/_http.py`

Standard library only.

```python
def fetch_bytes(
    url: str,
    *,
    timeout: float = 60.0,
    max_retries: int = 3,
    headers: Mapping[str, str] | None = None,
) -> bytes: ...


def download_file(
    url: str,
    *,
    destination: Path,
    timeout: float = 60.0,
    max_retries: int = 3,
    headers: Mapping[str, str] | None = None,
) -> Path: ...
```

- Only `http` and `https` URLs are accepted, checked before `urlopen` is
  called. That check is what makes the narrow `# noqa: S310` on the `urlopen`
  line correct.
- Redirects are followed. GitHub serves release assets through a redirect to
  its download host.
- `download_file` streams to a temporary file in the destination's folder and
  renames it into place, so a failed download never leaves a partial file.
- Retries cover connection errors, timeouts, HTTP 429, and HTTP 5xx, with
  exponential backoff starting at 1 second. A `Retry-After` header on a 429 or
  503 is honored. A 404 or other 4xx is not retried.
- A response that looks like a Cloudflare challenge raises `DownloadError`
  that names Cloudflare, and is not retried.
- The sleep function is a module attribute, so tests replace it and never
  wait.
- Every failure is raised as `DownloadError`, with the URL and the reason.

### New shared module: `src/call_report/core/_archive.py`

```python
def extract_zip(*, archive: Path, target_dir: Path) -> Path: ...
def sha256_of(path: Path) -> str: ...
```

- Checks the file is a zip (`zipfile.is_zipfile`) and that `testzip()` finds
  no corrupt member, and raises `DownloadError` otherwise.
- Refuses a member whose path is absolute or climbs out of the target with
  `..`.
- Extracts into a temporary sibling folder and renames it to `target_dir`, so
  a folder that exists is always a complete extraction.
- `PackagedArchiveTransport.resolve` switches to `extract_zip`. Its behavior
  does not change.

### New shared module: `src/call_report/core/_cache.py`

```python
def default_cache_dir() -> Path: ...
```

Returns the folder described under "A default cache directory". It does not
create it. The downloader creates `<cache>/fca/` on first use.

### Changes in `src/call_report/fca/catalog.py`

- `fca_release_stem(*, period)` returns a period's zip stem (`"2026March"`,
  `"Sept2014"`) with no bounds check. `_default_dirname` in `transport.py`
  calls it, so a local transport can name a folder for a quarter newer than
  the snapshot.
- `construct_fca_download_url` keeps its bounds check and its output. It is
  the snapshot's URL rule for FCA.
- `construct_fca_mirror_url(*, period)` is the same rule for the mirror.
- `parse_fca_release_filename(name)` turns a zip filename into a period, or
  returns `None`. It accepts both eras, any letter case, and `Sep` as well as
  `Sept`.
- `FCAReleaseCatalog` is added (see "Public API").
- The scrape uses a private `HTMLParser` subclass that collects every `href`
  ending in `.zip`, resolved with `urllib.parse.urljoin`. Links whose
  filenames parse become catalog entries. Links that do not parse are kept on
  `unmatched`.
- A scraped catalog is the FCA snapshot with the scraped entries laid over it.
  A scraped URL replaces the predicted one for the same quarter, and a quarter
  the page lists after the snapshot is added. A quarter the snapshot has but
  the page omits keeps its predicted URL.
- A page with no parsable zip links raises `DownloadError` saying the page
  layout may have changed. An empty catalog is never returned.
- PENDING: the scraper is checked against the real page markup once the
  probe runs.

### New public module: `src/call_report/fca/download.py`

Holds `FCAReleaseDownloader`. In outline:

```python
def download(self, *, start, end) -> Self:
    periods = PeriodRange(start=start, end=end)
    catalog = self._resolve_catalog()  # catalog, or snapshot plus manifest
    for period in (periods[0], periods[-1]):
        catalog.url_for(period=period)  # raises PeriodNotAvailableError

    to_fetch, skipped = [], []
    for period in periods:
        if not self.overwrite and self._target(period).is_dir():
            skipped.append(period)
        else:
            to_fetch.append(period)

    with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
        outcomes = pool.map(self._download_one, to_fetch)  # ok, or an FCAIssue

    self.periods_, self.skipped_ = periods, tuple(skipped)
    self.downloaded_, self.errors_ = ...
    if to_fetch and not self.downloaded_:
        raise DownloadError(
            "None of the ... quarter(s) could be downloaded. See errors_."
        )
    return self
```

- `_download_one` downloads the zip to a temporary file in the cache, checks
  its SHA-256 against the manifest when there is one, checks it holds at least
  one `D_*.TXT` layout file, and extracts it with `extract_zip`.
- With `overwrite=True`, the new release is extracted to a temporary folder
  first. Only once it is complete is the old folder moved aside, the new one
  renamed into place, and the old one removed. A failed download leaves the
  quarter already in the cache untouched.
- Each worker writes only its own quarter's folder, and results are collected
  in the calling thread, so no lock is needed.
- `errors_` uses the existing `FCAIssue` with `schedule=None`. `FCAIssue`
  moves from `report.py` to a small private module so `download.py` can import
  it without importing the report. It stays exported where it is today.
- The state is set before the "every quarter failed" error is raised, so
  `retry_failed()` still works after it.
- `cache_dir` is expanded with `Path.expanduser()`. The folder is created if
  missing.

### Changes in `src/call_report/fca/transport.py`

- `FCATransport` gains `available_periods(self) -> tuple[ReportingPeriod, ...]`.
- `LocalDirectoryTransport.available_periods()` checks, for each quarter from
  `EARLIEST_PERIOD` through the quarter containing today, whether
  `data_dir / dirname_for(period)` is a folder. That is about 107 checks, and
  it works for any `dirname_for` because it never reverses a folder name.
- `PackagedArchiveTransport.available_periods()` does the same for
  `archive_root / f"{dirname_for(period)}.zip"`.
- `DownloadTransport` is added (see "Public API"). It builds one
  `FCAReleaseDownloader` with `max_workers=1` and the same `cache_dir`,
  `source`, `overwrite`, and `catalog`. `resolve` downloads the period if
  needed and then returns
  `LocalDirectoryTransport(data_dir=<its cache folder>).resolve(period=...)`.
- `DownloadTransport` keeps a set of periods it has already resolved. With
  `overwrite=True`, a period is downloaded again only on its first `resolve`
  on that transport instance. So one `fetch()` never downloads a quarter
  twice, and `fetch()` again on the same report reads the folder already in
  the cache. A new transport instance downloads again.
- The module docstring loses its sentence about a future network transport.

### Changes in `src/call_report/fca/report.py`

- `fetch` keeps the lower bound check against `EARLIEST_PERIOD`. It replaces
  the upper bound check against `LATEST_KNOWN_PERIOD` with the transport's
  latest available period. A range ending after it raises
  `PeriodNotAvailableError`. A transport with no available periods raises
  `DownloadError` naming where it looked.
- A gap inside the range, such as a missing folder, is still recorded in
  `errors_`, as today.
- `available_periods()` returns `self.transport.available_periods()`. It
  still does not need `fetch` to have run.
- `get_schema` reads the shipped metadata, which stops at the snapshot. For a
  quarter newer than the snapshot it raises a clear `ScheduleNotFoundError`
  saying the shipped metadata does not cover that quarter yet, and pointing
  to `get_layout`, which reads the release itself. `load`, the reshaping
  methods, and `institutions()` read the release's own files and work
  unchanged.

### FCA call report disclosures

PENDING. The disclosures page is blocked from the development container too.
This section will list each disclosed issue that changes what a downloaded
release contains or how it parses, and say whether the downloader, the
parser, or the documentation handles it.

## Public API

New in `call_report.fca.catalog`, and `FCAReleaseCatalog` also exported from
`call_report.fca`:

```python
FCASource = Literal["github", "fca"]

DOWNLOAD_PAGE_URL: str
"""str: FCA's call report download page."""

MIRROR_BASE_URL: str
"""str: The GitHub release that mirrors every FCA release zip."""


def fca_release_stem(*, period: ReportingPeriod) -> str:
    """Return the stem of a period's FCA release zip, such as "2026March"."""


def parse_fca_release_filename(name: str) -> ReportingPeriod | None:
    """Return the period an FCA release zip filename names, or None."""


def construct_fca_mirror_url(*, period: ReportingPeriod) -> str:
    """Construct the GitHub mirror URL for a period's Call Report zip archive."""


@dataclass(frozen=True, kw_only=True)
class FCAReleaseCatalog:
    """The FCA release zips available for download, keyed by period."""

    urls: Mapping[ReportingPeriod, str]
    sha256: Mapping[ReportingPeriod, str] = field(default_factory=dict)
    unmatched: tuple[str, ...] = ()

    @classmethod
    def from_snapshot(cls, *, source: FCASource = "github") -> Self:
        """Return the catalog of quarters known when this package was released."""

    @classmethod
    def from_manifest_json(cls, text: str) -> Self:
        """Return a catalog built from the GitHub mirror's manifest."""

    @classmethod
    def from_github(cls, *, timeout: float = 60.0, max_retries: int = 3) -> Self:
        """Download the GitHub mirror's manifest and return the catalog it lists."""

    @classmethod
    def from_html(cls, html: str, *, base_url: str = DOWNLOAD_PAGE_URL) -> Self:
        """Return a catalog built from the HTML of FCA's download page."""

    @classmethod
    def from_fca(cls, *, timeout: float = 60.0, max_retries: int = 3) -> Self:
        """Download FCA's download page and return the catalog it lists."""

    @property
    def periods(self) -> tuple[ReportingPeriod, ...]:
        """Return every period in the catalog, oldest first."""

    @property
    def latest(self) -> ReportingPeriod:
        """Return the newest period in the catalog."""

    def url_for(self, *, period: ReportingPeriod) -> str:
        """Return the URL of a period's release zip."""
```

`url_for` raises `PeriodNotAvailableError` for a period the catalog does not
hold. A catalog built from the mirror carries `sha256`. One built from FCA's
page or the snapshot does not.

New in `call_report.core`:

```python
def default_cache_dir() -> Path:
    """Return the folder call_report caches downloaded files in."""
```

New public module `call_report.fca.download`, with `FCAReleaseDownloader`
also exported from `call_report.fca`:

```python
class FCAReleaseDownloader:
    """Download FCA call report releases and extract them into a cache directory."""

    def __init__(
        self,
        *,
        cache_dir: str | Path | None = None,
        source: FCASource = "github",
        overwrite: bool = False,
        catalog: FCAReleaseCatalog | None = None,
        max_workers: int = 4,
        timeout: float = 60.0,
        max_retries: int = 3,
    ) -> None: ...

    def download(self, *, start: str | date, end: str | date) -> Self:
        """Download every quarter from start to end into the cache."""

    def retry_failed(self) -> Self:
        """Download again every quarter the last download recorded in errors_."""

    def download_period(self, *, period: ReportingPeriod) -> Path:
        """Download one quarter, unless it is already cached, and return its folder."""

    @property
    def data_dir(self) -> Path:
        """Return the folder the extracted releases are written to."""

    # Set by download and retry_failed:
    periods_: PeriodRange
    downloaded_: tuple[ReportingPeriod, ...]
    skipped_: tuple[ReportingPeriod, ...]
    errors_: list[FCAIssue]
```

`data_dir` is `<cache_dir>/fca`, the folder to hand to
`LocalDirectoryTransport` for offline reading. `__init__` only stores its
parameters, following the scikit-learn convention `FCACallReport` uses.
Passing both `source` and `catalog` is allowed. The catalog decides the URLs,
and `source` is then only used in error messages. `download_period` raises
`DownloadError` on failure rather than recording it, because it is the call
`DownloadTransport` makes, and `FCACallReport.fetch` records the error
itself. `retry_failed` with nothing to retry returns at once.

New in `call_report.fca.transport`:

```python
@dataclass(kw_only=True)
class DownloadTransport:
    """A transport that downloads each period's release on first use and caches it."""

    cache_dir: Path | None = None
    source: FCASource = "github"
    overwrite: bool = False
    catalog: FCAReleaseCatalog | None = None
    timeout: float = 60.0
    max_retries: int = 3

    def resolve(self, *, period: ReportingPeriod) -> Path: ...
    def available_periods(self) -> tuple[ReportingPeriod, ...]: ...
```

Changed:

- `FCATransport` gains `available_periods(self) -> tuple[ReportingPeriod, ...]`,
  "Return every period this transport can supply, oldest first."
- `LocalDirectoryTransport` and `PackagedArchiveTransport` gain
  `available_periods()`.
- `FCACallReport.fetch` validates the range against the transport.
- `FCACallReport.available_periods()` returns the transport's periods.

Repeated parameters have one description each, copied verbatim to the
downloader and the transport.

- `cache_dir`: "The folder to cache downloaded releases in. FCA releases go
  in its `fca` subfolder. If None, `call_report.core.default_cache_dir()` is
  used."
- `source`: "Where to download release zips from. `"github"` uses this
  project's mirror of every FCA release and verifies each file's checksum.
  `"fca"` downloads from the FCA website."
- `overwrite`: "If False, a quarter whose folder is already in the cache is
  not downloaded again. If True, every quarter is downloaded again and
  replaces the cached folder."

Usage, as the user guide will show it:

```python
from call_report.fca import FCACallReport, FCAReleaseDownloader
from call_report.fca.transport import DownloadTransport, LocalDirectoryTransport

# Download on first use from the GitHub mirror, then read from the cache.
report = FCACallReport(
    start="2020-03-31", end="2025-12-31", transport=DownloadTransport()
)

# Download ahead of time, straight from FCA, into a folder you choose.
downloader = FCAReleaseDownloader(cache_dir="~/fca-cache", source="fca").download(
    start="2020-03-31", end="2025-12-31"
)
downloader.errors_  # quarters that failed
downloader.retry_failed()

# Read the cache later with no network access.
report = FCACallReport(
    start="2020-03-31",
    end="2025-12-31",
    transport=LocalDirectoryTransport(data_dir=downloader.data_dir),
)
```

## Data

No shipped data changes.

Added:

- The `fca-call-report-data` GitHub release, filled by the new workflow from
  `data/fca-call-report/`, with `manifest.json` built by
  `scripts/build_fca_mirror_manifest.py`. It is not in the wheel.
- `tests/fca/data/download_page.html`, a trimmed copy of FCA's download page,
  saved once the probe runs. It keeps the markup around the zip links and adds
  one link that does not parse and one relative link.
- `tests/fca/data/manifest.json`, a small manifest for parser tests.

CLAUDE.md's repo layout section gains one line: adding a quarter's zip to
`data/fca-call-report/` also updates the mirror, through the workflow.

## Pull requests

Three pull requests, all `Part of #119` except the last, landing in this
order.

1. **`[ENH] Add shared HTTP, archive, and cache helpers and transport periods (#119)`.**
   `core/_http.py`, `core/_archive.py`, `core/_cache.py`, `fca_release_stem`,
   `parse_fca_release_filename`, `available_periods()` on the protocol and the
   two local transports, and the `FCACallReport` validation change.
   `PackagedArchiveTransport` moves to `extract_zip`.
2. **`[MNT] Mirror FCA release zips as a GitHub release (#119)`.**
   `scripts/build_fca_mirror_manifest.py` and its tests, and
   `.github/workflows/fca-mirror.yml`. After it merges, a maintainer runs the
   workflow once by hand to publish all 105 quarters.
3. **`[ENH] Add FCAReleaseDownloader and DownloadTransport (#119)`.**
   `construct_fca_mirror_url`, `FCAReleaseCatalog`, the downloader, the
   transport, the user guide page, and the doc, changelog, CLAUDE.md, and API
   reference updates. This one closes #119 and sets this plan's status line.

The third depends on the mirror existing only for its opt-in live test. Its
default tests use the local server. Pull requests 1 and 3 carry the
`run-exhaustive` label, because both change how archive releases are
resolved. Pull request 2 carries `no-changelog`.

## Tests

No test in the default suite reaches the internet. HTTP tests use a
`http.server.ThreadingHTTPServer` on `127.0.0.1`, started by a fixture in
`tests/conftest.py` and stopped after the test. It serves files from a
temporary folder, can answer a path with a redirect, and can be told to
answer a path with a given sequence of status codes. This runs the real
`urllib` code end to end. Its helpers live in `tests/http_server.py`, not in
`conftest.py`. The mirror and FCA base URLs are module attributes that the
tests point at the local server.

`tests/core/test_http.py`:

- A 200 response returns the body, and `download_file` writes it.
- A redirect is followed.
- A 503 then a 200 succeeds after one retry. A 429 with `Retry-After: 2`
  sleeps 2 seconds (the sleep is replaced, so the test does not wait).
- Retries stop at `max_retries`, and the last error is raised as
  `DownloadError`.
- A 404 raises at once, with no retry.
- A connection refused and a timeout are retried.
- An HTML challenge page, and a `cf-mitigated: challenge` header, raise a
  `DownloadError` naming Cloudflare.
- A `file://` or `ftp://` URL raises before any request is made.
- A failed `download_file` leaves no file behind.

`tests/core/test_archive.py`:

- A valid zip extracts to `target_dir`.
- A file that is not a zip, and a zip with a corrupt member, raise
  `DownloadError`.
- A member named `../evil.txt` or `/etc/evil.txt` raises, and nothing is
  written.
- A failed extraction leaves no `target_dir`.
- `sha256_of` matches `hashlib` on a known file.

`tests/core/test_cache.py`:

- `CALL_REPORT_CACHE_DIR` wins when set.
- Windows, macOS, and other platforms each give their documented folder, with
  `sys.platform` and the environment patched by `monkeypatch`.
- `XDG_CACHE_HOME` is honored, and `~/.cache` is used without it.

`tests/fca/test_catalog.py` (added to):

- `fca_release_stem` for both eras, and for a quarter after the snapshot.
- `parse_fca_release_filename` for both eras, mixed case, `Sep`, and names
  that do not parse. A `hypothesis` round trip:
  `parse(stem(p) + ".zip") == p` for every period.
- `construct_fca_mirror_url` for both eras.
- `from_snapshot` for each source matches the matching URL function for
  every period.
- `from_manifest_json` on the fixture reads URLs and checksums, and rejects a
  manifest with an unknown `schema_version` or a missing key with
  `DownloadError`.
- `from_html` on the saved page: every snapshot period is present, relative
  links are resolved, an unparsable zip link lands in `unmatched`, a page
  listing a newer quarter adds it, and a scraped URL replaces the predicted
  one.
- `from_html` on a page with no zip links raises `DownloadError`.
- `from_github` and `from_fca` against the local server.
- `url_for` raises `PeriodNotAvailableError` outside the catalog. `periods`
  and `latest` are correct.

`tests/fca/test_download.py`:

- With `source="github"`, `download` over a range fetches the manifest and
  every quarter from the local server, verifies checksums, and extracts each
  quarter to `data_dir/<stem>/`.
- A zip whose checksum does not match the manifest is recorded as an error
  and not extracted.
- A newer quarter in the manifest is downloadable with no catalog passed.
- An unreadable manifest falls back to the snapshot and skips verification.
- With `source="fca"`, quarters come from the FCA base URL and no manifest is
  requested.
- With `overwrite=False`, a cached quarter is in `skipped_` and is not
  requested.
- With `overwrite=True`, it is downloaded again and its contents replaced.
- With `overwrite=True` and a failing download, the cached folder is
  untouched.
- One quarter returning 404 lands in `errors_`, and the others download.
- Every quarter failing raises `DownloadError`, and `errors_` is set.
- `retry_failed` downloads only the failed quarters, and empties `errors_`
  once the server answers. With nothing to retry, it makes no request.
- A zip with no `D_*.TXT` file is recorded as an error.
- A range outside the catalog raises `PeriodNotAvailableError` before any zip
  is requested.
- `max_workers=1` and `max_workers=4` give the same result.
- `cache_dir=None` uses `default_cache_dir()` (pointed at `tmp_path` through
  `CALL_REPORT_CACHE_DIR`). A `cache_dir` with `~` is expanded, and a missing
  one is created.

`tests/fca/test_transport.py` (added to):

- `available_periods()` on `LocalDirectoryTransport` with folders for some
  quarters, with a custom `dirname_for`, and with an empty folder.
- `available_periods()` on `PackagedArchiveTransport` against the real
  archive returns 2000Q1 through the newest zip.
- `DownloadTransport.resolve` downloads on first use and reads the cache
  after that. With `overwrite=True`, a second `resolve` of the same period
  does not request it again. With `overwrite=False`, a cached folder is not
  requested at all.
- `DownloadTransport.available_periods()` returns its catalog's periods.

`tests/fca/test_report.py` (added to):

- `fetch` raises `PeriodNotAvailableError` for an `end` after the transport's
  latest period, and accepts an `end` after `LATEST_KNOWN_PERIOD` when the
  transport has that quarter.
- `fetch` with a transport that has no periods raises `DownloadError`.
- `available_periods()` follows the transport.
- `get_schema` for a quarter newer than the shipped metadata raises
  `ScheduleNotFoundError` with the message above.

`tests/scripts/test_build_fca_mirror_manifest.py`:

- The manifest built from `data/fca-call-report/` lists all 105 quarters, with
  checksums that match `hashlib`, and source URLs that match
  `construct_fca_download_url`.
- A zip whose name does not parse, and a zip with no `D_*.TXT`, fail the
  script.

`tests/fca/test_download_regression.py`:

- The local server serves the real zips from `data/fca-call-report/` with a
  manifest built by the script. For a legacy quarter (2003Q1), both sides of
  the era boundary (2014Q4 and 2015Q1), and the newest quarter,
  `FCACallReport` with a `DownloadTransport` gives the same `load_all()`,
  `to_long_format()`, and `institutions()` as with
  `PackagedArchiveTransport`. Four quarters keep this fast enough to run
  without the `slow` marker.

`tests/fca/test_live_sources.py`:

- Marked `network`, a new marker registered in `pyproject.toml`, and skipped
  unless `--run-network` is passed, the same way `--run-exhaustive` works.
- `FCAReleaseCatalog.from_github()` lists every snapshot period, and every
  checksum matches the zip in `data/fca-call-report/`.
- `FCAReleaseCatalog.from_fca()` lists every snapshot period.
- One legacy and one modern quarter, downloaded from each source, match the
  checked-in zips.
- Not run on pull requests.

Branch coverage stays at 100%, including both values of `overwrite` and both
sources on the downloader and on the transport.

## Open questions

1. **Cloudflare outcome.** PENDING the probe. It now only affects
   `source="fca"`.
2. **Unreadable manifest.** This plan falls back to the snapshot and skips
   checksum verification when the manifest cannot be read. The stricter
   choice is to raise, so a `source="github"` download is always verified.
3. **Protocol change.** This plan adds `available_periods()` to
   `FCATransport`, which breaks a user's own transport. The alternative is for
   `fetch` to fall back to the snapshot when a transport has no such method.
   That keeps old transports working at the cost of a `getattr` check.
4. **Live test schedule.** Should `weekly-maintenance.yml` run the `network`
   tests? It would show when FCA changes its page or blocks downloads, and
   when FCA publishes a quarter the mirror does not have yet.
5. **Default cache location.** This plan uses the platform's user cache
   folder, overridable by `CALL_REPORT_CACHE_DIR`. A cache folder can be
   cleared by the operating system or by cleanup tools. If users should keep
   downloads for good, a data folder (`~/.local/share/call-report`) is the
   alternative.
6. **Default worker count.** 4 is proposed. A lower number is kinder to the
   servers, and a higher one is faster for a first full download of about 105
   quarters.
